"""The dispatcher: sync tasks to GitHub, then run implement/revise/review/merge jobs.

Every decision is re-derived from GitHub labels and PR comments on each tick, so the
process can be killed and restarted at any point. Local state (state.json) only
holds what GitHub can't: agent session ids, retry counters, and caches.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from agentpipe import milestones, proc, review, syncplan
from agentpipe.claude import CAPPED, MAX_TURNS, Claude, ClaudeResult
from agentpipe.config import Agent, Config, Milestone, Target
from agentpipe.deps import (PLAN_SCHEMA, Plan, PlanError, key_sort, merge_plans, stale_keys, text_hash,
                            validate_plan)
from agentpipe.git import Git
from agentpipe.github import GitHub
from agentpipe.status import (GRAPH_OWNED, TASK_MARKER, TICKET_CHANGE, TICKET_MARKER, Status,
                              status_from_labels, task_key_from_body)
from agentpipe.tasks import Task, fingerprint, mark_done, parse_tasks
from agentpipe.tickets import ticket_blockers

IMPL_KINDS = {"implement", "revise"}
RELEASE_KEY = "milestone"  # job key and slot of the milestone release job
MERGE_ATTEMPTS = 3  # tries to bring a behind branch up to date and merge it
MERGE_RETRY_SECONDS = 15  # lets GitHub finish computing mergeability after a push


def fence(text: str) -> str:
    return "\n\n```\n" + text + "\n```"


@dataclass
class Job:
    kind: str
    key: str
    slot: str
    future: Future


class CappedRun(RuntimeError):
    """An agent run hit its turn cap or timeout."""

    def __init__(self, result):
        super().__init__(result.subtype)
        self.result = result


class State:
    """Small JSON store, guarded by a lock and written atomically."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        for k, default in (("tasks", {}), ("db_ids", {}), ("links", {}), ("flagged", []),
                           ("cleared", []), ("paused_until", 0.0)):
            self.data.setdefault(k, default)

    def task(self, key: str) -> dict:
        with self.lock:
            return self.data["tasks"].setdefault(key, {})

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            replace_retrying(tmp, self.path)


def replace_retrying(src: Path, dst: Path, attempts: int = 50, delay: float = 0.1) -> None:
    """os.replace, retried: on Windows it fails with access denied while any other
    process (agentpipe watch, an editor, antivirus) has ``dst`` open, even just to read."""
    for i in range(attempts):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


