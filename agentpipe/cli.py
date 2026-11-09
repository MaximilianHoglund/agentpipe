"""agentpipe <command> <target>: plan, sync, status or run the pipeline for one project."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from agentpipe import config as config_mod
from agentpipe.deps import Plan, key_sort
from agentpipe.github import DryRunGitHub
from agentpipe.pipeline import Pipeline
from agentpipe.status import Status, status_from_labels
from agentpipe.tasks import fingerprint

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "pipeline.toml"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentpipe", description=__doc__)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("targets", help="list configured targets")
    for name, help_ in (("plan", "infer the dependency plan and print it (no GitHub writes)"),
                        ("sync", "create/update issues, statuses and blocked-by links"),
                        ("status", "show the queue by status"),
                        ("run", "run the dispatcher: implement, review, merge"),
                        ("start", "start the dispatcher hidden and open a watcher window"),
                        ("stop", "stop the background dispatcher once running agents finish"),
                        ("resume", "clear a usage-limit pause, e.g. after a plan upgrade"),
                        ("watch", "live view of the dispatcher and what each running agent is doing"),
                        ("denials", "review tool calls agents were refused, grouped, with the reason"),
                        ("wait", "park a decided issue until other issues or tasks are done, then release it"),
                        ("rounds-reset", "give a PR out of review rounds a fresh set and send it back for revision")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("target")
        if name == "plan":
            p.add_argument("--force", action="store_true", help="re-plan every open task from scratch")
        if name == "sync":
            p.add_argument("--dry-run", action="store_true", help="print GitHub writes instead of making them")
        if name == "run":
            p.add_argument("--implementers", type=int)
            p.add_argument("--reviewers", type=int)
            p.add_argument("--once", action="store_true", help="exit when no job is running")
        if name == "watch":
            p.add_argument("--follow", metavar="KEY", help="stream one task's agent in full, e.g. project-foundation/1.1")
            p.add_argument("--events", type=int, default=8, help="recent events shown per agent")
            p.add_argument("--interval", type=float, default=2.0, help="seconds between refreshes")
            p.add_argument("--once", action="store_true", help="print one snapshot and exit")
            p.add_argument("--plain", action="store_true", help="redrawn text view instead of panes")
            p.add_argument("--linked", action="store_true",
                           help="closing this watcher stops the dispatcher (start opens one like this)")
        if name == "start":
            p.add_argument("--no-watch", action="store_true", help="don't open a watcher window")
        if name == "stop":
            p.add_argument("--now", action="store_true", help="kill it and its agents instead of waiting")
        if name == "wait":
            p.add_argument("issue", type=int, help="the issue that waits")
            p.add_argument("blockers", nargs="+",
                           help="what it waits for: issue numbers (#316 or 316) or task keys (crafting/3.5)")
        if name == "rounds-reset":
            p.add_argument("issue", type=int, help="the task's issue (its PR is found from it)")
            p.add_argument("--why", default="", help="note for the PR, e.g. what the revision should fix")
        if name == "denials":
            p.add_argument("--hours", type=float, help="only runs from the last N hours")
            p.add_argument("--examples", type=int, default=2, help="example commands per group")
    args = ap.parse_args(argv)
    cfg = config_mod.load(args.config)

    if args.cmd == "targets":
        for name, t in cfg.targets.items():
            print(f"{name:12} {t.github_repo:40} {t.repo_root}  changes={t.changes_spec}")
        return 0
    if args.target not in cfg.targets:
        print(f"unknown target {args.target!r}; configured: {', '.join(cfg.targets)}", file=sys.stderr)
        return 2
    target = cfg.targets[args.target]
    if args.cmd in ("run", "start", "sync") and not cfg.runs_here(target.name):
        print(f"{target.name} doesn't run on this machine ({cfg.machine}); its targets: "
              f"{', '.join(cfg.machine_targets or []) or 'none'}. A target runs on one machine at a "
              f"time: change [machines.{cfg.machine}] targets in pipeline.toml to move it.", file=sys.stderr)
        return 2

    if args.cmd == "plan":
        pl = Pipeline(cfg, target)
        pl.refresh_base()
        tasks = pl.load_tasks()
        fp = fingerprint(tasks) + ":" + ",".join(sorted({t.change for t in tasks}))
        plan = Plan.load(pl.plan_path)
        if args.force or plan is None or plan.fingerprint != fp:
            plan = pl.make_plan(tasks, fp, None if args.force else plan)
            plan.save(pl.plan_path)
        print_plan(tasks, plan)
        print(f"\nsaved to {pl.plan_path}")
        return 0

    if args.cmd == "sync":
        gh = DryRunGitHub(target.github_repo, cfg.gh_bin, cwd=target.repo_root) if args.dry_run else None
        pl = Pipeline(cfg, target, gh=gh)
        if args.dry_run:
            pl.state.save = lambda: None  # fake issue numbers must never reach state.json
        else:
            pl.gh.ensure_labels()
        actions = pl.sync()
        print(f"{len(actions)} change(s)" + (" (dry run)" if args.dry_run else ""))
        return 0

    if args.cmd == "status":
        pl = Pipeline(cfg, target)
        pl.refresh_base()
        pl.tasks = {t.key: t for t in pl.load_tasks()}
        groups: dict[str, list[str]] = defaultdict(list)
        for key, i in pl.issues_by_key(pl.gh.list_issues()).items():
            s = Status.FINISHED if i["state"].upper() == "CLOSED" else status_from_labels(i["labels"])
            groups[s.value if s else "unlabelled"].append(f"#{i['number']} {key}")
        for s in [x.value for x in Status] + ["unlabelled"]:
            if groups.get(s):
                items = sorted(groups[s], key=lambda x: key_sort(x.split(" ", 1)[1]))
                print(f"{s} ({len(items)})")
                for line in items if s != Status.FINISHED.value else items[-5:]:
                    print(f"  {line}")
        return 0

    if args.cmd in ("start", "stop"):
        from agentpipe import service
        pid = service.running_pid(target.work_dir)
        if args.cmd == "start":
            already = pid is not None
            stop = target.work_dir / "stop"
            if already and stop.exists():
                stop.unlink(missing_ok=True)
                print(f"{target.name}: dispatcher {pid} was stopping; stop cancelled, it keeps running")
            elif already:
                print(f"{target.name}: dispatcher already running (pid {pid})")
            else:
                pid = service.start(target.name, target.work_dir, args.config.resolve())
                print(f"{target.name}: dispatcher started hidden (pid {pid})")
                print(f"  console output and crashes: {target.work_dir / 'logs' / 'dispatcher.out'}")
            if not args.no_watch and service.live_watchers(target.work_dir):
                print("  a watcher window is already open; not opening another")
            elif not args.no_watch:
                wpid = service.open_watcher(target.name, args.config.resolve())
                print(f"  watcher opened in its own window (pid {wpid})" if wpid else
                      f"  follow it with: agentpipe watch {target.name}")
            return 1 if already else 0
        if not pid:
            print(f"{target.name}: no dispatcher running")
            return 1
        if args.now:
            from agentpipe import handoff
            old = list(handoff.live(target.work_dir, pid))  # handed over, still finishing jobs
            for p in [pid, *old]:
                service.kill(p)
                handoff.remove(target.work_dir, p)
            print(f"{target.name}: killed dispatcher {', '.join(map(str, [pid, *old]))} and its agents")
        else:
            service.request_stop(target.work_dir)
            print(f"{target.name}: stop requested; dispatcher {pid} exits once running agents finish "
                  f"(watch shows progress; --now kills it)")
        return 0

    if args.cmd == "resume":
        from agentpipe import service
        from agentpipe.pipeline import State, clear_pause
        if pid := service.running_pid(target.work_dir):
            # The running dispatcher owns state.json in memory; it picks this up next tick.
            (target.work_dir / "resume").write_text("", encoding="utf-8")
            print(f"{target.name}: resume requested; dispatcher {pid} clears its pause on its next tick")
        else:
            clear_pause(State(target.work_dir / "state.json"))
            print(f"{target.name}: usage pause cleared")
        return 0

    if args.cmd == "rounds-reset":
        # GitHub writes only, so it is safe while the dispatcher runs.
        from agentpipe import review
        from agentpipe.github import GitHub
        from agentpipe.status import task_key_from_body
        gh = GitHub(target.github_repo, cfg.gh_bin, cwd=target.repo_root)
        issue = gh.issue(args.issue)
        key = task_key_from_body(issue.get("body"))
        pr = next((p for p in gh.list_prs() if key and task_key_from_body(p["body"]) == key), None)
        if pr is None:
            print(f"#{args.issue}: no open pipeline PR found for it", file=sys.stderr)
            return 1
        gh.comment(pr["number"], review.bot(
            f"**Fresh review rounds.** The owner reset the round count; earlier reviews stay as "
            f"feedback. {args.why}".strip() + f"\n\n{review.ROUNDS_RESET_MARKER}"))
        gh.set_status(args.issue, Status.CHANGES_REQUESTED, status_from_labels(issue["labels"]))
        print(f"#{args.issue} ({key}): PR #{pr['number']} has fresh rounds; sent back for revision")
        return 0

    if args.cmd == "wait":
        # GitHub writes only, no state.json, so it is safe while the dispatcher runs.
        pl = Pipeline(cfg, target)
        issues = None
        blockers = []
        for b in args.blockers:
            b = b.lstrip("#")
            if b.isdigit():
                blockers.append(int(b))
                continue
            if issues is None:  # task keys need the task list
                pl.refresh_base()
                pl.tasks = {t.key: t for t in pl.load_tasks()}
                issues = pl.issues_by_key(pl.gh.list_issues())
            if b in issues:
                blockers.append(issues[b]["number"])
            else:
                print(f"{b}: no issue for this task key. Wait only for a defined task: add it to tasks.md "
                      f"and run `agentpipe sync {target.name}` first.", file=sys.stderr)
                return 2
        if args.issue in blockers:
            print(f"#{args.issue} can't wait for itself", file=sys.stderr)
            return 2
        waiter = pl.gh.issue(args.issue)
        if waiter["state"].upper() != "OPEN":
            print(f"#{args.issue} is closed", file=sys.stderr)
            return 2
        pl.register_wait(args.issue, blockers, status_from_labels(waiter["labels"]))
        return 0

    if args.cmd == "denials":
        from agentpipe import denials
        since = __import__("time").time() - args.hours * 3600 if args.hours else 0.0
        # Every agent shares [permissions] deny; per-agent extras are unioned in.
        deny = sorted({r for a in cfg.agents.values() for r in a.deny})
        allowed = {t for a in cfg.agents.values() for t in a.tools if "(" not in t}
        print(denials.report(denials.scan(target.work_dir / "logs", deny, since, allowed), examples=args.examples))
        return 0

    if args.cmd == "watch":
        from agentpipe import service, watch
        if args.linked and not args.once and not args.follow:
            service.register_watcher(target.work_dir)
        try:
            if args.follow:
                watch.follow(target.work_dir, target.project_dir, args.follow)
            elif not args.once and not args.plain:
                from agentpipe import panes
                panes.run(target.name, target.work_dir, target.project_dir, interval=args.interval,
                          linked=args.linked)
            else:
                watch.watch(target.name, target.work_dir, target.project_dir,
                            interval=args.interval, events=args.events, once=args.once,
                            linked=args.linked)
        except KeyboardInterrupt:
            pass
        return 0

    if args.cmd == "run":
        overrides = {k: v for k, v in (("max_implementers", args.implementers),
                                       ("max_reviewers", args.reviewers)) if v is not None}
        pl = Pipeline(cfg, target)
        pl.watch_config(args.config, overrides)  # pipeline.toml edits apply without a restart
        pl.run_forever(once=args.once)
        return 0
    return 1


def print_plan(tasks, plan: Plan) -> None:
    for t in sorted((t for t in tasks if not t.done), key=lambda t: key_sort(t.key)):
        d = plan.get(t.key)
        flag = "" if d.autonomous else f"  [needs decision: {d.human_reason}]"
        flag += f"  [batch {d.batch}]" if d.batch else ""
        blockers = ", ".join(d.blocked_by) or "-"
        print(f"{t.key:40} <- {blockers}{flag}")
