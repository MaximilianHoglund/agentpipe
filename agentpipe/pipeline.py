"""The dispatcher: sync tasks to GitHub, then run implement/revise/review/merge jobs.

Every decision is re-derived from GitHub labels and PR comments on each tick, so the
process can be killed and restarted at any point. Local state (state.json) only
holds what GitHub can't: agent session ids, retry counters, and caches.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from agentpipe import milestones, proc, review, service, syncplan
from agentpipe.claude import CAPPED, MAX_TURNS, Claude, ClaudeResult
from agentpipe.config import Agent, Config, Milestone, Target
from agentpipe import conflicts
from agentpipe.deps import (PLAN_SCHEMA, TIER_SCHEMA, TOUCHES_SCHEMA, TRIAGE_SCHEMA, Plan, PlanError, clean_tier, clean_touches, key_sort,
                            merge_plans, stale_keys, text_hash, validate_plan)
from agentpipe.git import Git
from agentpipe.github import GitHub
from agentpipe.status import (GRAPH_OWNED, TASK_MARKER, TICKET_CHANGE, TICKET_MARKER, Status,
                              status_from_labels, task_key_from_body)
from agentpipe.tasks import Task, fingerprint, mark_done, parse_tasks
from agentpipe.tickets import filed_from, ticket_blockers

IMPL_KINDS = {"implement", "revise"}
# Implementer runs that go to the light model whatever the task's tier: resolving a merge and
# committing a dirty tree are narrow, and the gate checks the result.
LIGHT_RUNS = {"conflict", "cleanup"}
RELEASE_KEY = "milestone"  # job key and slot of the milestone release job
MUTATION_FIXES = 2  # implementer runs on missed mutants before the PR goes to review anyway
MERGE_ATTEMPTS = 3  # tries to bring a behind branch up to date and merge it
MERGE_RETRY_SECONDS = 15  # lets GitHub finish computing mergeability after a push


VERDICT_WORDS = {review.APPROVE: "APPROVED", review.REQUEST_CHANGES: "REJECTED, changes requested",
                 review.TESTS_FAILED: "REJECTED, test gate failed",
                 review.CONFLICT: "REJECTED, conflicts with the base branch"}


def review_line(what: str, pr: int, v: dict, round_: int, max_rounds: int) -> str:
    """The dispatcher's one line per review: the outcome in words, what blocks, and why."""
    blocking = sum(1 for f in v["findings"] if f.get("severity") == "blocking")
    nits = len(v["findings"]) - blocking
    counts = ", ".join(f"{n} {label}" for n, label in ((blocking, "blocking"), (nits, "nit")) if n)
    if v["verdict"] == review.APPROVE:
        nxt = "to merge"
    elif v["verdict"] in review.COUNTED and round_ >= max_rounds:
        nxt = "out of rounds, needs a decision"
    else:
        nxt = "back to the implementer"
    summary = " ".join(v.get("summary", "").split())
    summary = summary if len(summary) <= 140 else summary[:137].rstrip() + "..."
    return (f"review {what} PR #{pr}: {VERDICT_WORDS.get(v['verdict'], v['verdict'])}"
            f" (round {round_}/{max_rounds}{', ' + counts if counts else ''}); {nxt}"
            + (f". {summary}" if summary else ""))


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