class Pipeline:
    def __init__(self, cfg: Config, target: Target, gh: GitHub | None = None, log=print):
        self.cfg = cfg
        self.t = target
        self.gh = gh or GitHub(target.github_repo, cfg.gh_bin, cwd=target.repo_root)
        self.git = Git(target.repo_root)
        wd = target.work_dir
        self.base_wt = wd / "base"
        self.logs = wd / "logs"
        wd.mkdir(parents=True, exist_ok=True)
        mcp = wd / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": cfg.mcp_servers}, indent=2), encoding="utf-8")
        self.claude = Claude(cfg.claude_bin, self.logs, cfg.agent_timeout, on_denials=self.log_denials,
                             mcp_config=mcp, on_rate=self.note_rate)
        # Agents start only once the environment checks pass (see preflight).
        self.env_ok = not (target.preflight or target.preflight_gate)
        self.last_preflight = 0.0
        self.issue_map: dict[str, dict] = {}  # open issues by key, as of the last schedule()
        self.state = State(wd / "state.json")
        self.plan_path = wd / "plan.json"
        self._log = log
        self.tasks: dict[str, Task] = {}
        self.ticket_bodies: dict[str, str] = {}  # ticket key -> issue body (the ticket's spec)
        self.plan: Plan | None = None
        self.last_sync = 0.0
        self.jobs: dict[str, Job] = {}
        self.reviewed: dict[int, str] = {}  # PR number -> head sha already reviewed
        self.pool = ThreadPoolExecutor(max_workers=cfg.max_implementers + cfg.max_reviewers + 2)
        self.stopping = False

    def log(self, msg: str) -> None:
        line = time.strftime("%H:%M:%S ") + f"[{self.t.name}] " + msg
        self._log(line)
        self.logs.mkdir(parents=True, exist_ok=True)
        with open(self.logs / "pipeline.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d ") + line + "\n")

    def log_denials(self, run: str, denials: list[dict]) -> None:
        """One dispatcher line per run with denied tool calls; `agentpipe denials` has the detail."""
        from agentpipe.denials import describe
        shown = "; ".join(describe(d, 80) for d in denials[:3])
        more = f" (+{len(denials) - 3} more)" if len(denials) > 3 else ""
        self.log(f"{len(denials)} tool call(s) denied in {run}: {shown}{more}")

    # ------------------------------------------------------------------ paths
    def base_ref(self) -> str:
        return f"origin/{self.t.base}"

    def wt(self, task: Task) -> Path:
        return self.t.work_dir / "wt" / task.slug

    def agent_def(self, role: str) -> Agent:
        """The shared agent definition, plus this target's extra tools for agents that edit or test."""
        a = self.cfg.agents[role]
        if role == "planner" or not self.t.extra_tools:
            return a
        return Agent(a.model, a.max_turns, a.tools + self.t.extra_tools, a.deny)

    def env(self, slot: str) -> dict:
        env = dict(os.environ)
        # "{NAME}" in a value expands to the inherited value, e.g. PATH = "X;{PATH}".
        for k, v in self.t.env.items():
            env[k] = v.replace("{" + k + "}", os.environ.get(k, ""))
        env["CARGO_TARGET_DIR"] = str(self.t.work_dir / "target" / slot)
        # Keep agents', tests' and tools' temp files off C:.
        tmp = self.t.work_dir / "tmp" / slot
        tmp.mkdir(parents=True, exist_ok=True)
        env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(tmp)
        return env

    def context_list(self) -> str:
        return ", ".join(f"`{c}`" for c in self.t.context) or "nothing beyond the change itself"

    # ------------------------------------------------------------------ sync
    def refresh_base(self) -> None:
        self.git.fetch()
        if not (self.base_wt / ".git").exists():
            self.git.worktree_detached(self.base_wt, self.base_ref())
        else:
            self.git.git("checkout", "--force", "--detach", self.base_ref(), cwd=self.base_wt)
            self.git.git("clean", "-fdq", cwd=self.base_wt)

    def load_tasks(self) -> list[Task]:
        tasks = []
        for change in self.t.changes(self.base_wt):
            path = self.base_wt / self.t.changes_dir / change / "tasks.md"
            tasks += parse_tasks(change, path.read_text(encoding="utf-8"))
        return tasks

    def make_plan(self, tasks: list[Task], fp: str, old: Plan | None = None) -> Plan:
        """Plan the target's tasks. With an ``old`` plan, only new or reworded tasks (and
        batches they touch) go to the planner, unless so much changed that a full plan is
        as cheap; everything else is kept as it was."""
        stale, removed = stale_keys(tasks, old) if old else (set(), set())
        open_keys = {t.key for t in tasks if not t.done}
        incremental = old is not None and len(stale) <= max(10, len(open_keys) // 2)
        if incremental and not stale:
            # Only removals, or only done tasks changed: no planner needed.
            return validate_plan(tasks, merge_plans(old, {"tasks": []}, stale, removed), fp, self.cfg.max_batch)
        plan_t = [t for t in tasks if t.key in stale] if incremental else [t for t in tasks if not t.done]
        self.log(f"planning {len(plan_t)} task(s)" + (f" incrementally ({len(open_keys)} open)" if incremental else ""))
        prompt = self.planner_prompt(tasks, plan_t, old if incremental else None, removed)
        errors = ""
        for attempt in range(2):
            p = prompt if not errors else prompt + f"\n\nYour previous answer was rejected: {errors}\nFix every problem."
            res = self.claude.run(p, self.base_wt, self.cfg.agents["planner"], PLAN_SCHEMA,
                                  log_name=f"plan-{fp.split(':')[0]}-{attempt}")
            if res.limited:
                raise LimitHit(res.text)
            if not res.ok:
                errors = f"run failed: {res.text[:500]}"
                continue
            raw = merge_plans(old, res.structured, stale, removed) if incremental else res.structured
            try:
                return validate_plan(tasks, raw, fp, self.cfg.max_batch)
            except PlanError as e:
                errors = str(e)
                self.log(f"plan rejected: {errors}")
        raise PlanError(f"planner failed twice: {errors}")

    def planner_prompt(self, tasks: list[Task], plan_t: list[Task], old: Plan | None, removed: set[str]) -> str:
        def line(t: Task) -> str:
            return f"- {t.key} | {t.section} | {t.text}"

        planned = {t.key for t in plan_t}
        existing = ""
        if old is not None:
            rows = []
            for t in sorted((t for t in tasks if not t.done and t.key not in planned), key=lambda t: key_sort(t.key)):
                d = old.get(t.key)
                extra = (f" [batch {d.batch}]" if d.batch else "") + ("" if d.autonomous else " [needs human]")
                rows.append(f"{line(t)} <- {', '.join(d.blocked_by) or '-'}{extra}")
            gone = f"\nRemoved since the last plan: {', '.join(sorted(removed, key=key_sort))}\n" if removed else ""
            existing = (
                "\n## Already planned (kept as is)\n\nThese open tasks keep their entries; each line ends "
                "`<- blockers [batch]`. They are valid blockers for the tasks above. Return an entry for "
                "one of them only if a task above must change it (e.g. it must now wait for a new task); "
                "a returned entry replaces the old one whole. You may add a task above to an existing "
                "batch by giving it that batch id.\n\n" + ("\n".join(rows) or "(none)") + "\n" + gone)
        rule = ('Every task under "Open tasks to plan" must appear exactly once.' if old is None else
                'Every task under "Open tasks to plan" must appear exactly once; add entries for '
                "already-planned tasks only to change them.")
        return review.prompt(
            "planner",
            repo_root=self.base_wt, target=self.t.name, project_dir=self.t.project_dir,
            context_list="\n".join(f"- `{c}`" for c in self.t.context) or "(no extra context docs)",
            changes_list="\n".join(f"- `{c}`" for c in sorted({t.change for t in tasks})),
            open_tasks="\n".join(line(t) for t in plan_t) or "(none)",
            done_tasks="\n".join(f"- {t.key} | {t.text}" for t in tasks if t.done) or "(none)",
            existing_section=existing, answer_rule=rule, max_batch=self.cfg.max_batch,
        )

    def sync(self, gh: GitHub | None = None, on_ready=None) -> list:
        """Reconcile GitHub with tasks.md. ``on_ready`` runs once every ready task has
        its issue, before the slower rest (blocked issues, links), so agents start early."""
        gh = gh or self.gh
        self.refresh_base()
        tasks = self.load_tasks()
        fp = fingerprint(tasks) + ":" + ",".join(sorted({t.change for t in tasks}))
        plan = Plan.load(self.plan_path)
        if plan is not None and not plan.texts:
            # A plan from before per-task hashes: treat its tasks as unchanged.
            plan.texts = {t.key: text_hash(t) for t in tasks if t.key in plan.tasks}
            plan.save(self.plan_path)
        if plan is None or plan.fingerprint != fp:
            plan = self.make_plan(tasks, fp, plan)
            plan.save(self.plan_path)
        self.tasks = {t.key: t for t in tasks}
        self.plan = plan
        issues = self.issues_by_key(gh.list_issues())
        st = self.state
        actions = syncplan.compute(tasks, plan, issues, set(st.data["flagged"]), set(st.data["cleared"]))
        pending_ready = on_ready is not None
        for a in actions:
            if pending_ready and not syncplan.makes_ready(a):
                st.save()
                on_ready()
                pending_ready = False
            self.apply(gh, a, issues)
        st.save()
        self.load_tickets(issues)
        self.sync_links(gh, issues)
        self.last_sync = time.time()
        return actions

    def apply(self, gh: GitHub, a, issues: dict) -> None:
        st = self.state
        if isinstance(a, syncplan.Create):
            t = self.tasks[a.key]
            title = f"[{t.change}] {t.id} {t.text}"
            title = title if len(title) <= 120 else title[:117] + "…"
            body = (f"{TASK_MARKER.format(key=t.key)}\n**{t.key}** — {t.text}\n\n"
                    f"Section: {t.section}  \nSpec: `openspec/changes/{t.change}/`\n")
            n = gh.create_issue(title, body, a.status)
            issues[a.key] = {"number": n, "state": "OPEN", "labels": [a.status.label]}
            self.log(f"#{n} created [{a.status.value}] {t.key}")
            if a.status is Status.NEEDS_DECISION:
                self.flag(gh, a.key, n, a.reason)
        elif isinstance(a, syncplan.SetStatus):
            gh.set_status(a.number, a.new, a.old)
            self.log(f"#{a.number} {a.key}: {a.old.value if a.old else '-'} -> {a.new.value}")
            if a.new is Status.NEEDS_DECISION:
                self.flag(gh, a.key, a.number, a.reason)
        elif isinstance(a, syncplan.Close):
            gh.set_status(a.number, Status.FINISHED, a.old)
            gh.comment(a.number, review.bot("Ticked in tasks.md outside the pipeline; closing."))
            gh.close_issue(a.number)
            self.log(f"#{a.number} {a.key} closed (ticked by hand)")
        elif isinstance(a, syncplan.Cleared):
            with st.lock:
                st.data["flagged"].remove(a.key)
                st.data["cleared"].append(a.key)
            self.log(f"{a.key}: decision cleared by a human")

    def load_tickets(self, issues: dict[str, dict]) -> None:
        """Agent-filed tickets become tasks too; they wait in pending approval until a human approves."""
        self.ticket_bodies = {}
        for key, i in issues.items():
            if key.startswith(TICKET_CHANGE + "/") and i["state"].upper() == "OPEN":
                kind = next((l.split(": ", 1)[1] for l in i["labels"] if l.startswith("kind: ")), "ticket")
                self.tasks[key] = Task(TICKET_CHANGE, str(i["number"]), i["title"], False, kind, 0)
                self.ticket_bodies[key] = i["body"]

    def ticket_waits_for(self, key: str) -> list[str]:
        """Unfinished tasks an approved, not yet started ticket needs; logged once per change."""
        if not key.startswith(TICKET_CHANGE + "/") or self.state.task(key).get("runs"):
            return []
        task_keys = {k for k in self.tasks if not k.startswith(TICKET_CHANGE + "/")}
        needs = ticket_blockers(self.ticket_bodies.get(key, ""), task_keys)
        waiting = sorted((k for k in needs if not self.tasks[k].done), key=key_sort)
        st = self.state.task(key)
        if st.get("waits_for") != waiting:
            st["waits_for"] = waiting
            self.state.save()
            if waiting:
                self.log(f"{key} waits for {', '.join(waiting)} to merge")
        return waiting

    def open_tickets(self) -> str:
        titles = [f"#{t.id} {t.text}" for k, t in self.tasks.items() if k.startswith(TICKET_CHANGE + "/")]
        return ("\n" + "\n".join(f"- {x}" for x in titles)) if titles else "none"

    def file_tickets(self, source: str, tickets: list[dict] | None) -> None:
        known = {t.text.strip().lower() for k, t in self.tasks.items() if k.startswith(TICKET_CHANGE + "/")}
        for tk in tickets or []:
            title = tk.get("title", "").strip()[:120]
            if not title or title.lower() in known:
                continue
            kind = tk["kind"] if tk.get("kind") in ("defect", "improvement") else "improvement"
            body = (f"{TICKET_MARKER}\n{tk.get('body', '').strip()}\n\n---\n_Filed by the pipeline from "
                    f"{source}. Approve by setting `{Status.PENDING_IMPLEMENTATION.label}`; close it to reject._\n")
            n = self.gh.create_issue(title, body, Status.PENDING_APPROVAL, (f"kind: {kind}",))
            with self.state.lock:
                self.state.data.setdefault("tickets", []).append(n)
            self.state.save()
            key = f"{TICKET_CHANGE}/{n}"
            self.tasks[key] = Task(TICKET_CHANGE, str(n), title, False, kind, 0)
            self.ticket_bodies[key] = body
            known.add(title.lower())
            self.log(f"#{n} ticket filed ({kind}) from {source}: {title}")

    def spec_ref(self, t: Task) -> str:
        if t.change == TICKET_CHANGE:
            quoted = "\n".join("  > " + l for l in self.ticket_bodies.get(t.key, "").splitlines()
                               if "<!--" not in l)
            return "This task is an approved ticket; its description is the spec:\n\n" + quoted
        return (f"The change: `openspec/changes/{t.change}/` — read proposal.md, design.md and every "
                "spec under specs/ that this task touches. Each spec scenario in scope needs a test.")

    def flag(self, gh: GitHub, key: str, number: int, reason: str) -> None:
        gh.comment(number, review.bot(
            f"**Needs a decision or information.** {reason}\n\n"
            "Answer in a comment on this issue, then set the label to "
            f"`{Status.PENDING_IMPLEMENTATION.label}`. The implementer reads every "
            "non-pipeline comment here."))
        with self.state.lock:
            if key not in self.state.data["flagged"]:
                self.state.data["flagged"].append(key)

    def sync_links(self, gh: GitHub, issues: dict) -> None:
        """Mirror plan edges as GitHub's native blocked-by links (only when they changed)."""
        st = self.state
        todo = []
        for key, issue in issues.items():
            if issue["state"].upper() != "OPEN" or key not in self.tasks:
                continue
            want = sorted(issues[b]["number"] for b in self.plan.get(key).blocked_by if b in issues)
            if st.data["links"].get(key) != want:
                todo.append((key, issue, want))
        if todo:
            self.log(f"linking blocked-by for {len(todo)} issue(s)")
        # Remove every stale link before adding any: a re-plan can reverse an edge
        # (A<-B becomes B<-A), and GitHub rejects an add that would close a cycle.
        current = {}
        for key, issue, want in todo:
            have = gh.blocked_by(issue["number"])
            for n in set(have) - set(want):
                gh.remove_blocked_by(issue["number"], have[n])
            current[key] = have
        for key, issue, want in todo:
            for n in set(want) - set(current[key]):
                try:
                    gh.add_blocked_by(issue["number"], self.db_id(gh, n))
                except proc.CommandError as e:
                    # A link GitHub refuses (e.g. a cycle through a closed issue) only
                    # mirrors the plan; the plan itself still orders the work.
                    self.log(f"#{issue['number']}: could not link blocked-by #{n}: {str(e)[-200:]}")
            st.data["links"][key] = want
            st.save()

    def db_id(self, gh: GitHub, number: int) -> int:
        ids = self.state.data["db_ids"]
        if str(number) not in ids:
            ids[str(number)] = gh.issue_db_id(number)
        return ids[str(number)]

    def issues_by_key(self, issues: list[dict]) -> dict[str, dict]:
        """Pipeline issues belonging to this target, by task key.

        Tickets have no change of their own: a target owns the tickets filed while
        it ran (recorded in state.json).
        """
        changes = set(self.t.changes(self.base_wt))
        mine = set(self.state.data.setdefault("tickets", []))
        out = {}
        for i in issues:
            body = i["body"] or ""
            if TICKET_MARKER in body:
                if i["number"] in mine:
                    out[f"{TICKET_CHANGE}/{i['number']}"] = i
                continue
            key = task_key_from_body(body)
            if key and key.split("/")[0] in changes:
                out[key] = i
        return out

    # ------------------------------------------------------------------ scheduling
    def tick(self) -> None:
        self.reap()
        if self.stopping:
            return
        if time.time() < self.state.data["paused_until"]:
            return
        try:
            self.check_environment()
        except LimitHit as e:
            return self.pause(str(e))
        if time.time() - self.last_sync >= self.cfg.sync_seconds or not self.tasks:
            try:
                self.sync(on_ready=self.schedule)
            except LimitHit as e:
                return self.pause(str(e))
            if self.t.milestones and RELEASE_KEY not in self.jobs:
                self.check_milestones()
        self.schedule()

    def check_environment(self) -> None:
        """Run preflight at start, then once per sync interval while it fails."""
        if self.env_ok or time.time() - self.last_preflight < self.cfg.sync_seconds:
            return
        self.last_preflight = time.time()
        self.refresh_base()
        problems = self.preflight()
        if problems:
            self.log(f"preflight FAILED, starting no agents until it passes (rechecked every "
                     f"{self.cfg.sync_seconds}s):\n" + "\n".join(f"  - {p}" for p in problems))
        else:
            self.env_ok = True
            self.log("preflight passed")

    def preflight(self) -> list[str]:
        """Problems that would make every agent run fail; empty when the environment is fine.

        Each check costs nothing next to an agent run that discovers the same problem the
        hard way: commands from the dispatcher's shell, the same commands through an
        agent's own Bash/PowerShell tools (catches PATH and permission gaps), and
        optionally the gate on the base branch."""
        problems = []
        cwd = self.base_wt / self.t.project_dir
        env = self.env("preflight")
        for c in self.t.preflight:
            r = proc.run(c, cwd, env=env, timeout=600, check=False, shell=True)
            if not r.ok:
                problems.append(f"`{c}` exits {r.code} in the dispatcher's shell: {r.tail(300)}")
        if self.t.preflight:
            agent = self.agent_def("preflight")
            shells = [s for s in ("Bash", "PowerShell") if s in agent.tools]
            prompt = review.prompt("preflight", shells=" and ".join(shells),
                                   commands="\n".join(f"- `{c}`" for c in self.t.preflight))
            res = self.claude.run(prompt, cwd, agent, review.PREFLIGHT_SCHEMA,
                                  log_name=f"preflight-{time.strftime('%Y%m%d-%H%M%S')}", env=env)
            if res.limited:
                raise LimitHit(res.text)
            if res.denials:
                from agentpipe.denials import describe
                problems.append("agent tool calls denied: " + "; ".join(describe(d, 120) for d in res.denials))
            if not res.ok:
                problems.append(f"the preflight agent failed ({res.subtype}): {res.text[:300]}")
            else:
                seen = set()
                for r in res.structured.get("results", []):
                    seen.add((r.get("tool"), r.get("command")))
                    if r.get("exit_code") != 0:
                        problems.append(f"`{r.get('command')}` exits {r.get('exit_code')} in the agent's "
                                        f"{r.get('tool')} tool: {str(r.get('output', ''))[:200]}")
                missing = [f"{s}: {c}" for s in shells for c in self.t.preflight if (s, c) not in seen]
                if missing:
                    problems.append("the preflight agent did not report " + "; ".join(missing))
        if self.t.preflight_gate:
            g = self.run_gate(cwd, "preflight", self.t.test, self.t.test_timeout)
            if not g.ok:
                problems.append(f"the gate `{self.t.test}` fails on {self.base_ref()}: {g.tail(500)}")
        return problems

    def schedule(self) -> None:
        """Fill free implementer, reviewer and merge slots from GitHub's current state."""
        if not self.env_ok:
            return
        issues = {k: i for k, i in self.issues_by_key(self.gh.list_issues()).items()
                  if i["state"].upper() == "OPEN" and k in self.tasks}
        self.issue_map = issues
        prs = {}
        for pr in self.gh.list_prs():
            if key := task_key_from_body(pr["body"]):
                prs[key] = pr
        free_impl = [f"impl-{i}" for i in range(self.cfg.max_implementers)
                     if f"impl-{i}" not in {j.slot for j in self.jobs.values()}]
        free_rev = [f"review-{i}" for i in range(self.cfg.max_reviewers)
                    if f"review-{i}" not in {j.slot for j in self.jobs.values()}]
        merging = any(j.kind == "merge" for j in self.jobs.values())

        def by_status(s: Status) -> list[str]:
            return sorted((k for k, i in issues.items()
                           if status_from_labels(i["labels"]) is s and k not in self.jobs), key=key_sort)

        for s in (Status.PENDING_APPROVAL, Status.CHANGES_REQUESTED, Status.APPROVED):
            for key in by_status(s):
                # A ticket nobody has worked on yet sits in pending approval waiting for a human.
                proposal = (s is Status.PENDING_APPROVAL and key.startswith(TICKET_CHANGE + "/")
                            and not self.state.task(key).get("runs"))
                if key not in prs and not proposal:
                    self.to_decision(issues[key]["number"], s, f"Status is `{s.value}` but no open PR was found.")
        for key in by_status(Status.IN_PROGRESS) + by_status(Status.CHANGES_REQUESTED) + \
                by_status(Status.PENDING_IMPLEMENTATION):
            if not free_impl:
                break
            status = status_from_labels(issues[key]["labels"])
            kind = "revise" if status is Status.CHANGES_REQUESTED else "implement"
            if kind == "revise" and key not in prs:
                continue
            if kind == "implement" and self.ticket_waits_for(key):
                continue
            self.start(kind, key, free_impl.pop(0), issues[key], prs.get(key))
        for key in by_status(Status.PENDING_APPROVAL):
            if not free_rev:
                break
            pr = prs.get(key)
            if pr and self.needs_review(pr):
                self.start("review", key, free_rev.pop(0), issues[key], pr)
        if not merging:
            for key in by_status(Status.APPROVED):
                if key in prs:
                    self.start("merge", key, "merge", issues[key], prs[key])
                    break

    def start(self, kind: str, key: str, slot: str, issue: dict, pr: dict | None) -> None:
        fn = getattr(self, f"job_{kind}")
        self.log(f"start {kind} {key} (#{issue['number']}) on {slot}")
        fut = self.pool.submit(fn, self.tasks[key], issue["number"], pr, slot)
        self.jobs[key] = Job(kind, key, slot, fut)

    def reap(self) -> None:
        for key, job in list(self.jobs.items()):
            if not job.future.done():
                continue
            del self.jobs[key]
            exc = job.future.exception()
            if isinstance(exc, LimitHit):
                self.pause(str(exc))
            elif exc:
                tb = "".join(traceback.format_exception(exc))
                self.log(f"{job.kind} {key} crashed: {exc}")
                self.bump_failure(key, job.kind, tb)
            else:
                self.log(f"done {job.kind} {key}")

    def bump_failure(self, key: str, kind: str, detail: str) -> None:
        st = self.state.task(key)
        st[f"fail_{kind}"] = st.get(f"fail_{kind}", 0) + 1
        self.state.save()
        if st[f"fail_{kind}"] >= 2:
            issues = self.issues_by_key(self.gh.list_issues())
            if i := issues.get(key):
                self.to_decision(i["number"], status_from_labels(i["labels"]),
                                 f"The `{kind}` job failed twice. Last error:\n\n```\n{detail[-3000:]}\n```")
            st[f"fail_{kind}"] = 0
            self.state.save()

    def pause(self, why: str) -> None:
        now = time.time()
        # A known window reset beats a guess; the backoff covers capacity errors with none.
        resets = [w.get("resetsAt", 0) for w in (self.state.data.get("rate") or {}).values()
                  if isinstance(w, dict) and (w.get("status") != "allowed" or w.get("utilization", 0) >= 1)]
        until = max([now + self.cfg.limit_backoff_seconds] + [r + 60 for r in resets if r > now])
        with self.state.lock:
            self.state.data["paused_until"] = max(until, self.state.data["paused_until"])
        self.state.save()
        self.log(f"usage limit hit, pausing until {time.strftime('%H:%M', time.localtime(until))}: {why[:200]}")

    def note_rate(self, windows: dict) -> None:
        """Record the usage windows an agent run reported, and throttle near a limit."""
        now = time.time()
        with self.state.lock:
            self.state.data["rate"] = {**windows, "_at": now}
            for name, cap in self.cfg.throttle.items():
                w = windows.get(name) or {}
                used, resets = float(w.get("utilization") or 0), float(w.get("resetsAt") or 0)
                if used >= cap and resets > now and resets + 60 > self.state.data["paused_until"]:
                    self.state.data["paused_until"] = resets + 60
                    self.log(f"usage {name} at {used:.0%} (throttle {cap:.0%}): starting nothing new "
                             f"until it resets at {time.strftime('%a %H:%M', time.localtime(resets))}; "
                             "running jobs finish")
        self.state.save()

    def needs_review(self, pr: dict) -> bool:
        sha = pr["headRefOid"]
        if self.reviewed.get(pr["number"]) == sha:
            return False
        history = review.review_history(self.gh.comments(pr["number"]))
        if history and history[-1]["sha"] == sha:
            self.reviewed[pr["number"]] = sha
            return False
        return True

    def to_decision(self, number: int, old: Status | None, why: str) -> None:
        self.gh.set_status(number, Status.NEEDS_DECISION, old)
        self.gh.comment(number, review.bot(
            f"**Needs a decision.** {why}\n\nWhen resolved, set the label to the status the task "
            f"should resume from (usually `{Status.PENDING_IMPLEMENTATION.label}`)."))
        self.log(f"#{number} -> needs decision: {why.splitlines()[0][:120]}")

    @property
    def stop_file(self) -> Path:
        """`agentpipe stop` creates this; the dispatcher then drains its jobs and exits."""
        return self.t.work_dir / "stop"

    @property
    def pid_file(self) -> Path:
        return self.t.work_dir / "dispatcher.pid"

    def run_forever(self, once: bool = False) -> None:
        self.stop_file.unlink(missing_ok=True)  # a stale request must not stop a new run
        self.pid_file.write_text(str(os.getpid()), encoding="utf-8")
        self.gh.ensure_labels()
        self.log(f"running (pid {os.getpid()}); implementers={self.cfg.max_implementers} "
                 f"reviewers={self.cfg.max_reviewers}")
        try:
            while True:
                if not self.stopping and self.stop_file.exists():
                    self.stopping = True
                    self.log(f"stop requested: starting nothing new, waiting for {len(self.jobs)} running job(s)")
                self.tick()
                if (once or self.stopping) and not self.jobs:
                    break
                # Sleep in short steps so a stop request is noticed within seconds.
                deadline = time.time() + (self.cfg.poll_seconds if not once else 5)
                while time.time() < deadline and (self.stopping or not self.stop_file.exists()):
                    time.sleep(min(2.0, max(deadline - time.time(), 0)))
            self.log("stopped")
        except KeyboardInterrupt:
            self.stopping = True
            self.log("stopping: waiting for running agents (Ctrl-C again to abandon; work resumes on restart)")
            self.pool.shutdown(wait=True, cancel_futures=True)
        except Exception as e:
            # Hidden runs have no console, so the traceback must reach pipeline.log.
            self.log(f"dispatcher crashed: {e!r}\n" + traceback.format_exc())
            raise
        finally:
            self.stop_file.unlink(missing_ok=True)
            self.pid_file.unlink(missing_ok=True)

    # ------------------------------------------------------------------ jobs
    def human_input(self, issue: int, pr: dict | None) -> str:
        notes = review.human_comments(self.gh.comments(issue))
        if pr:
            notes += review.human_comments(self.gh.comments(pr["number"]))
        if not notes:
            return ""
        body = "\n\n".join(f"**{c['user']}** ({c['created_at']}):\n{c['body']}" for c in notes)
        return "## Input from humans (authoritative; it overrides the spec where they conflict)\n\n" + body

    # ------------------------------------------------------------------ batches
    def batch(self, t: Task) -> list[Task]:
        """The tasks ``t``'s job covers, leader first: fixed when its first run starts, so a
        re-plan midway can't change what the open PR is for."""
        keys = self.state.task(t.key).get("batch") or [t.key]
        return [self.tasks[k] for k in keys if k in self.tasks] or [t]

    def record_batch(self, t: Task) -> None:
        st = self.state.task(t.key)
        if "batch" in st or not self.plan:
            return
        done = {k for k, x in self.tasks.items() if x.done}
        members = [t.key]
        for k in self.plan.batch_of(t.key, done)[1:]:
            i = self.issue_map.get(k)
            # Only followers nobody else is working on: one already claimed keeps its own job.
            if i and status_from_labels(i["labels"]) in GRAPH_OWNED and k not in self.jobs:
                members.append(k)
        st["batch"] = members
        st["batch_issues"] = {k: self.issue_map[k]["number"] for k in members[1:]}
        self.state.save()
        if len(members) > 1:
            issue = self.issue_map.get(t.key, {}).get("number")
            for k in members[1:]:
                self.gh.comment(st["batch_issues"][k], review.bot(
                    f"Batched: done together with **{t.key}** (#{issue}) in one PR; this issue closes when it merges."))
            self.log(f"{t.key}: batch of {len(members)}: {', '.join(members)}")

    def batch_section(self, t: Task) -> str:
        tasks = self.batch(t)
        if len(tasks) == 1:
            return ""
        nums = self.state.task(t.key).get("batch_issues", {})
        rows = "\n".join(f"- **{x.key}** — {x.text} (section: {x.section}"
                         + (f"; issue #{nums[x.key]})" if x.key in nums else ")") for x in tasks)
        return ("**This is a batch.** These small tasks are done together, on one branch, in one PR, "
                "and reviewed as one; the task above is the first of them. All of them must be "
                "complete:\n\n" + rows + "\n")

    # ------------------------------------------------------------------ sessions
    def resumable(self, t: Task) -> str | None:
        """The agent session to resume, or None. An old session would be re-cached in full
        and carries stale context, so past resume_within_seconds a run starts fresh."""
        st = self.state.task(t.key)
        s = st.get("session")
        if s and time.time() - st.get("session_at", 0) <= self.cfg.resume_within_seconds:
            return s
        return None

    def prior_work(self, t: Task, pr: dict | None) -> str:
        """For a fresh session on a task with history: what is already there."""
        if self.resumable(t):
            return ""
        wt = self.wt(t)
        exists = (wt / ".git").exists()
        ahead = self.git.commits_ahead(wt, self.base_ref()) if exists else 0
        dirty = exists and not self.git.is_clean(wt)
        if not (pr or ahead or dirty):
            return ""
        parts = ["## Earlier work on this task\n\nAn earlier session worked on this branch; continue "
                 "from it instead of starting over."]
        if ahead:
            parts.append(f"It has {ahead} commit(s) on top of `{self.base_ref()}` "
                         f"(`git log {self.base_ref()}..HEAD`).")
        if dirty:
            parts.append("The worktree also holds uncommitted changes from an interrupted run "
                         "(`git status`): keep what belongs to the task.")
        if pr:
            parts.append(f"Its PR is #{pr['number']}; its description:\n\n"
                         + pr["body"].split("## Summary", 1)[-1].strip()[:3000])
        return "\n\n".join(parts) + "\n"

    def impl_prompt(self, t: Task, issue: int, pr: dict | None) -> str:
        tasks = self.batch(t)
        inside = {x.key for x in tasks}
        blockers = sorted({b for x in tasks for b in (self.plan.get(x.key).blocked_by if self.plan else [])
                           if b not in inside}, key=key_sort)
        return review.prompt(
            "implementer", key=t.key, text=t.text, section=t.section, issue=issue,
            blockers=", ".join(blockers) or "none", worktree=self.wt(t), project_dir=self.t.project_dir,
            spec_ref=self.spec_ref(t), context_list=self.context_list(),
            human_input=self.human_input(issue, pr), test_cmd=self.t.test, open_tickets=self.open_tickets(),
            batch_section=self.batch_section(t), prior_work=self.prior_work(t, pr))

    def ensure_worktree(self, t: Task) -> Path:
        wt = self.wt(t)
        if not (wt / ".git").exists():
            self.git.fetch()
            start = f"origin/{t.branch}" if self.git.remote_branch_exists(t.branch) else self.base_ref()
            self.git.worktree_add(wt, t.branch, start)
            self.state.task(t.key).pop("session", None)
        return wt

    def agent(self, t: Task, prompt: str, slot: str, tag: str) -> ClaudeResult:
        st = self.state.task(t.key)
        n = st.get("runs", 0) + 1
        st["runs"] = n
        self.clean_workspace_crates(self.wt(t) / self.t.project_dir, slot)
        res = self.claude.run(prompt, self.wt(t) / self.t.project_dir, self.agent_def("implementer"),
                              review.IMPLEMENTER_SCHEMA, log_name=f"{t.slug}-{tag}-{n}",
                              resume=self.resumable(t), env=self.env(slot))
        if res.session_id:
            st["session"] = res.session_id
            st["session_at"] = time.time()
        self.state.save()
        if res.limited:
            raise LimitHit(res.text)
        return res

    def job_implement(self, t: Task, issue: int, pr: dict | None, slot: str) -> None:
        old = status_from_labels(self.gh_issue_labels(issue))
        self.ensure_worktree(t)
        self.record_batch(t)
        if old is not Status.IN_PROGRESS:
            self.gh.set_status(issue, Status.IN_PROGRESS, old)
        full = self.impl_prompt(t, issue, pr)
        if self.resumable(t):
            prompt = ("You were interrupted, or a human answered your question. Continue the task "
                      "and finish with the structured output. Your full instructions, with any new "
                      "human input, follow.\n\n" + full)
        else:
            prompt = full
        res = self.agent(t, prompt, slot, "impl")
        self.deliver(t, issue, pr, res, slot, Status.IN_PROGRESS)

    def job_revise(self, t: Task, issue: int, pr: dict, slot: str) -> None:
        wt = self.ensure_worktree(t)
        comments = self.gh.comments(pr["number"])
        history = review.review_history(comments)
        last = history[-1] if history else None
        merge_note = ""
        if last and last["verdict"] == review.CONFLICT:
            self.git.fetch()
            if not self.git.merge(wt, self.base_ref()):
                merge_note = (f"`{self.base_ref()}` has been merged into your branch and has conflicts. "
                              "Resolve them keeping both sides' intent, run the tests, and commit the merge.")
        prompt = review.prompt(
            "revise", key=t.key, round=review.rounds_used(history), max_rounds=self.cfg.max_rounds,
            feedback=last["body"] if last else "(no review found)", merge_note=merge_note,
            test_cmd=self.t.test)
        if not self.resumable(t):
            prompt = self.impl_prompt(t, issue, pr) + "\n\n---\n\n" + prompt
        else:
            prompt += "\n\n" + self.human_input(issue, pr)
        res = self.agent(t, prompt, slot, "revise")
        self.deliver(t, issue, pr, res, slot, Status.CHANGES_REQUESTED)

    def capped(self, t: Task, issue: int, cur: Status, res: ClaudeResult, role: str) -> None:
        """A run that hit its turn cap or timeout: stop instead of paying for it again."""
        st = self.state.task(t.key)
        st.pop("session", None)  # a requeue starts fresh, from what is committed
        self.state.save()
        a = self.agent_def(role)
        what = (f"its {a.max_turns}-turn cap" if res.subtype == MAX_TURNS
                else f"the {self.cfg.agent_timeout // 60}-minute timeout")
        self.to_decision(issue, cur, (
            f"The {role} hit {what} without finishing. Rather than spend another run like it: split the "
            f"task in tasks.md if it is too big for one session, or requeue it to continue in a fresh "
            f"session from what is committed."))

    def deliver(self, t: Task, issue: int, pr: dict | None, res: ClaudeResult, slot: str,
                cur: Status) -> None:
        """After an implementer turn: check the result, sync with base, push, open the PR."""
        wt = self.wt(t)
        if res.subtype in CAPPED:
            return self.capped(t, issue, cur, res, "implementer")
        if not res.ok:
            raise RuntimeError(f"implementer run failed ({res.subtype}): {res.text[:1000]}")
        out = res.structured
        self.file_tickets(f"the implementer of {t.key} (#{issue})", out.get("tickets"))
        if out["status"] == "needs_decision":
            self.to_decision(issue, cur, out.get("question") or out["summary"])
            return
        if not self.git.is_clean(wt) or self.git.merging(wt):
            res = self.agent(t, "The worktree has uncommitted changes or an unfinished merge. Commit what "
                                "belongs to the task, discard the rest, and return the structured output.",
                             slot, "cleanup")
            if not res.ok or not self.git.is_clean(wt) or self.git.merging(wt):
                self.to_decision(issue, cur, "The implementer left the worktree dirty twice.")
                return
            out = res.structured
        if self.git.commits_ahead(wt, self.base_ref()) == 0:
            self.to_decision(issue, cur, "The implementer reported done but committed nothing.\n\n" + out["summary"])
            return
        self.git.fetch()
        if not self.git.contains(wt, self.base_ref()) and not self.git.merge(wt, self.base_ref()):
            res = self.agent(t, f"`{self.base_ref()}` was merged into your branch and conflicts. Resolve "
                                "the conflicts keeping both sides' intent, run the tests, commit the merge, "
                                "and return the structured output.", slot, "conflict")
            if not res.ok or self.git.merging(wt) or not self.git.is_clean(wt):
                self.git.merge_abort(wt)
                self.to_decision(issue, cur, "Could not resolve merge conflicts with the base branch.")
                return
        self.git.push(wt, t.branch)
        tasks = self.batch(t)
        nums = self.state.task(t.key).get("batch_issues", {})
        closes = "\n".join([f"Closes #{issue}"] + [f"Closes #{nums[x.key]}" for x in tasks[1:] if x.key in nums])
        listing = "\n".join(f"**{x.key}** — {x.text}" for x in tasks)
        body = (f"{TASK_MARKER.format(key=t.key)}\n{closes}\n\n{listing}\n\n"
                f"## Summary\n\n{out['summary']}\n\n## Tests\n\n{out['tests']}\n")
        if pr is None:
            title = (f"{t.key}: {t.text}" if len(tasks) == 1 else
                     f"{t.change} {', '.join(x.id for x in tasks)}: {t.text}")
            n = self.gh.create_pr(t.branch, self.t.base, title[:120], body)
            self.log(f"PR #{n} opened for {t.key}" + (f" (batch of {len(tasks)})" if len(tasks) > 1 else ""))
        else:
            self.gh.comment(pr["number"], review.bot(f"**Revised.**\n\n{out['summary']}\n\n**Tests:** {out['tests']}"))
        self.gh.set_status(issue, Status.PENDING_APPROVAL, cur)
        st = self.state.task(t.key)
        st.pop("fail_implement", None)
        st.pop("fail_revise", None)
        self.state.save()

    def run_gate(self, cwd: Path, slot: str, cmd: str, timeout: int) -> proc.Result:
        self.clean_workspace_crates(cwd, slot)
        return proc.run(cmd, cwd, env=self.env(slot), timeout=timeout, check=False, shell=True)

    def clean_workspace_crates(self, cwd: Path, slot: str) -> None:
        """Drop the workspace's own crates from the slot's shared target dir (see config)."""
        if not self.t.clean_workspace_crates or not (cwd / "Cargo.toml").exists():
            return
        env = self.env(slot)
        try:
            meta = json.loads(proc.run(["cargo", "metadata", "--no-deps", "--format-version", "1"],
                                       cwd, env=env, timeout=300).out)
            names = sorted({p["name"] for p in meta.get("packages", [])})
            if names:
                proc.run(["cargo", "clean", *[a for n in names for a in ("-p", n)]],
                         cwd, env=env, timeout=300)
        except (proc.CommandError, OSError, ValueError, KeyError) as e:
            self.log(f"could not clean workspace crates in {slot}: {str(e)[-200:]}")

    def job_review(self, t: Task, issue: int, pr: dict, slot: str) -> None:
        sha = pr["headRefOid"]
        rv = self.t.work_dir / "review" / t.slug
        self.git.fetch()
        self.git.worktree_detached(rv, sha)
        try:
            pdir = rv / self.t.project_dir
            history = review.review_history(self.gh.comments(pr["number"]))
            round_ = review.rounds_used(history) + 1
            self.log(f"phase {t.key}: test gate")
            gate = self.run_gate(pdir, slot, self.t.test, self.t.test_timeout)
            if not gate.ok:
                v = {"verdict": review.TESTS_FAILED, "summary": f"`{self.t.test}` failed on this commit.",
                     "findings": [], "probes": [], "details": gate.tail(6000)}
            else:
                try:
                    v = self.reviewer_verdict(t, pr, rv, pdir, slot, history)
                except CappedRun as e:
                    return self.capped(t, issue, Status.PENDING_APPROVAL, e.result, "reviewer")
            self.gh.comment(pr["number"], review.render_review(v, sha, round_, self.cfg.max_rounds))
            self.reviewed[pr["number"]] = sha
            if v["verdict"] == review.APPROVE:
                self.gh.set_status(issue, Status.APPROVED, Status.PENDING_APPROVAL)
            elif round_ >= self.cfg.max_rounds:
                self.to_decision(issue, Status.PENDING_APPROVAL,
                                 f"The reviewer rejected PR #{pr['number']} {round_} times; see the PR.")
            else:
                self.gh.set_status(issue, Status.CHANGES_REQUESTED, Status.PENDING_APPROVAL)
            self.log(f"review {t.key} PR #{pr['number']}: {v['verdict']} (round {round_})")
        finally:
            self.git.worktree_remove(rv)

    def reviewer_verdict(self, t: Task, pr: dict, rv: Path, pdir: Path, slot: str, history) -> dict:
        mutation_section = ""
        if self.t.mutation:
            diff_path = rv.parent / f"{t.slug}.diff"
            # Paths relative to the project dir, which is where the mutation tool runs.
            diff_path.write_text(self.git.diff(pdir, self.base_ref(), relative=True), encoding="utf-8")
            self.log(f"phase {t.key}: mutation testing")
            m = self.run_gate(pdir, slot, self.t.mutation.replace("{diff}", str(diff_path)),
                              self.t.mutation_timeout)
            self.git.restore(rv)  # an in-place run killed mid-mutant must not leave a mutated file
            mutation_section = (
                "Mutation testing on the changed lines (`" + self.t.mutation + "`). Lines starting "
                "`MISSED` are mutants no test caught: each is a candidate coverage gap to judge, "
                "and a real one is a blocking test-coverage finding.\n\n```\n" + m.tail(5000) + "\n```")
        counted = [h for h in history if h["verdict"] in review.COUNTED]
        prior = ""
        if counted:
            prior = ("## Your previous review (check each blocking finding first)\n\n" + counted[-1]["body"]
                     + "\n\nLatest replies on the PR:\n\n" + "\n\n".join(
                         c["body"] for c in self.gh.comments(pr["number"])[-3:]))
        prompt = review.prompt(
            "reviewer", pr=pr["number"], key=t.key, text=t.text, spec_ref=self.spec_ref(t),
            batch_section=self.batch_section(t),
            open_tickets=self.open_tickets(),
            head=pr["headRefOid"][:10], project_dir=self.t.project_dir, base_ref=self.base_ref(),
            diffstat=self.git.diff(rv, self.base_ref(), stat=True).strip()[-4000:],
            pr_summary=pr["body"].split("## Summary", 1)[-1].strip()[:6000],
            test_cmd=self.t.test, mutation_section=mutation_section, prior_section=prior,
            context_list=self.context_list())
        self.log(f"phase {t.key}: agent (reviewer)")
        for attempt in range(2):
            res = self.claude.run(prompt, pdir, self.agent_def("reviewer"), review.REVIEW_SCHEMA,
                                  log_name=f"{t.slug}-review-{pr['headRefOid'][:8]}-{attempt}",
                                  env=self.env(slot))
            if res.limited:
                raise LimitHit(res.text)
            if res.subtype in CAPPED:
                raise CappedRun(res)
            if res.ok:
                self.file_tickets(f"the review of PR #{pr['number']} ({t.key})", res.structured.get("tickets"))
                return review.normalize_verdict(res.structured)
            self.git.git("checkout", "--force", "--detach", pr["headRefOid"], cwd=rv)  # undo stray probes
        raise RuntimeError(f"reviewer failed twice ({res.subtype}): {res.text[:1000]}")

    def job_merge(self, t: Task, issue: int, pr: dict, slot: str) -> None:
        wt = self.ensure_worktree(t)
        self.git.fetch()
        self.git.git("checkout", "--force", "-B", t.branch, f"origin/{t.branch}", cwd=wt)
        if self.git.head(wt) != pr["headRefOid"]:
            raise RuntimeError("PR head moved after approval; refusing to merge")
        # A branch that fell behind is brought up to date and retried, never handed to the
        # owner: the base can move again between our push and GitHub's merge, and GitHub
        # reports "not mergeable" while it is still computing mergeability after a push.
        for attempt in range(MERGE_ATTEMPTS):
            if attempt:
                time.sleep(MERGE_RETRY_SECONDS)
                self.git.fetch()
            if not self.git.contains(wt, self.base_ref()):
                if not self.git.merge(wt, self.base_ref()):
                    self.git.merge_abort(wt)
                    return self.merge_conflict(issue, pr, f"The approved branch no longer merges cleanly "
                                                          f"with `{self.base_ref()}`.")
                self.log(f"phase {t.key}: test gate (after merging {self.base_ref()})")
                gate = self.run_gate(wt / self.t.project_dir, slot, self.t.test, self.t.test_timeout)
                if not gate.ok:
                    history = review.review_history(self.gh.comments(pr["number"]))
                    round_ = review.rounds_used(history) + 1
                    v = {"verdict": review.TESTS_FAILED, "findings": [], "probes": [], "details": gate.tail(6000),
                         "summary": f"After merging `{self.base_ref()}`, `{self.t.test}` fails."}
                    self.git.git("reset", "--hard", pr["headRefOid"], cwd=wt)
                    self.gh.comment(pr["number"], review.render_review(v, pr["headRefOid"], round_, self.cfg.max_rounds))
                    self.gh.set_status(issue, Status.CHANGES_REQUESTED, Status.APPROVED)
                    return
                self.git.push(wt, t.branch)
            try:
                self.gh.merge_pr(pr["number"], self.git.head(wt))
                break
            except proc.CommandError as e:
                if "not mergeable" not in str(e).lower():
                    raise
                self.log(f"merge {t.key} PR #{pr['number']}: not mergeable yet (attempt {attempt + 1}); retrying")
        else:
            return self.merge_conflict(issue, pr, f"GitHub still reports the branch as not mergeable after "
                                                  f"{MERGE_ATTEMPTS} attempts to bring it up to date with "
                                                  f"`{self.base_ref()}`; rebase it onto `{self.base_ref()}` "
                                                  f"and resolve the conflicts.")
        tasks = self.batch(t)
        self.log(f"merged PR #{pr['number']} ({', '.join(x.key for x in tasks)})")
        self.gh.set_status(issue, Status.FINISHED, Status.APPROVED)
        self.gh.close_issue(issue)  # "Closes #n" normally does this; harmless if already closed
        for k, n in self.state.task(t.key).get("batch_issues", {}).items():
            self.gh.set_status(n, Status.FINISHED, Status.BLOCKED_BY_TASK)
            self.gh.close_issue(n)
        if t.change != TICKET_CHANGE:
            self.commit_to_base(tasks, f"Tick {', '.join(x.key for x in tasks)} in tasks.md")
        self.git.worktree_remove(wt)
        self.git.delete_branch(t.branch)
        self.git.delete_remote_branch(t.branch)
        with self.state.lock:
            for x in tasks:
                self.state.data["tasks"].pop(x.key, None)
        self.state.save()
        self.last_sync = 0.0  # dependents may be unblocked now

    def merge_conflict(self, issue: int, pr: dict, summary: str) -> None:
        """Send an approved PR that cannot be merged back to its implementer to update it."""
        v = {"verdict": review.CONFLICT, "findings": [], "probes": [], "summary": summary}
        self.gh.comment(pr["number"], review.render_review(v, pr["headRefOid"], 0, self.cfg.max_rounds))
        self.gh.set_status(issue, Status.CHANGES_REQUESTED, Status.APPROVED)

    def commit_to_base(self, tasks: list[Task], message: str) -> None:
        """Tick ``tasks`` (all of one change: a batch never mixes changes) in one commit."""
        t = tasks[0]
        rel = self.t.changes_dir / t.change / "tasks.md"
        for _ in range(3):
            self.refresh_base()
            path = self.base_wt / rel
            text = path.read_text(encoding="utf-8")
            new = text
            for x in tasks:
                new = mark_done(new, x.id)
            if new == text:
                return
            path.write_text(new, encoding="utf-8")
            self.git.git("add", str(rel).replace("\\", "/"), cwd=self.base_wt)
            self.git.git("commit", "-m", message, cwd=self.base_wt)
            if self.git.git("push", "origin", f"HEAD:refs/heads/{self.t.base}", cwd=self.base_wt, check=False).ok:
                return
        self.log(f"could not push the tick for {t.key}; the next sync will retry via Close")

    # ------------------------------------------------------------------ milestones
    def release_state(self, m: Milestone) -> dict:
        with self.state.lock:
            return self.state.data.setdefault("releases", {}).setdefault(m.tag, {"tried": []})

    def check_milestones(self) -> None:
        """Start a release when the next milestone's changes are all done on the base branch.

        It is cut from the commit that ticked the milestone's last task, so later work that
        landed afterwards stays out. If that fails, the base branch head is tried once it moves.
        """
        m = milestones.next_due(self.t.milestones, self.git.remote_tags(), self.base_wt)
        if m is None:
            return
        cut = self.git.git("log", "-1", "--format=%H", self.base_ref(), "--",
                           *milestones.task_paths(m)).out.strip()
        head = self.git.git("rev-parse", self.base_ref()).out.strip()
        rs = self.release_state(m)
        sha = next((s for s in (cut, head) if s and s not in rs["tried"]), None)
        if sha is None:
            return
        self.log(f"milestone {m.tag} complete; releasing {sha[:8]} to {self.t.release_branch}")
        fut = self.pool.submit(self.job_release, m, sha)
        self.jobs[RELEASE_KEY] = Job("release", RELEASE_KEY, RELEASE_KEY, fut)

    def job_release(self, m: Milestone, sha: str) -> None:
        rs = self.release_state(m)
        rs["tried"].append(sha)
        self.state.save()
        wt = self.t.work_dir / RELEASE_KEY
        rel = self.t.release_branch
        try:
            self.git.git("fetch", "origin", "--prune", "--tags", "--force")
            self.git.worktree_detached(wt, f"origin/{rel}")
            msg = f"Release {m.title} ({m.tag})"
            if not self.git.git("merge", "--no-ff", "-m", msg, sha, cwd=wt, check=False).ok:
                self.git.merge_abort(wt)
                return self.release_failed(m, sha, f"`{self.t.base}` at {sha[:8]} does not merge cleanly "
                                           f"into `{rel}`. Merge `{rel}` into `{self.t.base}` to resolve it.")
            pdir = wt / self.t.project_dir
            gate = self.run_gate(pdir, RELEASE_KEY, self.t.test, self.t.test_timeout)
            if not gate.ok:
                return self.release_failed(m, sha, f"`{self.t.test}` fails on the merge:" + fence(gate.tail(4000)))
            assets: list[str] = []
            if self.t.release_build:
                b = self.run_gate(pdir, RELEASE_KEY, self.t.release_build, self.t.test_timeout)
                if not b.ok:
                    return self.release_failed(m, sha, f"`{self.t.release_build}` fails:" + fence(b.tail(4000)))
            for g in self.t.release_assets:
                assets += sorted(str(p) for p in pdir.glob(g) if p.is_file())
            if self.t.release_assets and not assets:
                return self.release_failed(m, sha, f"No file matched `release_assets` {self.t.release_assets}.")
            i = self.t.milestones.index(m)
            prev = self.t.milestones[i - 1].tag if i else None
            log = self.git.git("log", "--oneline", "--no-merges", "--invert-grep", "--grep=^Tick ",
                               "-200", f"{prev}..{sha}" if prev else sha, cwd=wt).out
            self.git.git("push", "origin", f"HEAD:refs/heads/{rel}", cwd=wt)
            self.git.git("tag", "-a", "-f", m.tag, "-m", msg, cwd=wt)
            self.git.git("push", "origin", f"refs/tags/{m.tag}", cwd=wt)
            self.gh.create_release(m.tag, m.title, milestones.notes(m, self.t.base, prev, log), assets)
            rs["released"] = sha
            self.state.save()
            self.log(f"released {m.tag} on {rel} ({len(assets)} asset(s))")
        except Exception as e:  # noqa: BLE001 - every failure must reach a human
            self.release_failed(m, sha, "The release job crashed:" +
                                fence("".join(traceback.format_exception(e))[-3000:]))
        finally:
            self.git.worktree_remove(wt)

    def release_failed(self, m: Milestone, sha: str, why: str) -> None:
        rs = self.release_state(m)
        text = (f"Releasing **{m.title}** (`{m.tag}`) from {sha[:8]} failed. {why}\n\n"
                f"The pipeline retries from the head of `{self.t.base}` once it moves.")
        if rs.get("issue"):
            self.gh.comment(rs["issue"], review.bot(text))
        else:
            rs["issue"] = self.gh.create_issue(f"Milestone {m.tag} release blocked", text,
                                               Status.NEEDS_DECISION)
        self.state.save()
        self.log(f"release {m.tag} failed: {why.splitlines()[0][:120]}")

    def gh_issue_labels(self, number: int) -> list[str]:
        for i in self.gh.list_issues():
            if i["number"] == number:
                return i["labels"]
        return []


class LimitHit(RuntimeError):
    """Claude reported a usage or capacity limit; the dispatcher pauses."""
