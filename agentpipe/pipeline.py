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
from agentpipe.claude import Claude, ClaudeResult
from agentpipe.config import Agent, Config, Milestone, Target
from agentpipe.deps import PLAN_SCHEMA, Plan, PlanError, key_sort, validate_plan
from agentpipe.git import Git
from agentpipe.github import GitHub
from agentpipe.status import (TASK_MARKER, TICKET_CHANGE, TICKET_MARKER, Status,
                              status_from_labels, task_key_from_body)
from agentpipe.tasks import Task, fingerprint, mark_done, parse_tasks

IMPL_KINDS = {"implement", "revise"}
RELEASE_KEY = "milestone"  # job key and slot of the milestone release job


def fence(text: str) -> str:
    return "\n\n```\n" + text + "\n```"


@dataclass
class Job:
    kind: str
    key: str
    slot: str
    future: Future


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
            os.replace(tmp, self.path)


class Pipeline:
    def __init__(self, cfg: Config, target: Target, gh: GitHub | None = None, log=print):
        self.cfg = cfg
        self.t = target
        self.gh = gh or GitHub(target.github_repo, cfg.gh_bin, cwd=target.repo_root)
        self.git = Git(target.repo_root)
        wd = target.work_dir
        self.base_wt = wd / "base"
        self.logs = wd / "logs"
        self.claude = Claude(cfg.claude_bin, self.logs, cfg.agent_timeout)
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
        return Agent(a.model, a.max_turns, a.tools + self.t.extra_tools)

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

    def make_plan(self, tasks: list[Task], fp: str) -> Plan:
        open_t = [t for t in tasks if not t.done]
        done_t = [t for t in tasks if t.done]
        changes = sorted({t.change for t in tasks})
        prompt = review.prompt(
            "planner",
            repo_root=self.base_wt, target=self.t.name, project_dir=self.t.project_dir,
            context_list="\n".join(f"- `{c}`" for c in self.t.context) or "(no extra context docs)",
            changes_list="\n".join(f"- `{c}`" for c in changes),
            open_tasks="\n".join(f"- {t.key} | {t.section} | {t.text}" for t in open_t) or "(none)",
            done_tasks="\n".join(f"- {t.key} | {t.text}" for t in done_t) or "(none)",
        )
        errors = ""
        for attempt in range(2):
            p = prompt if not errors else prompt + f"\n\nYour previous answer was rejected: {errors}\nFix every problem."
            res = self.claude.run(p, self.base_wt, self.cfg.agents["planner"], PLAN_SCHEMA,
                                  log_name=f"plan-{fp}-{attempt}")
            if res.limited:
                raise LimitHit(res.text)
            if not res.ok:
                errors = f"run failed: {res.text[:500]}"
                continue
            try:
                return validate_plan(tasks, res.structured, fp)
            except PlanError as e:
                errors = str(e)
                self.log(f"plan rejected: {errors}")
        raise PlanError(f"planner failed twice: {errors}")

    def sync(self, gh: GitHub | None = None) -> list:
        gh = gh or self.gh
        self.refresh_base()
        tasks = self.load_tasks()
        fp = fingerprint(tasks) + ":" + ",".join(sorted({t.change for t in tasks}))
        plan = Plan.load(self.plan_path)
        if plan is None or plan.fingerprint != fp:
            self.log(f"planning {sum(not t.done for t in tasks)} open tasks")
            plan = self.make_plan(tasks, fp)
            plan.save(self.plan_path)
        self.tasks = {t.key: t for t in tasks}
        self.plan = plan
        issues = self.issues_by_key(gh.list_issues())
        st = self.state
        actions = syncplan.compute(tasks, plan, issues, set(st.data["flagged"]), set(st.data["cleared"]))
        for a in actions:
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
        for key, issue in issues.items():
            if issue["state"].upper() != "OPEN" or key not in self.tasks:
                continue
            want = sorted(issues[b]["number"] for b in self.plan.get(key).blocked_by if b in issues)
            if st.data["links"].get(key) == want:
                continue
            have = gh.blocked_by(issue["number"])
            for n in set(want) - set(have):
                gh.add_blocked_by(issue["number"], self.db_id(gh, n))
            for n in set(have) - set(want):
                gh.remove_blocked_by(issue["number"], have[n])
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
        if time.time() - self.last_sync >= self.cfg.sync_seconds or not self.tasks:
            try:
                self.sync()
            except LimitHit as e:
                return self.pause(str(e))
            if self.t.milestones and RELEASE_KEY not in self.jobs:
                self.check_milestones()
        issues = {k: i for k, i in self.issues_by_key(self.gh.list_issues()).items()
                  if i["state"].upper() == "OPEN" and k in self.tasks}
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
        until = time.time() + self.cfg.limit_backoff_seconds
        self.state.data["paused_until"] = until
        self.state.save()
        self.log(f"usage limit hit, pausing until {time.strftime('%H:%M', time.localtime(until))}: {why[:200]}")

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

    def run_forever(self, once: bool = False) -> None:
        self.gh.ensure_labels()
        self.log(f"running; implementers={self.cfg.max_implementers} reviewers={self.cfg.max_reviewers}")
        try:
            while True:
                self.tick()
                if once and not self.jobs:
                    break
                time.sleep(self.cfg.poll_seconds if not once else 5)
        except KeyboardInterrupt:
            self.stopping = True
            self.log("stopping: waiting for running agents (Ctrl-C again to abandon; work resumes on restart)")
            self.pool.shutdown(wait=True, cancel_futures=True)

    # ------------------------------------------------------------------ jobs
    def human_input(self, issue: int, pr: dict | None) -> str:
        notes = review.human_comments(self.gh.comments(issue))
        if pr:
            notes += review.human_comments(self.gh.comments(pr["number"]))
        if not notes:
            return ""
        body = "\n\n".join(f"**{c['user']}** ({c['created_at']}):\n{c['body']}" for c in notes)
        return "## Input from humans (authoritative; it overrides the spec where they conflict)\n\n" + body

    def impl_prompt(self, t: Task, issue: int, pr: dict | None) -> str:
        blockers = self.plan.get(t.key).blocked_by if self.plan else []
        return review.prompt(
            "implementer", key=t.key, text=t.text, section=t.section, issue=issue,
            blockers=", ".join(blockers) or "none", worktree=self.wt(t), project_dir=self.t.project_dir,
            spec_ref=self.spec_ref(t), context_list=self.context_list(),
            human_input=self.human_input(issue, pr), test_cmd=self.t.test, open_tickets=self.open_tickets())

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
        res = self.claude.run(prompt, self.wt(t) / self.t.project_dir, self.agent_def("implementer"),
                              review.IMPLEMENTER_SCHEMA, log_name=f"{t.slug}-{tag}-{n}",
                              resume=st.get("session"), env=self.env(slot))
        if res.session_id:
            st["session"] = res.session_id
        self.state.save()
        if res.limited:
            raise LimitHit(res.text)
        return res

    def job_implement(self, t: Task, issue: int, pr: dict | None, slot: str) -> None:
        old = status_from_labels(self.gh_issue_labels(issue))
        wt = self.ensure_worktree(t)
        if old is not Status.IN_PROGRESS:
            self.gh.set_status(issue, Status.IN_PROGRESS, old)
        st = self.state.task(t.key)
        full = self.impl_prompt(t, issue, pr)
        if st.get("session"):
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
        if not self.state.task(t.key).get("session"):
            prompt = self.impl_prompt(t, issue, pr) + "\n\n---\n\n" + prompt
        else:
            prompt += "\n\n" + self.human_input(issue, pr)
        res = self.agent(t, prompt, slot, "revise")
        self.deliver(t, issue, pr, res, slot, Status.CHANGES_REQUESTED)

    def deliver(self, t: Task, issue: int, pr: dict | None, res: ClaudeResult, slot: str,
                cur: Status) -> None:
        """After an implementer turn: check the result, sync with base, push, open the PR."""
        wt = self.wt(t)
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
        body = (f"{TASK_MARKER.format(key=t.key)}\nCloses #{issue}\n\n**{t.key}** — {t.text}\n\n"
                f"## Summary\n\n{out['summary']}\n\n## Tests\n\n{out['tests']}\n")
        if pr is None:
            n = self.gh.create_pr(t.branch, self.t.base, f"{t.key}: {t.text}"[:120], body)
            self.log(f"PR #{n} opened for {t.key}")
        else:
            self.gh.comment(pr["number"], review.bot(f"**Revised.**\n\n{out['summary']}\n\n**Tests:** {out['tests']}"))
        self.gh.set_status(issue, Status.PENDING_APPROVAL, cur)
        st = self.state.task(t.key)
        st.pop("fail_implement", None)
        st.pop("fail_revise", None)
        self.state.save()

    def run_gate(self, cwd: Path, slot: str, cmd: str, timeout: int) -> proc.Result:
        return proc.run(cmd, cwd, env=self.env(slot), timeout=timeout, check=False, shell=True)

    def job_review(self, t: Task, issue: int, pr: dict, slot: str) -> None:
        sha = pr["headRefOid"]
        rv = self.t.work_dir / "review" / t.slug
        self.git.fetch()
        self.git.worktree_detached(rv, sha)
        try:
            pdir = rv / self.t.project_dir
            history = review.review_history(self.gh.comments(pr["number"]))
            round_ = review.rounds_used(history) + 1
            gate = self.run_gate(pdir, slot, self.t.test, self.t.test_timeout)
            if not gate.ok:
                v = {"verdict": review.TESTS_FAILED, "summary": f"`{self.t.test}` failed on this commit.",
                     "findings": [], "probes": [], "details": gate.tail(6000)}
            else:
                v = self.reviewer_verdict(t, pr, rv, pdir, slot, history)
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
            open_tickets=self.open_tickets(),
            head=pr["headRefOid"][:10], project_dir=self.t.project_dir, base_ref=self.base_ref(),
            diffstat=self.git.diff(rv, self.base_ref(), stat=True).strip()[-4000:],
            pr_summary=pr["body"].split("## Summary", 1)[-1].strip()[:6000],
            test_cmd=self.t.test, mutation_section=mutation_section, prior_section=prior,
            context_list=self.context_list())
        for attempt in range(2):
            res = self.claude.run(prompt, pdir, self.agent_def("reviewer"), review.REVIEW_SCHEMA,
                                  log_name=f"{t.slug}-review-{pr['headRefOid'][:8]}-{attempt}",
                                  env=self.env(slot))
            if res.limited:
                raise LimitHit(res.text)
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
        if not self.git.contains(wt, self.base_ref()):
            if not self.git.merge(wt, self.base_ref()):
                self.git.merge_abort(wt)
                v = {"verdict": review.CONFLICT, "findings": [], "probes": [],
                     "summary": f"The approved branch no longer merges cleanly with `{self.base_ref()}`."}
                self.gh.comment(pr["number"], review.render_review(v, pr["headRefOid"], 0, self.cfg.max_rounds))
                self.gh.set_status(issue, Status.CHANGES_REQUESTED, Status.APPROVED)
                return
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
        self.gh.merge_pr(pr["number"], self.git.head(wt))
        self.log(f"merged PR #{pr['number']} ({t.key})")
        self.gh.set_status(issue, Status.FINISHED, Status.APPROVED)
        self.gh.close_issue(issue)  # "Closes #n" normally does this; harmless if already closed
        if t.change != TICKET_CHANGE:
            self.commit_to_base(t, f"Tick {t.key} in tasks.md")
        self.git.worktree_remove(wt)
        self.git.delete_branch(t.branch)
        self.git.delete_remote_branch(t.branch)
        with self.state.lock:
            self.state.data["tasks"].pop(t.key, None)
        self.state.save()
        self.last_sync = 0.0  # dependents may be unblocked now

    def commit_to_base(self, t: Task, message: str) -> None:
        rel = self.t.changes_dir / t.change / "tasks.md"
        for _ in range(3):
            self.refresh_base()
            path = self.base_wt / rel
            text = path.read_text(encoding="utf-8")
            new = mark_done(text, t.id)
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