def clear_pause(state: State) -> None:
    """Drop a usage pause and the usage it was based on. A paused dispatcher runs no agents, so
    it never learns that limits changed (a plan upgrade); the next run reports fresh usage and
    re-pauses if it is still near a limit."""
    with state.lock:
        state.data["paused_until"] = 0.0
        state.data.pop("rate", None)
    state.save()


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
        self.merge_order: list[tuple[Task, int, dict]] = []  # approved PRs the next merge job may batch
        # Batch members a merge job is landing: their PR may already be merged (no longer open)
        # before finish_merge relabels them, so the sync mustn't call them stuck.
        self.merging: set[str] = set()
        # The base worktree is shared by the sync loop and merge jobs (ticks, archives); two
        # git commands in it at once collide on its index.lock.
        self.base_lock = threading.RLock()
        self.plan: Plan | None = None
        self.last_sync = 0.0
        self.jobs: dict[str, Job] = {}
        self.reviewed: dict[int, str] = {}  # PR number -> head sha already reviewed
        self.review_at: dict[int, str] = {}  # PR number -> when that review was posted (ISO UTC)
        self.touches_tried: set[str] = set()  # task keys sent to backfill_touches this run
        self.tiers_tried: set[str] = set()  # ticket keys sent to rate_tickets this run
        self.held: dict[str, str] = {}  # task key -> why it waits for another's files (logged once)
        # Threads start lazily, so a generous cap costs nothing and lets a config reload
        # raise the slot counts without replacing the pool.
        self.pool = ThreadPoolExecutor(max_workers=64)
        self.stopping = False
        # Set by `agentpipe run` so edits to pipeline.toml apply without a restart.
        self.config_path: Path | None = None
        self.overrides: dict[str, object] = {}  # command-line settings that beat the file
        self.config_mtime = 0.0

    # Settings read once at startup; changing them in the file still needs a restart.
    RESTART_ONLY = ("claude_bin", "gh_bin", "agent_timeout", "mcp_servers")
    RESTART_ONLY_TARGET = ("repo_root", "github_repo", "work_dir")

    def watch_config(self, path: Path, overrides: dict[str, object]) -> None:
        self.config_path, self.overrides = path, overrides
        self.config_mtime = path.stat().st_mtime
        for k, v in overrides.items():
            setattr(self.cfg, k, v)

    def reload_config(self) -> None:
        """Apply pipeline.toml edits on the fly; a broken file keeps the current settings."""
        if self.config_path is None:
            return
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            return
        if mtime == self.config_mtime:
            return
        self.config_mtime = mtime
        from agentpipe import config as config_mod
        try:
            new = config_mod.load(self.config_path)
            target = new.targets[self.t.name]
        except Exception as e:
            self.log(f"config reload failed, keeping current settings: {e!r}")
            return
        for k, v in self.overrides.items():
            setattr(new, k, v)
        old, old_t = self.cfg, self.t
        stale = [k for k in self.RESTART_ONLY if getattr(new, k) != getattr(old, k)]
        stale += [f"targets.{self.t.name}.{k}" for k in self.RESTART_ONLY_TARGET
                  if getattr(target, k) != getattr(old_t, k)]
        changed = [f"{k}={getattr(new, k)!r}" for k in ("max_implementers", "max_reviewers", "max_rounds",
                                                         "poll_seconds", "sync_seconds", "throttle",
                                                         "limit_backoff_seconds", "max_batch")
                   if getattr(new, k) != getattr(old, k)]
        if new.agents != old.agents:
            changed.append("agents")
        if target != old_t:
            changed.append(f"targets.{self.t.name}")
        for k in self.RESTART_ONLY:
            setattr(new, k, getattr(old, k))
        for k in self.RESTART_ONLY_TARGET:
            setattr(target, k, getattr(old_t, k))
        self.cfg, self.t = new, target
        if changed:
            self.log("config reloaded: " + ", ".join(changed))
        if stale:
            self.log("config: these take effect only after a restart: " + ", ".join(stale))

    def log(self, msg: str) -> None:
        line = time.strftime("%H:%M:%S ") + f"[{self.t.name}] " + msg
        self._log(line)
        self.logs.mkdir(parents=True, exist_ok=True)
        with open(self.logs / "pipeline.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d ") + line + "\n")

    def about(self, key: str, width: int = 60) -> str:
        """A task's text, shortened for a log line, so a key or issue number says what it is."""
        t = self.tasks.get(key)
        text = " ".join(t.text.split()) if t else ""
        return text if len(text) <= width else text[:width - 3].rstrip() + "..."

    def what(self, key: str) -> str:
        """`key "short description"`, or the bare key for a task the dispatcher doesn't know."""
        about = self.about(key)
        return f'{key} "{about}"' if about else key

    def issue_ref(self, number: int) -> str:
        """`#n key "short description"` for an open task issue, else just `#n`."""
        key = next((k for k, i in self.issue_map.items() if i["number"] == number), None)
        return f"#{number} {self.what(key)}" if key else f"#{number}"

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
        return replace(a, tools=a.tools + self.t.extra_tools)

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
        with self.base_lock:
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

    def backfill_touches(self, tasks: list[Task], plan: Plan) -> bool:
        """Predict `touches` and `tier` for open tasks planned before the planner gave them
        (one run, on the planner's light model).

        A failed run is logged and not retried until a restart: without a prediction a task
        is simply scheduled as before, and runs as `standard`. Returns True if the plan changed."""
        need = [t for t in tasks if not t.done and t.key in plan.tasks and plan.tasks[t.key].autonomous
                and (plan.tasks[t.key].touches is None or plan.tasks[t.key].tier is None)
                and t.key not in self.touches_tried]
        if not need:
            return False
        self.touches_tried |= {t.key for t in need}
        self.log(f"predicting the files and tier of {len(need)} planned task(s)")
        prompt = review.prompt(
            "touches", repo_root=self.base_wt, target=self.t.name, project_dir=self.t.project_dir,
            tasks="\n".join(f"- {t.key} | {t.section} | {t.text}" for t in need))
        planner = self.cfg.agents["planner"]
        res = self.claude.run(prompt, self.base_wt, replace(planner, model=planner.light_model or planner.model),
                              TOUCHES_SCHEMA, log_name=f"touches-{len(need)}")
        if res.limited:
            raise LimitHit(res.text)
        if not res.ok:
            self.log(f"predicting touched files failed ({res.subtype}); scheduling without them")
            return False
        asked = {t.key for t in need}
        n = 0
        for e in res.structured.get("tasks", []):
            key = str(e.get("key", "")).strip()
            if key not in asked:
                continue
            d, hit = plan.tasks[key], False
            if d.touches is None and (touches := clean_touches(e.get("touches"))) is not None:
                d.touches, hit = touches, True
            if d.tier is None and (tier := clean_tier(e.get("tier"))) is not None:
                d.tier, hit = tier, True
            n += hit
        light = sum(1 for t in need if plan.tasks[t.key].tier == "light")
        self.log(f"predicted files or tier for {n} of {len(need)} task(s); {light} rated light")
        return n > 0

    def triage_tickets(self, gh: GitHub, issues: dict[str, dict]) -> None:
        """Approve purely technical proposals without Max (target `auto_approve`).

        One run on the full planner model over every untriaged ticket in proposed that nobody
        has worked on. `approve` moves it to pending implementation; `ask` leaves it for
        Max. Both comment the reason. Each ticket is triaged once (state `triaged`), so an `ask`
        isn't re-asked and Max can always override by hand. A failed run is retried next sync."""
        if not self.t.auto_approve:
            return
        done = set(self.state.data.get("triaged", []))
        need = [k for k, i in issues.items()
                if k in self.tasks and k.startswith(TICKET_CHANGE + "/") and i["state"].upper() == "OPEN"
                and status_from_labels(i["labels"]) is Status.PROPOSED and k not in done
                and not self.t.is_parked(k, i["number"])]
        if not need:
            return
        self.log(f"triaging {len(need)} ticket(s)")
        rows = "\n\n".join(f"### {k}: {self.tasks[k].text}\n\n{self.ticket_bodies.get(k, '')[:4000]}" for k in need)
        prompt = review.prompt("ticket_triage", repo_root=self.base_wt, target=self.t.name,
                               project_dir=self.t.project_dir, tickets=rows)
        res = self.claude.run(prompt, self.base_wt, self.cfg.agents["planner"], TRIAGE_SCHEMA,
                              log_name=f"ticket-triage-{len(need)}")
        if res.limited:
            raise LimitHit(res.text)
        if not res.ok:
            self.log(f"triaging tickets failed ({res.subtype}); retrying next sync")
            return
        approved = 0
        for e in res.structured.get("tickets", []):
            key = str(e.get("key", "")).strip()
            if key not in need or key in done:
                continue
            reason = str(e.get("reason", "")).strip() or "no reason given"
            n = issues[key]["number"]
            if e.get("verdict") == "approve":
                gh.set_status(n, Status.PENDING_IMPLEMENTATION, Status.PROPOSED)
                issues[key]["labels"] = [Status.PENDING_IMPLEMENTATION.label] + [
                    l for l in issues[key]["labels"] if l != Status.PROPOSED.label]
                gh.comment(n, review.bot(f"**Approved by triage** (technical, no change a player sees): "
                                         f"{reason} Close the issue to reject it."))
                approved += 1
            else:
                gh.comment(n, review.bot(f"**Needs your decision:** {reason} Approve by setting "
                                         f"`{Status.PENDING_IMPLEMENTATION.label}`; close it to reject."))
            done.add(key)
            self.log(f"#{n} triage: {e.get('verdict')}: {reason}")
        with self.state.lock:
            self.state.data["triaged"] = sorted(done, key=key_sort)
        self.state.save()
        self.log(f"triaged {len(need)} ticket(s); {approved} approved")

    def ticket_origin(self, key: str, seen: frozenset[str] = frozenset()) -> str | None:
        """The OpenSpec change whose work led to this ticket, following tickets filed while
        working other tickets back to a task. Cached in state.json as `origin`, since a parent
        ticket's body is gone from view once it closes."""
        st = self.state.task(key)
        if st.get("origin"):
            return st["origin"]
        src = filed_from(self.ticket_bodies.get(key, ""))
        if not src or key in seen:
            return None
        origin = self.ticket_origin(src, seen | {key}) if src.startswith(TICKET_CHANGE + "/") \
            else src.split("/")[0]
        if origin:
            st["origin"] = origin
        return origin

    def rate_tickets(self, issues: dict[str, dict]) -> None:
        """Rate approved tickets (one run, on the planner's light model), into state.json:
        `tier` (`light` or `standard`) for those not yet started, and for every one `unblocks`,
        whether it fixes something broken now. Tickets aren't planned, so this is where they get
        the tier the plan gives tasks; `unblocks` and the ticket's origin set its place in the
        queue (``milestones.ticket_rank``). Only approved ones: a proposal may never be built.
        A failed run is logged and not retried until a restart; an unrated ticket runs as
        `standard`, after its milestone's tasks."""
        open_tickets = [k for k, i in issues.items()
                        if k in self.tasks and k.startswith(TICKET_CHANGE + "/") and i["state"].upper() == "OPEN"]
        found = [self.ticket_origin(k) for k in open_tickets if "origin" not in self.state.task(k)]
        if any(found):  # a list, so every ticket gets its origin, not just the first
            self.state.save()
        if not self.cfg.agents["implementer"].light_model:
            return
        live = (Status.PENDING_IMPLEMENTATION, Status.IN_PROGRESS, Status.CHANGES_REQUESTED)
        need = [k for k in open_tickets
                if status_from_labels(issues[k]["labels"]) in live and k not in self.tiers_tried
                and ("unblocks" not in self.state.task(k)
                     or ("tier" not in self.state.task(k) and not self.state.task(k).get("runs")))]
        if not need:
            return
        self.tiers_tried |= set(need)
        self.log(f"rating {len(need)} approved ticket(s)")
        rows = "\n\n".join(f"### {k}: {self.tasks[k].text}\n\n{self.ticket_bodies.get(k, '')[:4000]}" for k in need)
        prompt = review.prompt("ticket_tiers", repo_root=self.base_wt, target=self.t.name,
                               project_dir=self.t.project_dir, tickets=rows)
        planner = self.cfg.agents["planner"]
        res = self.claude.run(prompt, self.base_wt, replace(planner, model=planner.light_model or planner.model),
                              TIER_SCHEMA, log_name=f"ticket-tiers-{len(need)}")
        if res.limited:
            raise LimitHit(res.text)
        if not res.ok:
            self.log(f"rating tickets failed ({res.subtype}); they run as standard")
            return
        light = unblocking = 0
        for e in res.structured.get("tickets", []):
            key = str(e.get("key", "")).strip()
            if key not in need:
                continue
            st = self.state.task(key)
            if (tier := clean_tier(e.get("tier"))) and "tier" not in st and not st.get("runs"):
                st["tier"] = tier
                light += tier == "light"
            if isinstance(e.get("unblocks"), bool):
                st["unblocks"] = e["unblocks"]
                unblocking += e["unblocks"]
        self.state.save()
        self.log(f"rated {len(need)} ticket(s); {light} light, {unblocking} unblocking")

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
        play = milestones.play_tags(self.t.milestones)
        released = self.git.remote_tags() if play else set()
        actions = syncplan.compute(tasks, plan, issues, set(st.data["flagged"]), set(st.data["cleared"]),
                                   play, released)
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
        self.check_waiters(issues)
        # After the ready agents started: a task without a prediction is simply never held back.
        if self.backfill_touches(tasks, plan):
            plan.save(self.plan_path)
        self.triage_tickets(gh, issues)
        self.rate_tickets(issues)
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
            self.log(f"#{n} created [{a.status.value}] {self.what(t.key)}")
            if a.status is Status.NEEDS_DECISION:
                self.flag(gh, a.key, n, a.reason)
        elif isinstance(a, syncplan.SetStatus):
            gh.set_status(a.number, a.new, a.old)
            self.log(f"#{a.number} {self.what(a.key)}: {a.old.value if a.old else '-'} -> {a.new.value}")
            if a.new is Status.NEEDS_DECISION:
                self.flag(gh, a.key, a.number, a.reason)
            elif a.old is Status.NEEDS_DECISION:
                # Taken back from the human until its blockers are done; it is flagged again then.
                with st.lock:
                    if a.key in st.data["flagged"]:
                        st.data["flagged"].remove(a.key)
                gh.comment(a.number, review.bot(
                    "**Not ready for input yet.** This waits for the tasks it is blocked by; "
                    "the pipeline will ask again once they are done."))
        elif isinstance(a, syncplan.Close):
            gh.set_status(a.number, Status.FINISHED, a.old)
            gh.comment(a.number, review.bot("Ticked in tasks.md outside the pipeline; closing."))
            gh.close_issue(a.number)
            self.log(f"#{a.number} {self.what(a.key)} closed (ticked by hand)")
            self.release_waiters(a.number)
        elif isinstance(a, syncplan.Cleared):
            with st.lock:
                st.data["flagged"].remove(a.key)
                st.data["cleared"].append(a.key)
            self.log(f"{a.key}: decision cleared by a human")

    def load_tickets(self, issues: dict[str, dict]) -> None:
        """Agent-filed tickets become tasks too; they wait in proposed until a human (or triage) approves."""
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
                self.log(f"{self.what(key)} waits for {', '.join(self.what(w) for w in waiting)} to merge")
        return waiting

    # ------------------------------------------------------------------ waiting on other tasks
    # A waiter is an issue in `awaiting task`. What it waits for are its GitHub blocked-by
    # links; GitHub shows the backlink ("blocking") on each blocker. When an issue closes, its
    # backlinks are checked, and a waiter is released once its last blocker is done.

    def register_wait(self, waiter: int, blockers: list[int], old: Status | None = None) -> None:
        """Make ``waiter`` wait for ``blockers`` (issue numbers): link, backlink, label, comment."""
        have = self.gh.blocked_by(waiter)
        for b in blockers:
            if b not in have:
                self.gh.add_blocked_by(waiter, self.gh.issue_db_id(b))
                self.gh.comment(b, review.bot(
                    f"**#{waiter} waits for this.** It is released once this and everything else "
                    "it waits for are done."))
        if old is not Status.AWAITING_TASK:
            self.gh.set_status(waiter, Status.AWAITING_TASK, old)
        refs = ", ".join(f"#{b}" for b in sorted(set(blockers) | set(have)))
        self.gh.comment(waiter, review.bot(
            f"**Awaiting task:** waits for {refs}. The pipeline sets it to "
            f"`{Status.PENDING_IMPLEMENTATION.label}` once all of them are done."))
        self.log(f"{self.issue_ref(waiter)} awaits {refs}")
        self.check_waiter(self.gh.issue(waiter))

    def check_waiter(self, issue: dict) -> bool:
        """Release an open `awaiting task` issue whose blockers are all done; True if released."""
        n = issue["number"]
        if issue["state"].upper() != "OPEN" or status_from_labels(issue["labels"]) is not Status.AWAITING_TASK:
            return False
        blockers = self.gh.blockers(n)
        if not blockers:
            self.to_decision(n, Status.AWAITING_TASK,
                             "Status is `awaiting task`, but it isn't blocked by anything. Name what it "
                             f"waits for (`agentpipe wait {self.t.name} {n} <issue or task key>...`), "
                             "or set it to the status it should resume from.")
            return False
        dropped = sorted(b for b, s in blockers.items() if s["state"] == "closed" and s["reason"] == "not_planned")
        if dropped:
            self.to_decision(n, Status.AWAITING_TASK,
                             f"It waits for {', '.join(f'#{b}' for b in dropped)}, which was closed without "
                             "being done. Decide whether it still waits for something.")
            return False
        if any(s["state"] != "closed" for s in blockers.values()):
            return False
        self.gh.set_status(n, Status.PENDING_IMPLEMENTATION, Status.AWAITING_TASK)
        refs = ", ".join(f"#{b}" for b in sorted(blockers))
        self.gh.comment(n, review.bot(f"**Released.** Everything it waited for is done: {refs}."))
        self.log(f"{self.issue_ref(n)} released: {refs} done")
        return True

    def release_waiters(self, done: int) -> None:
        """Issue ``done`` just closed: release the waiters whose last blocker it was."""
        try:
            for n in self.gh.blocking(done):
                self.check_waiter(self.gh.issue(n))
        except proc.CommandError as e:
            # The sync's sweep over every waiter catches anything missed here.
            self.log(f"releasing waiters of #{done} failed; the next sync retries: {str(e)[-200:]}")

    def check_waiters(self, issues: dict[str, dict]) -> None:
        """Sweep: blockers can close outside the pipeline, or while the dispatcher was down."""
        for i in issues.values():
            if i["state"].upper() == "OPEN" and status_from_labels(i["labels"]) is Status.AWAITING_TASK:
                self.check_waiter(i)

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
            n = self.gh.create_issue(title, body, Status.PROPOSED, (f"kind: {kind}",))
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
            if status_from_labels(issue["labels"]) is Status.AWAITING_TASK:
                continue  # its links say what it waits for; the plan doesn't own them
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
                    blocker = next((k for k, i in issues.items() if i["number"] == n), None)
                    self.log(f"#{issue['number']} {self.what(key)}: could not link blocked-by "
                             f"#{n}{' ' + self.what(blocker) if blocker else ''}: {str(e)[-200:]}")
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
        self.reload_config()
        if self.resume_file.exists():
            self.resume_file.unlink(missing_ok=True)
            clear_pause(self.state)
            self.log("resume requested: usage pause cleared; the next agent run reports fresh usage")
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
            # Claude Code offers agents a PowerShell tool only on Windows (not in WSL).
            shells = [s for s in ("Bash", "PowerShell") if s in agent.tools
                      and (s != "PowerShell" or os.name == "nt")]
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

        def priority(k: str) -> tuple:
            # Milestone work first (the next test build), then by task key.
            if self.tasks[k].change == TICKET_CHANGE:
                st = self.state.task(k)
                return milestones.ticket_rank(self.t.milestones, st.get("origin"),
                                              st.get("unblocks", False)), key_sort(k)
            return milestones.rank(self.t.milestones, self.tasks[k].change), key_sort(k)

        def by_status(s: Status) -> list[str]:
            return sorted((k for k, i in issues.items()
                           if status_from_labels(i["labels"]) is s and k not in self.jobs
                           and k not in self.merging), key=priority)

        for s in (Status.PENDING_REVIEW, Status.CHANGES_REQUESTED, Status.APPROVED):
            for key in by_status(s):
                if key in prs:
                    continue
                if (s is Status.PENDING_REVIEW and key.startswith(TICKET_CHANGE + "/")
                        and not self.state.task(key).get("runs")):
                    # A ticket nobody has worked on is a proposal: carried over from the old
                    # shared "pending approval" label, or set by hand.
                    self.gh.set_status(issues[key]["number"], Status.PROPOSED, None)
                    issues[key]["labels"] = [Status.PROPOSED.label]
                    self.log(f"{self.what(key)}: no PR and never worked on, so it is a proposal")
                    continue
                self.to_decision(issues[key]["number"], s, f"Status is `{s.value}` but no open PR was found.")
        self.record_awaiting(issues)
        for key in by_status(Status.IN_PROGRESS) + by_status(Status.CHANGES_REQUESTED) + \
                by_status(Status.PENDING_IMPLEMENTATION):
            if not free_impl:
                break
            status = status_from_labels(issues[key]["labels"])
            kind = "revise" if status is Status.CHANGES_REQUESTED else "implement"
            if kind == "revise" and key not in prs:
                continue
            if kind == "implement" and (waits := self.ticket_waits_for(key)):
                # Link the named tasks that have an open issue, so their merge releases this.
                # A task without one (not created yet, or closed but not yet ticked) is
                # picked up by a later tick, which waits until tasks.md says it is done.
                if open_ := [issues[w]["number"] for w in waits if w in issues]:
                    try:
                        self.register_wait(issues[key]["number"], open_, status)
                        issues[key]["labels"] = [Status.AWAITING_TASK.label]
                    except proc.CommandError as e:
                        self.log(f"{self.what(key)}: could not register its wait; retrying next tick: "
                                 f"{str(e)[-200:]}")
                continue
            # Only fresh starts wait: work already under way resumes, or two waiting on each
            # other would never run.
            if status is Status.PENDING_IMPLEMENTATION and (why := self.overlap_hold(key, issues, prs)):
                if self.held.get(key) != why:
                    self.held[key] = why
                    self.log(f"{self.what(key)} {why}")
                continue
            self.held.pop(key, None)
            self.start(kind, key, free_impl.pop(0), issues[key], prs.get(key))
        for key in by_status(Status.PENDING_REVIEW):
            if not free_rev:
                break
            pr = prs.get(key)
            if pr and self.needs_review(pr, issues[key]["number"]):
                self.start("review", key, free_rev.pop(0), issues[key], pr)
        if not merging:
            # Oldest approval first, not milestone order: merging is cheap and releases the files
            # an approved PR holds (`touches`), and a PR left waiting conflicts with a moving base.
            # Approvals from before this was recorded count as oldest.
            approved = sorted(by_status(Status.APPROVED), key=lambda k: self.state.task(k).get("approved_at", 0))
            # But one the review gated against the current base needs no gate (job_merge), so it
            # takes about a minute and goes first, before the base moves and it goes stale.
            base = self.current_base()
            fresh = [k for k in approved if base and k in prs
                     and (self.state.task(k).get("gated") or {}).get("base") == base
                     and (self.state.task(k).get("gated") or {}).get("head") == prs[k]["headRefOid"]]
            approved = fresh + [k for k in approved if k not in fresh]
            order = [(self.tasks[k], issues[k]["number"], prs[k]) for k in approved if k in prs]
            if order:
                # The merge job may take the next ones along in a batch (merge_train).
                self.merge_order = order[:self.t.merge_batch]
                t0 = order[0][0]
                self.start("merge", t0.key, "merge", issues[t0.key], prs[t0.key])

    def touches_of(self, key: str) -> list[str]:
        """Repo-relative paths a task (with its batch) will likely change: the planner's
        prediction, or for a ticket the files its body names."""
        if key.startswith(TICKET_CHANGE + "/"):
            pd = conflicts.normalize(self.t.project_dir)
            paths = conflicts.ticket_paths(self.ticket_bodies.get(key, ""))
            if pd in ("", "."):
                return paths
            return [p if p.startswith(pd + "/") else f"{pd}/{p}" for p in paths]
        if self.plan is None:
            return []
        return [p for k in self.plan.batch_of(key) for p in (self.plan.get(k).touches or [])]

    def overlap_hold(self, key: str, issues: dict[str, dict], prs: dict[str, dict]) -> str:
        """Why ``key`` should wait before starting: another task in flight touches its files.

        In flight means being implemented, or having an open PR that isn't parked with a human.
        Its files are its predicted ones plus, once it has a PR, the files the PR changes.
        Returns "" when the task may start (also when nothing about its files is known)."""
        mine = self.touches_of(key)
        if not mine:
            return ""
        active = {Status.IN_PROGRESS, Status.PENDING_REVIEW, Status.CHANGES_REQUESTED, Status.APPROVED}
        for k, i in sorted(issues.items(), key=lambda kv: key_sort(kv[0])):
            if k == key:
                continue
            job = self.jobs.get(k)
            if not ((job and job.kind in IMPL_KINDS) or status_from_labels(i["labels"]) in active):
                continue
            pr = prs.get(k)
            theirs = self.touches_of(k) + [f.get("path", "") for f in (pr or {}).get("files") or []]
            if both := conflicts.overlapping(mine, theirs, self.t.overlap_ignore):
                shown = ", ".join(f"`{p}`" for p in both[:3]) + (f" and {len(both) - 3} more" if len(both) > 3 else "")
                via = f"PR #{pr['number']}" if pr else f"#{i['number']}"
                return f"waits for {k} ({via}) to merge first: both touch {shown}"
        return ""

    def record_awaiting(self, issues: dict[str, dict]) -> None:
        """Save the issues waiting on Max, for the watcher's header.

        These are issues in needs decision or proposed. Parked items (target `parked`,
        milestone `deferred`) aren't counted.
        """
        awaiting = []
        for key, i in issues.items():
            if self.t.is_parked(key, i["number"]):
                continue
            if status_from_labels(i["labels"]) in (Status.NEEDS_DECISION, Status.PROPOSED):
                awaiting.append(i["number"])
        awaiting.sort()
        with self.state.lock:
            if self.state.data.get("awaiting_input") == awaiting:
                return
            self.state.data["awaiting_input"] = awaiting
        self.state.save()

    def start(self, kind: str, key: str, slot: str, issue: dict, pr: dict | None) -> None:
        fn = getattr(self, f"job_{kind}")
        about = self.about(key, 160)  # the watcher gives it two lines
        self.log(f"start {kind} {key} (#{issue['number']}) on {slot}" + (f": {about}" if about else ""))
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
                  if isinstance(w, dict) and (w.get("status") == "rejected" or w.get("utilization", 0) >= 1)]
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

    def needs_review(self, pr: dict, issue: int) -> bool:
        """True unless this head was reviewed already. A review moves the issue off pending
        review, so a reviewed head still labelled pending review was set back after its review:
        by hand, or by a revise that changed no code. Review it again then, rather than leaving
        it to wait for a new commit that never comes."""
        n, sha = pr["number"], pr["headRefOid"]
        if self.reviewed.get(n) != sha:
            history = review.review_history(self.gh.comments(n))
            if not history or history[-1]["sha"] != sha:
                return True
            self.reviewed[n] = sha
            self.review_at[n] = history[-1]["at"]
        at = self.gh.label_added_at(issue, Status.PENDING_REVIEW.label)
        if at and at > self.review_at.get(n, ""):
            self.log(f"PR #{pr['number']} was set to pending review after its last review of this "
                     "commit; reviewing it again")
            return True
        if self.held.get(f"review#{n}") != sha:
            self.held[f"review#{n}"] = sha
            self.log(f"PR #{n} is pending review, but commit {sha[:10]} was already reviewed and "
                     "the label predates that review; set the label again to have it re-reviewed")
        return False

    def to_decision(self, number: int, old: Status | None, why: str) -> None:
        self.gh.set_status(number, Status.NEEDS_DECISION, old)
        self.gh.comment(number, review.bot(
            f"**Needs a decision.** {why}\n\nWhen resolved, set the label to the status the task "
            f"should resume from (usually `{Status.PENDING_IMPLEMENTATION.label}`)."))
        self.log(f"{self.issue_ref(number)} -> needs decision:{why.splitlines()[0][:120]}")

    @property
    def stop_file(self) -> Path:
        """`agentpipe stop` creates this; the dispatcher then drains its jobs and exits."""
        return self.t.work_dir / "stop"

    @property
    def resume_file(self) -> Path:
        """`agentpipe resume` creates this; the dispatcher then drops its usage pause."""
        return self.t.work_dir / "resume"

    @property
    def pid_file(self) -> Path:
        return self.t.work_dir / "dispatcher.pid"

    def stop_reason(self) -> str | None:
        """Why the dispatcher should drain and exit now: a stop request, or its watcher closed."""
        if self.stop_file.exists():
            return "stop requested"
        live = service.live_watchers(self.t.work_dir)
        if live:
            self.had_watcher = True
        elif self.had_watcher:
            return "watcher window closed"
        return None

    def run_forever(self, once: bool = False) -> None:
        self.stop_file.unlink(missing_ok=True)  # a stale request must not stop a new run
        self.pid_file.write_text(str(os.getpid()), encoding="utf-8")
        self.had_watcher = False
        self.gh.ensure_labels()
        try:
            self.git.enable_rerere()
        except proc.CommandError as e:
            self.log(f"could not enable git rerere: {str(e)[-200:]}")
        self.log(f"running (pid {os.getpid()}); implementers={self.cfg.max_implementers} "
                 f"reviewers={self.cfg.max_reviewers}")
        try:
            while True:
                reason = self.stop_reason()
                if reason and not self.stopping:
                    self.stopping = True
                    self.log(f"{reason}: starting nothing new, waiting for {len(self.jobs)} running job(s)")
                elif not reason and self.stopping:
                    # `start` while draining removes the stop file and opens a watcher: keep going.
                    self.stopping = False
                    self.log("stop cancelled: running again")
                self.tick()
                if (once or self.stopping) and not self.jobs:
                    break
                # Sleep in short steps so a stop request (or its cancelling) is noticed within seconds.
                deadline = time.time() + (self.cfg.poll_seconds if not once else 5)
                while time.time() < deadline and self.stopping == bool(self.stop_reason()):
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
        return ("## Input from humans (authoritative; it overrides the spec where they conflict)\n\n"
                "Each decision or fact here must be written into the docs by the PR that acts on "
                "it (see \"Keep the docs current\"), not left only in a comment.\n\n" + body)

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
    def resumable(self, t: Task, run: str = "impl") -> str | None:
        """The agent session a ``run`` (impl, revise, conflict, cleanup) would resume, or None.
        An old session would be re-cached in full and carries stale context, so past
        resume_within_seconds a run starts fresh; so does a run on another model than the
        session's, which could reuse none of its cache."""
        st = self.state.task(t.key)
        s = st.get("session")
        if not s or time.time() - st.get("session_at", 0) > self.cfg.resume_within_seconds:
            return None
        if st.get("session_model", self.model_for(t, run)) != self.model_for(t, run):
            return None
        return s

    # ------------------------------------------------------------------ model tiers
    def light_task(self, t: Task) -> bool:
        """The planner rated every task of ``t``'s job light (a ticket: rate_tickets did)."""
        if t.change == TICKET_CHANGE:
            return self.state.task(t.key).get("tier") == "light"
        return bool(self.plan) and all(self.plan.get(x.key).tier == "light" for x in self.batch(t))

    def model_for(self, t: Task, run: str) -> str:
        """The implementer model for a ``run`` of task ``t``. The light model takes conflict and
        cleanup runs, and the first implementation of a light task; revisions (the reviewer
        rejected the work) and anything after an escalation get the full model."""
        a = self.cfg.agents["implementer"]
        if not a.light_model or self.state.task(t.key).get("escalated"):
            return a.model
        if run == "mutants":  # continues the session that wrote the code, on its model
            return self.state.task(t.key).get("session_model") or self.model_for(t, "impl")
        if run in LIGHT_RUNS or (run == "impl" and self.light_task(t)):
            return a.light_model
        return a.model

    def is_light(self, model: str) -> bool:
        a = self.cfg.agents["implementer"]
        return bool(a.light_model) and model == a.light_model != a.model

    def escalate(self, t: Task, why: str) -> None:
        """Run every later implementer job of ``t`` on the full model."""
        st = self.state.task(t.key)
        if st.get("escalated"):
            return
        st["escalated"] = why
        self.state.save()
        self.log(f"{self.what(t.key)}: moving to {self.cfg.agents['implementer'].model}: {why}")

    def prior_work(self, t: Task, pr: dict | None, run: str = "impl") -> str:
        """For a fresh session on a task with history: what is already there."""
        if self.resumable(t, run):
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

    def impl_prompt(self, t: Task, issue: int, pr: dict | None, run: str = "impl") -> str:
        tasks = self.batch(t)
        inside = {x.key for x in tasks}
        blockers = sorted({b for x in tasks for b in (self.plan.get(x.key).blocked_by if self.plan else [])
                           if b not in inside}, key=key_sort)
        return review.prompt(
            "implementer", key=t.key, text=t.text, section=t.section, issue=issue,
            blockers=", ".join(blockers) or "none", worktree=self.wt(t), project_dir=self.t.project_dir,
            spec_ref=self.spec_ref(t), context_list=self.context_list(),
            human_input=self.human_input(issue, pr), test_cmd=self.t.test, open_tickets=self.open_tickets(),
            batch_section=self.batch_section(t), prior_work=self.prior_work(t, pr, run),
            self_check=self.self_check(t))

    def self_check(self, t: Task) -> str:
        """What the implementer is told about the pipeline's mutation pass, or nothing for a
        target without one. The pipeline runs it (``mutation_pass``), so agents don't."""
        if not self.t.mutation:
            return ""
        return ("When you finish, the pipeline runs mutation testing on the lines you changed and "
                "hands you every mutant no test caught before the PR goes to review. You don't need "
                "to run it yourself, but each miss costs you another round, so probe first.")

    def ensure_worktree(self, t: Task) -> Path:
        wt = self.wt(t)
        if not (wt / ".git").exists():
            self.git.fetch()
            start = f"origin/{t.branch}" if self.git.remote_branch_exists(t.branch) else self.base_ref()
            self.git.worktree_add(wt, t.branch, start)
            self.state.task(t.key).pop("session", None)
        return wt

    def agent(self, t: Task, prompt: str, slot: str, tag: str, retry_prompt=None) -> ClaudeResult:
        """One implementer run (``tag``: impl, revise, conflict or cleanup) on the model
        ``model_for`` picks. A light run that hits its cap or timeout escalates the task and is
        redone at once on the full model, from its saved WIP, with ``retry_prompt()`` (the
        prompt for a fresh session) if given, else the same prompt."""
        model = self.model_for(t, tag)
        res = self.run_agent(t, prompt, slot, tag, model)
        if self.is_light(model) and res.subtype in CAPPED:
            what = "its turn cap" if res.subtype == MAX_TURNS else "the timeout"
            self.escalate(t, f"a {tag} run on {model} hit {what}")
            self.state.task(t.key).pop("session", None)
            self.save_wip(t, f"{what} on {model}")
            res = self.run_agent(t, retry_prompt() if retry_prompt else prompt, slot, tag,
                                 self.model_for(t, tag))
        return res

    def run_agent(self, t: Task, prompt: str, slot: str, tag: str, model: str) -> ClaudeResult:
        st = self.state.task(t.key)
        n = st.get("runs", 0) + 1
        st["runs"] = n
        self.clean_workspace_crates(self.wt(t) / self.t.project_dir, slot)
        resume = self.resumable(t, tag)
        res = self.claude.run(prompt, self.wt(t) / self.t.project_dir,
                              replace(self.agent_def("implementer"), model=model),
                              review.IMPLEMENTER_SCHEMA, log_name=f"{t.slug}-{tag}-{n}",
                              resume=resume, env=self.env(slot))
        if res.session_id:
            st.update(session=res.session_id, session_at=time.time(), session_model=model)
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
        res = self.agent(t, prompt, slot, "impl", retry_prompt=lambda: self.impl_prompt(t, issue, pr))
        self.deliver(t, issue, pr, res, slot, Status.IN_PROGRESS)

    def merge_base(self, wt: Path, slot: str, gate: bool) -> str:
        """Bring the branch checked out in ``wt`` up to date with the base branch.

        Returns "current" (it already contains the base), "clean" (merged without conflicts, or
        rerere replayed earlier resolutions of all of them), "auto" (the dispatcher resolved
        conflicts where both sides only added lines, by keeping both, then ran `format`), or
        "conflict": a real conflict, left in progress for an agent. With ``gate``, a merge that
        needed resolving ("auto", or a rerere replay) must pass the test gate, else it is undone
        and redone as "conflict" so an agent looks at it.
        """
        if self.git.contains(wt, self.base_ref()):
            return "current"
        before = self.git.head(wt)
        if self.git.merge(wt, self.base_ref()):
            return "clean"
        paths = self.git.conflicted(wt)
        fixed: dict[str, bytes] = {}
        for p in paths:
            try:
                text = (wt / p).read_bytes()
            except OSError:
                return "conflict"  # deleted on one side, or not a file
            if (resolved := conflicts.resolve_additive(text)) is None:
                return "conflict"
            fixed[p] = resolved
        for p, data in fixed.items():
            (wt / p).write_bytes(data)
            self.git.git("add", "--", p, cwd=wt)
        if fixed and self.t.format:
            f = proc.run(self.t.format, wt / self.t.project_dir, env=self.env(slot), timeout=900,
                         check=False, shell=True)
            if not f.ok:
                self.log(f"`{self.t.format}` failed after resolving a merge in {wt.name}: {f.tail(300)}")
            self.git.git("add", "-u", cwd=wt)
        # rerere off for this commit: it should remember agents' tested resolutions, not ours.
        self.git.git("-c", "rerere.enabled=false", "commit", "--no-edit", "--no-verify", "--quiet", cwd=wt)
        how =("kept both sides of additive conflicts in " + ", ".join(f"`{p}`" for p in fixed)
               if fixed else "replayed earlier resolutions (rerere)")
        if gate:
            g = self.run_gate(wt / self.t.project_dir, slot, self.t.test, self.t.test_timeout)
            if not g.ok:
                self.log(f"merging {self.base_ref()} in {wt.name}: {how}, but the gate fails; "
                         "handing the conflict to an agent")
                self.git.git("reset", "--hard", "--quiet", before, cwd=wt)
                # Without rerere, so the agent sees the raw conflicts rather than a replayed
                # resolution that just failed the gate.
                self.git.git("-c", "merge.conflictStyle=diff3", "-c", "rerere.enabled=false", "merge",
                             "--no-edit", self.base_ref(), cwd=wt, check=False)
                return "conflict"
        self.log(f"merged {self.base_ref()} in {wt.name}: {how}")
        return "auto"

    def conflict_prompt(self, t: Task) -> str:
        return (f"You are an implementer agent in an unattended pipeline, working on **{t.key}** — {t.text} "
                f"({self.spec_ref(t)}). `{self.base_ref()}` has moved on and is being merged into your "
                "branch; the merge is in progress in this worktree and conflicts. Resolve every conflict "
                "keeping both sides' intent (the base's changes are merged work: keep them), change "
                f"nothing the merge doesn't need, run `{self.t.test}` from the project dir until it "
                "passes, and commit the merge. `git checkout` is denied: take one side of a file with "
                "`git restore --theirs <file>` (or `--ours`). Then return the structured output with "
                "status `done`; in `summary`, say how you resolved each conflicted file.")

    def resolve_conflict(self, t: Task, slot: str) -> ClaudeResult:
        """A conflict run for the merge in progress in ``t``'s worktree. A light run that ends
        without resolving it escalates the task and the full model tries once more."""
        wt = self.wt(t)
        model = self.model_for(t, "conflict")
        res = self.agent(t, self.conflict_prompt(t), slot, "conflict")
        # A capped light run was already redone on the full model inside agent().
        still_light = self.model_for(t, "conflict") == model
        if (self.is_light(model) and still_light and res.subtype not in CAPPED
                and (not res.ok or self.git.merging(wt) or not self.git.is_clean(wt))):
            self.escalate(t, f"a conflict run on {model} left the merge unresolved")
            res = self.agent(t, self.conflict_prompt(t), slot, "conflict")
        return res

    def job_revise(self, t: Task, issue: int, pr: dict, slot: str) -> None:
        wt = self.ensure_worktree(t)
        comments = self.gh.comments(pr["number"])
        history = review.review_history(comments)
        last = history[-1] if history else None
        merge_note = ""
        if last and last["verdict"] == review.CONFLICT:
            # Only the base moved: update the branch without a full revise. A clean or mechanical
            # merge needs no agent at all; a real conflict gets a short conflict-only run.
            self.git.fetch()
            m = self.merge_base(wt, slot, gate=True)
            if m in ("clean", "auto"):
                self.git.push(wt, t.branch)
                self.gh.comment(pr["number"], review.bot(
                    f"**Updated.** Merged `{self.base_ref()}` into the branch" +
                    (" and resolved its conflicts mechanically (both sides only added lines); the gate passes."
                     if m == "auto" else "; it merged cleanly.")))
                self.gh.set_status(issue, Status.PENDING_REVIEW, Status.CHANGES_REQUESTED)
                return
            if m == "conflict":
                res = self.resolve_conflict(t, slot)
                if res.subtype not in CAPPED and (not res.ok or self.git.merging(wt) or not self.git.is_clean(wt)):
                    self.git.merge_abort(wt)
                    self.to_decision(issue, Status.CHANGES_REQUESTED,
                                     "Could not resolve merge conflicts with the base branch.")
                    return
                return self.deliver(t, issue, pr, res, slot, Status.CHANGES_REQUESTED)
            # "current": the branch has the base, yet GitHub refused the merge: let the agent look.
            merge_note = (f"The branch already contains `{self.base_ref()}`, yet GitHub reported it as not "
                          "mergeable. Check `git status` and the branch history, fix what blocks the merge, "
                          "run the tests, and commit.")
        prompt = review.prompt(
            "revise", key=t.key, round=review.rounds_used(history), max_rounds=self.cfg.max_rounds,
            feedback=last["body"] if last else "(no review found)", merge_note=merge_note,
            test_cmd=self.t.test, self_check=self.self_check(t))
        if not merge_note and self.light_task(t) and self.cfg.agents["implementer"].light_model:
            self.escalate(t, "the reviewer rejected its PR")
        if not self.resumable(t, "revise"):
            prompt = self.impl_prompt(t, issue, pr, "revise") + "\n\n---\n\n" + prompt
        else:
            prompt += "\n\n" + self.human_input(issue, pr)
        res = self.agent(t, prompt, slot, "revise")
        self.deliver(t, issue, pr, res, slot, Status.CHANGES_REQUESTED)

    def run_mutation(self, t: Task, wt: Path, slot: str) -> str:
        """The target's mutation command on HEAD's diff against the base, run by the pipeline
        (not an agent) in ``wt``. Its output tail is stored by commit for the reviewer."""
        pdir = wt / self.t.project_dir
        diff_path = wt.parent / f"{t.slug}.mutation.diff"
        diff_path.write_text(self.git.diff(pdir, self.base_ref(), relative=True), encoding="utf-8")
        r = proc.run(self.t.mutation.replace("{diff}", str(diff_path)), pdir, env=self.env(slot),
                     timeout=self.t.mutation_timeout, check=False, shell=True)
        self.git.restore(wt)  # an in-place run cut off mid-mutant must not leave a mutated file
        text = r.tail(5000)
        self.state.task(t.key)["mutation"] = {"head": self.git.head(wt), "output": text}
        self.state.save()
        return text

    def mutation_pass(self, t: Task, slot: str, out: dict) -> dict:
        """Before a push: run mutation on the implementer's work, and send what it missed straight
        back to the implementer (up to MUTATION_FIXES times) instead of spending a review on it.
        The last run's result, stored by commit, is the reviewer's evidence. Returns the
        implementer's structured output, updated by any fix run."""
        if not self.t.mutation:
            return out
        wt = self.wt(t)
        for attempt in range(MUTATION_FIXES + 1):
            self.log(f"phase {t.key}: mutation testing (before review)")
            text = self.run_mutation(t, wt, slot)
            missed = [l for l in text.splitlines() if l.startswith("MISSED")]
            if not missed or attempt == MUTATION_FIXES:
                if missed:
                    self.log(f"{self.what(t.key)}: {len(missed)} mutant(s) still missed; the reviewer judges them")
                return out
            self.log(f"{self.what(t.key)}: {len(missed)} mutant(s) missed; back to the implementer")
            res = self.agent(t, (
                "The pipeline ran mutation testing on your committed work before review. Each `MISSED` "
                "line below is a change to your code that no test caught; the reviewer gets this same "
                "list and rejects every real gap. For each one, add a test that catches it, or, if the "
                "mutant can't change observable behaviour, say why in `tests`. Look for the same kind of "
                "gap in the rest of your change too. Run the tests, commit, and return the structured "
                "output again, with `summary` and `tests` describing the whole change.\n\n```\n"
                + "\n".join(missed)[:6000] + "\n```"), slot, "mutants")
            if not res.ok or res.subtype in CAPPED or res.structured.get("status") != "done":
                self.log(f"{self.what(t.key)}: the mutation fix run didn't finish; reviewing what is committed")
                self.git.restore(wt)
                return out
            if not self.git.is_clean(wt):
                self.git.restore(wt)  # uncommitted leftovers; what was committed stands
            self.file_tickets(f"the implementer of {t.key}", res.structured.get("tickets"))
            out = {**out, **{k: v for k, v in res.structured.items() if k in ("summary", "tests", "docs") and v}}
        return out

    def stored_mutation(self, t: Task, pr: dict) -> str | None:
        """The pipeline's mutation output for the PR's head commit, if it ran on exactly that."""
        m = self.state.task(t.key).get("mutation") or {}
        return m.get("output") if m.get("head") and m["head"] == pr.get("headRefOid") else None

    def capped(self, t: Task, issue: int, cur: Status, res: ClaudeResult, role: str) -> None:
        """A run that hit its turn cap or timeout: stop instead of paying for it again."""
        st = self.state.task(t.key)
        st.pop("session", None)  # a requeue starts fresh, from what is committed
        self.state.save()
        a = self.agent_def(role)
        what = (f"its {a.max_turns}-turn cap" if res.subtype == MAX_TURNS
                else f"the {self.cfg.agent_timeout // 60}-minute timeout")
        saved = self.save_wip(t, what) if role == "implementer" else ""
        self.to_decision(issue, cur, (
            f"The {role} hit {what} without finishing.{saved} Rather than spend another run like it: split "
            f"the task in tasks.md if it is too big for one session, or requeue it to continue in a fresh "
            f"session from what is committed."))

    def save_wip(self, t: Task, what: str) -> str:
        """Commit and push what a capped implementer left uncommitted, so a requeue keeps it.

        Returns a sentence for the decision comment. A worktree in an unfinished merge is left
        alone: committing it would record conflict markers.
        """
        wt = self.wt(t)
        if not (wt / ".git").exists():
            return ""
        try:
            if self.git.merging(wt):
                return (" Its worktree is in an unfinished merge with uncommitted work, left as is: "
                        "resolve it by hand before requeueing.")
            if not self.git.commit_all(wt, f"{t.key}: WIP from a run that hit {what}\n\n"
                                           "Saved by the dispatcher; not yet gated."):
                return ""
            self.git.push(wt, t.branch)
        except proc.CommandError as e:
            self.log(f"could not save WIP for {self.what(t.key)}: {e}")
            return " Saving its uncommitted work failed; it is still in the worktree."
        self.log(f"saved WIP for {self.what(t.key)} after {what}")
        return (" Its uncommitted work was saved as a WIP commit on the branch (not yet gated), "
                "so a requeue continues from it.")

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
        # No gate here: the review runs it on whatever is pushed.
        if self.merge_base(wt, slot, gate=False) == "conflict":
            res = self.resolve_conflict(t, slot)
            if not res.ok or self.git.merging(wt) or not self.git.is_clean(wt):
                self.git.merge_abort(wt)
                self.to_decision(issue, cur, "Could not resolve merge conflicts with the base branch.")
                return
        out = self.mutation_pass(t, slot, out)
        self.git.push(wt, t.branch)
        tasks = self.batch(t)
        nums = self.state.task(t.key).get("batch_issues", {})
        closes = "\n".join([f"Closes #{issue}"] + [f"Closes #{nums[x.key]}" for x in tasks[1:] if x.key in nums])
        listing = "\n".join(f"**{x.key}** — {x.text}" for x in tasks)
        body = (f"{TASK_MARKER.format(key=t.key)}\n{closes}\n\n{listing}\n\n"
                f"## Summary\n\n{out['summary']}\n\n## Tests\n\n{out['tests']}\n\n"
                f"## Docs\n\n{out.get('docs') or 'none'}\n")
        if pr is None:
            title = (f"{t.key}: {t.text}" if len(tasks) == 1 else
                     f"{t.change} {', '.join(x.id for x in tasks)}: {t.text}")
            n = self.gh.create_pr(t.branch, self.t.base, title[:120], body)
            self.log(f"PR #{n} opened for {self.what(t.key)}" +(f" (batch of {len(tasks)})" if len(tasks) > 1 else ""))
        else:
            self.gh.comment(pr["number"], review.bot(f"**Revised.**\n\n{out['summary']}\n\n**Tests:** {out['tests']}"
                                                  f"\n\n**Docs:** {out.get('docs') or 'none'}"))
        self.gh.set_status(issue, Status.PENDING_REVIEW, cur)
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
        self.prune_checkout_scratch(slot)
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

    def prune_checkout_scratch(self, slot: str) -> None:
        """Delete per-checkout test scratch (`CARGO_TARGET_TMPDIR/checkout-*`, arpg ticket #307).

        One job uses a slot at a time, so nothing in this slot's target dir is in use when a gate
        or implementer starts; tests recreate their own folder. Without this, every worktree that
        ever ran in the slot leaves a folder behind, some holding whole nested target trees."""
        tmp = self.t.work_dir / "target" / slot / "tmp"
        for d in tmp.glob("checkout-*") if tmp.is_dir() else ():
            shutil.rmtree(d, ignore_errors=True)

    def job_review(self, t: Task, issue: int, pr: dict, slot: str) -> None:
        sha = pr["headRefOid"]
        rv = self.t.work_dir / "review" / t.slug
        self.git.fetch()
        self.git.worktree_detached(rv, sha)
        try:
            gated_base = self.merge_base_for_review(rv)
            pdir = rv / self.t.project_dir
            history = review.review_history(self.gh.comments(pr["number"]))
            round_ = review.rounds_used(history) + 1
            # Mutation testing runs in copies of the tree with target dirs of their own, so it
            # starts alongside the gate rather than after it (arpg: 3-17 min each, Nov 2026). Not
            # when a conflict-resolution review may do instead, which needs no mutation run.
            mutation = None
            if (self.t.mutation and self.t.mutation_alongside_gate and self.stored_mutation(t, pr) is None
                    and not review.resolution_review_possible(history)):
                self.log(f"phase {t.key}: test gate and mutation testing")
                mutation = self.start_mutation(t, rv, pdir, slot)
            else:
                self.log(f"phase {t.key}: test gate")
            try:
                gate = self.run_gate(pdir, slot, self.t.test, self.t.test_timeout)
            except BaseException:
                if mutation:
                    mutation.kill()
                raise
            if not gate.ok:
                if mutation:
                    mutation.kill()
                v = {"verdict": review.TESTS_FAILED, "summary": f"`{self.t.test}` failed on this commit.",
                     "findings": [], "probes": [], "details": gate.tail(6000)}
            else:
                try:
                    v = None if mutation else self.resolution_verdict(t, pr, rv, pdir, slot, history)
                    if v is None:
                        v = self.reviewer_verdict(t, pr, rv, pdir, slot, history, issue, mutation)
                except CappedRun as e:
                    return self.capped(t, issue, Status.PENDING_REVIEW, e.result, "reviewer")
            self.gh.comment(pr["number"], review.render_review(v, sha, round_, self.cfg.max_rounds))
            self.reviewed[pr["number"]] = sha
            self.review_at[pr["number"]] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.log(review_line(self.what(t.key), pr["number"], v, round_, self.cfg.max_rounds))
            if v["verdict"] == review.APPROVE:
                # For the merge job: approvals merge oldest first, and the gate just passed on this
                # head merged with this base, so an unchanged base needs no second gate.
                st = self.state.task(t.key)
                st["approved_at"] = time.time()
                st["gated"] = {"head": sha, "base": gated_base} if gated_base else None
                self.state.save()
                self.gh.set_status(issue, Status.APPROVED, Status.PENDING_REVIEW)
            elif round_ >= self.cfg.max_rounds:
                self.to_decision(issue, Status.PENDING_REVIEW,
                                 f"The reviewer rejected PR #{pr['number']} {round_} times; see the PR.")
            else:
                self.gh.set_status(issue, Status.CHANGES_REQUESTED, Status.PENDING_REVIEW)
        finally:
            self.git.worktree_remove(rv)

    def current_base(self) -> str:
        """The base commit as of the last fetch, or "" when it can't be read. Only a scheduling
        hint: job_merge fetches and checks again before it skips a gate."""
        try:
            return self.git.rev(self.base_wt, self.base_ref())
        except Exception:  # no base worktree yet, or a test's stub git
            return ""

    def merge_base_for_review(self, rv: Path) -> str | None:
        """Merges the base into the review worktree, locally (nothing is pushed), so the review
        judges the PR as it would land, and the gate and tests are the base's latest rather than
        the ones the branch forked with: arpg's gate fixes otherwise reached a PR only once its
        branch merged them (Nov 2026). A conflicting merge is undone and the PR reviewed as it is;
        the merge job deals with the conflict. Returns the base commit the review's gate then
        covers, or None when it covers only the branch."""
        base = self.git.rev(rv, self.base_ref())
        if self.git.contains(rv, self.base_ref()):
            return base
        if not self.git.merge(rv, self.base_ref()):
            self.git.merge_abort(rv)
            self.log(f"review worktree {rv.name}: {self.base_ref()} doesn't merge cleanly; reviewing the branch as it is")
            return None
        return base

    def resolution_verdict(self, t: Task, pr: dict, rv: Path, pdir: Path, slot: str, history) -> dict | None:
        """A short review for a PR that was approved, hit a conflict, and since then only merged
        the base: check the merges' conflict resolutions, not the whole PR again (no mutation run).

        Merges that resolved nothing (clean ones) are approved without an agent: the gate has
        passed and nobody wrote any code. Returns None when a full review is needed."""
        if not review.resolution_review_possible(history):
            return None
        sha = history[review.last_approval(history)]["sha"]
        if not self.git.contains(rv, sha):
            return None  # rewritten since the approval
        merges, others = self.git.merge_commits(rv, sha, self.base_ref())
        if others or not merges:
            return None
        diffs = [d for d in (self.git.remerge_diff(rv, c).strip() for c in merges) if d]
        if not diffs:
            return {"verdict": review.APPROVE, "findings": [], "probes": [],
                    "summary": f"Since the approval at `{sha[:10]}` the branch only merged `{self.base_ref()}`, "
                               "without conflicts to resolve, and the gate passes."}
        resolution = "\n".join(diffs)
        if len(resolution) > 40000:
            return None  # too big to judge from the resolution alone
        prompt = review.prompt(
            "resolution", pr=pr["number"], key=t.key, text=t.text, approved=sha[:10],
            base_ref=self.base_ref(), head=pr["headRefOid"][:10], project_dir=self.t.project_dir,
            test_cmd=self.t.test, resolution=resolution)
        self.log(f"phase {t.key}: agent (reviewer, conflict resolution only)")
        res = self.claude.run(prompt, pdir, self.agent_def("reviewer"), review.REVIEW_SCHEMA,
                              log_name=f"{t.slug}-resolution-{pr['headRefOid'][:8]}", env=self.env(slot))
        if res.limited:
            raise LimitHit(res.text)
        if res.subtype in CAPPED:
            raise CappedRun(res)
        if not res.ok:
            self.git.git("checkout", "--force", "--detach", "HEAD", cwd=rv)
            return None  # fall back to the full review
        self.file_tickets(f"the review of PR #{pr['number']} ({t.key})", res.structured.get("tickets"))
        return review.normalize_verdict(res.structured)

    def start_mutation(self, t: Task, rv: Path, pdir: Path, slot: str) -> proc.Background:
        diff_path = rv.parent / f"{t.slug}.diff"
        # Paths relative to the project dir, which is where the mutation tool runs.
        diff_path.write_text(self.git.diff(pdir, self.base_ref(), relative=True), encoding="utf-8")
        # Not run_gate: cleaning the workspace crates is the gate's business, not the mutants'.
        return proc.Background(self.t.mutation.replace("{diff}", str(diff_path)), pdir, env=self.env(slot),
                               timeout=self.t.mutation_timeout, shell=True)

    def reviewer_verdict(self, t: Task, pr: dict, rv: Path, pdir: Path, slot: str, history,
                         issue: int | None = None, mutation: proc.Background | None = None) -> dict:
        mutation_section = ""
        if self.t.mutation:
            if (text := self.stored_mutation(t, pr)) is not None:
                # The pipeline ran it on this exact commit before the push; the implementer has
                # already had the chance to fix what it missed.
                self.log(f"phase {t.key}: mutation result from before the push (same commit)")
            else:
                if mutation is None:
                    self.log(f"phase {t.key}: mutation testing")
                    mutation = self.start_mutation(t, rv, pdir, slot)
                else:
                    self.log(f"phase {t.key}: waiting for mutation testing")
                text = mutation.result().tail(5000)
                self.git.restore(rv)  # an in-place run killed mid-mutant must not leave a mutated file
            mutation_section = (
                "Mutation testing on the changed lines (`" + self.t.mutation + "`), run by the pipeline. "
                "Lines starting `MISSED` are mutants no test caught: each is a candidate coverage gap to "
                "judge, and a real one is a blocking test-coverage finding. The implementer saw this "
                "list before the PR came to you; its `Tests` section says why any it kept are harmless."
                "\n\n```\n" + text + "\n```")
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
        # Max's answers on the issue override the spec for the implementer, so the reviewer
        # must judge against them too; without them it tried `gh issue view --comments`.
        if issue is not None and (notes := self.human_input(issue, pr)):
            prompt += "\n\n" + notes
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
            self.git.git("checkout", "--force", "--detach", "HEAD", cwd=rv)  # undo stray probes
        raise RuntimeError(f"reviewer failed twice ({res.subtype}): {res.text[:1000]}")

    def job_merge(self, t: Task, issue: int, pr: dict, slot: str) -> None:
        order = self.merge_order
        if (not self.broke_a_batch(t, pr) and len(order) > 1 and order[0][0].key == t.key
                and self.merge_train(order, slot) == "merged"):
            return
        self.merge_one(t, issue, pr, slot)

    def broke_a_batch(self, t: Task, pr: dict) -> bool:
        """True if a batch bisect found that this PR, at this head, fails the gate. It then
        merges alone, where a failing gate sends it back with the output. A new head clears it."""
        return self.state.task(t.key).get("batch_culprit") == pr["headRefOid"]

    def merge_train(self, order: list[tuple[Task, int, dict]], slot: str) -> str:
        """Merge several approved PRs with one gate: merge them all onto the base in a scratch
        worktree, gate the result once, then squash-merge each on GitHub in the same order.
        A PR whose head moved, that doesn't merge into the batch cleanly, or that broke an
        earlier batch is left for later. If the gate fails, the batch is bisected: the longest
        passing prefix merges, and the PR after it is marked so it merges alone next time.
        Returns "merged" (at least the first PR landed), "too few" (under two PRs fit, nothing
        was gated) or "gate failed"; for the last two the caller merges the first PR alone."""
        train = self.t.work_dir / "train"
        self.git.fetch()
        self.git.worktree_detached(train, self.base_ref())
        try:
            members, heads = [], []  # heads[i]: the batch's commit once members[:i+1] are merged
            for t, issue, pr in order:
                ref = f"origin/{t.branch}"
                if self.broke_a_batch(t, pr):
                    self.log(f"merge batch: {self.what(t.key)} left out, it broke an earlier batch")
                elif self.git.rev(train, ref) != pr["headRefOid"]:
                    self.log(f"merge batch: {self.what(t.key)} left out, its head moved after approval")
                elif self.git.merge(train, ref):
                    members.append((t, issue, pr))
                    heads.append(self.git.head(train))
                else:
                    self.git.merge_abort(train)
                    self.log(f"merge batch: {self.what(t.key)} left out, it conflicts with the batch")
                if not members:
                    # The job's own PR (first in line) doesn't fit: merge it alone, which resolves
                    # its conflict. Batching around it would leave it first in line, skipped forever.
                    return "too few"
            if len(members) < 2:
                return "too few"
            good = self.bisect_batch(train, members, heads, slot)
            if good < len(members):
                t, _, pr = members[good]
                self.state.task(t.key)["batch_culprit"] = pr["headRefOid"]
                self.state.save()
                self.log(f"merge batch: {self.what(t.key)} PR #{pr['number']} fails the gate "
                         f"{'on its own' if good == 0 else 'on top of the PRs before it'}; it merges alone")
                if good == 0:
                    return "gate failed"
                members = members[:good]
                self.git.git("reset", "--hard", heads[good - 1], cwd=train)
            prs = ", ".join(f"#{pr['number']}" for _, _, pr in members)
            tree = self.git.rev(train, "HEAD^{tree}")
            self.merging.update(t.key for t, _, _ in members)
            merged = []
            for t, issue, pr in members:
                if not self.squash_merge(t, pr):
                    self.log(f"merge batch: GitHub won't merge PR #{pr['number']} yet; the rest wait for the next merge")
                    break
                merged.append((t, issue, pr))
            self.git.fetch()
            if len(merged) == len(members) and self.git.rev(train, f"{self.base_ref()}^{{tree}}") != tree:
                self.log(f"merge batch {prs}: WARNING {self.base_ref()} doesn't match the gated tree; "
                         "the next gate on it will tell")
            # Each PR is already merged on GitHub: one failing follow-up mustn't skip the rest.
            failed = None
            for t, issue, pr in merged:
                try:
                    self.finish_merge(t, issue, pr)
                except Exception as e:
                    self.log(f"merge batch: finishing PR #{pr['number']} ({t.key}) failed: {str(e)[-300:]}")
                    failed = failed or e
            if failed:
                raise failed
            return "merged" if merged else "too few"
        finally:
            self.merging.difference_update(t.key for t, _, _ in order)
            self.git.worktree_remove(train)

    def bisect_batch(self, train: Path, members: list, heads: list[str], slot: str) -> int:
        """How many of ``members``, from the front, pass the gate together (all of them if the
        whole batch passes). Binary search over prefixes, assuming the base itself passes: a
        bad PR costs about log2(n) extra gates instead of n single merges, and a failing gate
        is usually quick (it stops at the first failing check)."""
        if self.gate_batch(train, members, slot):
            return len(members)
        good, bad = 0, len(members)  # prefix `good` passes (the base), prefix `bad` fails
        while bad - good > 1:
            mid = (good + bad) // 2
            self.git.git("reset", "--hard", heads[mid - 1], cwd=train)
            if self.gate_batch(train, members[:mid], slot):
                good = mid
            else:
                bad = mid
        return good

    def gate_batch(self, train: Path, members: list, slot: str) -> bool:
        prs = ", ".join(f"#{pr['number']}" for _, _, pr in members)
        self.log(f"phase merge batch: test gate on {len(members)} PR(s) together ({prs})")
        gate = self.run_gate(train / self.t.project_dir, slot, self.t.test, self.t.test_timeout)
        if not gate.ok:
            out = self.logs / f"merge-batch-{'-'.join(str(pr['number']) for _, _, pr in members)}.log"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(gate.tail(20000), encoding="utf-8")
            self.log(f"merge batch {prs}: the gate fails (output in {out.name})")
        return gate.ok

    def squash_merge(self, t: Task, pr: dict) -> bool:
        """Squash-merge ``pr`` at its approved head, retrying while GitHub is still computing
        mergeability. False if it still refuses."""
        for attempt in range(MERGE_ATTEMPTS):
            if attempt:
                time.sleep(MERGE_RETRY_SECONDS)
            try:
                self.gh.merge_pr(pr["number"], pr["headRefOid"])
                return True
            except proc.CommandError as e:
                if "not mergeable" not in str(e).lower():
                    raise
                self.log(f"merge {self.what(t.key)} PR #{pr['number']}: not mergeable yet (attempt {attempt + 1}); retrying")
        return False

    def merge_one(self, t: Task, issue: int, pr: dict, slot: str) -> None:
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
                # The gate runs just below in any case, so merge_base needn't run it.
                base = self.git.rev(wt, self.base_ref())
                m = self.merge_base(wt, slot, gate=False)
                if m == "conflict":
                    self.git.merge_abort(wt)
                    return self.merge_conflict(issue, pr, f"The approved branch no longer merges cleanly "
                                                          f"with `{self.base_ref()}`.")
                gated = self.state.task(t.key).get("gated") or {}
                if m == "clean" and gated.get("head") == pr["headRefOid"] and gated.get("base") == base:
                    # The review's gate ran on exactly this merge (same head, same base): a second
                    # gate would prove nothing new.
                    self.log(f"phase {t.key}: no gate needed ({self.base_ref()} unchanged since the review's gate)")
                    gate = proc.Result(0, "", "")
                else:
                    self.log(f"phase {t.key}: test gate (after merging {self.base_ref()})")
                    gate = self.run_gate(wt / self.t.project_dir, slot, self.t.test, self.t.test_timeout)
                if not gate.ok and m == "auto":
                    # The dispatcher's own resolution broke it: nobody's fault, no round used.
                    self.git.git("reset", "--hard", pr["headRefOid"], cwd=wt)
                    return self.merge_conflict(issue, pr, f"Merging `{self.base_ref()}` conflicts; keeping "
                                                          f"both sides fails `{self.t.test}`.")
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
                self.log(f"merge {self.what(t.key)} PR #{pr['number']}: not mergeable yet (attempt {attempt + 1}); retrying")
        else:
            return self.merge_conflict(issue, pr, f"GitHub still reports the branch as not mergeable after "
                                                  f"{MERGE_ATTEMPTS} attempts to bring it up to date with "
                                                  f"`{self.base_ref()}`; rebase it onto `{self.base_ref()}` "
                                                  f"and resolve the conflicts.")
        self.finish_merge(t, issue, pr)

    def finish_merge(self, t: Task, issue: int, pr: dict) -> None:
        """After a PR merged: close its issues, release what waited on it, tick tasks.md."""
        wt = self.wt(t)
        tasks = self.batch(t)
        self.log(f"merged PR #{pr['number']}: {', '.join(self.what(x.key) for x in tasks)}")
        self.gh.set_status(issue, Status.FINISHED, Status.APPROVED)
        self.gh.close_issue(issue)  # "Closes #n" normally does this; harmless if already closed
        for k, n in self.state.task(t.key).get("batch_issues", {}).items():
            self.gh.set_status(n, Status.FINISHED, Status.BLOCKED_BY_TASK)
            self.gh.close_issue(n)
        for n in [issue, *self.state.task(t.key).get("batch_issues", {}).values()]:
            self.release_waiters(n)
        if t.change != TICKET_CHANGE:
            self.commit_to_base(tasks, f"Tick {', '.join(x.key for x in tasks)} in tasks.md")
            self.archive_if_finished(t.change)
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
        with self.base_lock:
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

    def archive_if_finished(self, change: str) -> None:
        """Archive ``change`` once every task of it is ticked on the base branch.

        `openspec archive` merges the change's spec deltas into openspec/specs/ and moves the
        change under changes/archive/, so the main specs describe what was actually built
        instead of the knowledge staying spread over finished changes. A change with a task
        left (a play or deferred one, say) stays open. When the archive fails, typically
        because a delta no longer applies to the main specs, a ticket asks for it to be fixed.
        """
        if not self.t.archive:
            return
        rel = self.t.changes_dir / change / "tasks.md"
        with self.base_lock:
            for _ in range(3):
                self.refresh_base()
                if not (self.base_wt / rel).exists() or not milestones.change_done(self.base_wt, change):
                    return
                cmd = self.t.archive.replace("{change}", change)
                r = proc.run(cmd, self.base_wt, timeout=600, check=False, shell=True)
                if not r.ok or (self.base_wt / rel).exists():
                    self.log(f"archiving finished change {change} failed: {r.tail(500)}")
                    break
                self.git.git("add", "-A", "--", "openspec", cwd=self.base_wt)
                self.git.git("commit", "-m", f"Archive {change}: every task is done; its specs are synced "
                                             "into openspec/specs", cwd=self.base_wt)
                if self.git.git("push", "origin", f"HEAD:refs/heads/{self.t.base}", cwd=self.base_wt, check=False).ok:
                    self.log(f"archived {change}: its specs are synced into openspec/specs")
                    return
            else:
                self.log(f"could not push the archive of {change}; run `{self.t.archive.replace('{change}', change)}` by hand")
                return
        # The archive command failed; the ticket is filed outside the lock.
        self.file_tickets("the pipeline's archive step", [{
            "kind": "defect", "title": f"Archive the finished change {change}",
            "body": (f"Every task of `{change}` is ticked, but `{cmd}` at the repo root "
                     f"failed, so its spec deltas never reached `openspec/specs/`:"
                     + fence(r.tail(3000)) +
                     "\nFix what the output names (usually a delta whose MODIFIED or "
                     "REMOVED requirement doesn't match the main spec), run the command, "
                     "and commit the result.")}])

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
            test = self.t.release_test or self.t.test
            gate = self.run_gate(pdir, RELEASE_KEY, test, self.t.test_timeout)
            if not gate.ok:
                return self.release_failed(m, sha, f"`{test}` fails on the merge:" + fence(gate.tail(4000)))
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
