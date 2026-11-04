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
                        ("watch", "live view of the dispatcher and what each running agent is doing"),
                        ("denials", "review tool calls agents were refused, grouped, with the reason")):
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
        if name == "start":
            p.add_argument("--no-watch", action="store_true", help="don't open a watcher window")
        if name == "stop":
            p.add_argument("--now", action="store_true", help="kill it and its agents instead of waiting")
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
            if already:
                print(f"{target.name}: dispatcher already running (pid {pid})")
            else:
                pid = service.start(target.name, target.work_dir, args.config.resolve())
                print(f"{target.name}: dispatcher started hidden (pid {pid})")
                print(f"  console output and crashes: {target.work_dir / 'logs' / 'dispatcher.out'}")
            if not args.no_watch:
                wpid = service.open_watcher(target.name, args.config.resolve())
                print(f"  watcher opened in its own window (pid {wpid})" if wpid else
                      f"  follow it with: agentpipe watch {target.name}")
            return 1 if already else 0
        if not pid:
            print(f"{target.name}: no dispatcher running")
            return 1
        if args.now:
            service.kill(pid)
            print(f"{target.name}: killed dispatcher {pid} and its agents")
        else:
            service.request_stop(target.work_dir)
            print(f"{target.name}: stop requested; dispatcher {pid} exits once running agents finish "
                  f"(watch shows progress; --now kills it)")
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
        from agentpipe import watch
        try:
            if args.follow:
                watch.follow(target.work_dir, target.project_dir, args.follow)
            else:
                watch.watch(target.name, target.work_dir, target.project_dir,
                            interval=args.interval, events=args.events, once=args.once)
        except KeyboardInterrupt:
            pass
        return 0

    if args.cmd == "run":
        if args.implementers is not None:
            cfg.max_implementers = args.implementers
        if args.reviewers is not None:
            cfg.max_reviewers = args.reviewers
        Pipeline(cfg, target).run_forever(once=args.once)
        return 0
    return 1


def print_plan(tasks, plan: Plan) -> None:
    for t in sorted((t for t in tasks if not t.done), key=lambda t: key_sort(t.key)):
        d = plan.get(t.key)
        flag = "" if d.autonomous else f"  [needs decision: {d.human_reason}]"
        flag += f"  [batch {d.batch}]" if d.batch else ""
        blockers = ", ".join(d.blocked_by) or "-"
        print(f"{t.key:40} <- {blockers}{flag}")
