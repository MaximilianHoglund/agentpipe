"""Run Claude Code agents through the Claude Agent SDK, with structured output.

The SDK starts the same Claude Code CLI that `claude -p` did (``claude_bin``), so prompts, tools,
settings, skills and transcripts are unchanged. What it adds over the bare CLI are hooks the
dispatcher answers in-process: deletes outside the agent's checkout are denied
(:func:`delete_guard`), and an agent near its turn cap is told to wrap up (:class:`RunState`).
Every stream-json line the CLI prints is still logged to ``logs/<run>.out.json`` as before, since
``denials``, ``tools/friction.py`` and ``tools/reviews.py`` read those files.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

import anyio
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, HookMatcher, ProcessError, query
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from agentpipe import proc
from agentpipe.config import Agent

# Messages that mean "out of quota / capacity", as opposed to a failed task. A match pauses the
# whole dispatcher, so every alternative needs context: a target's own prose ("inventory capacity
# check failed", "fire rate limit of the weapon", "player is overloaded") must not match.
_CODE = r"\b(?:429|529)\b"
LIMIT_RE = re.compile(
    "|".join([
        # Claude Code quota: "Claude usage limit reached", "You've hit your 5-hour limit · resets
        # 11:30pm (...)", "5-hour limit reached ∙ resets 3pm", "Weekly limit reached".
        r"\busage limit",
        r"\b(?:hit|reached|exceeded) your (?:[\w-]+ ){0,3}limit",
        r"\b(?:\d+[- ]hour|hourly|daily|weekly|monthly|session|opus|sonnet|haiku|model) limit "
        r"(?:reached|exceeded|hit)",
        r"\blimit\W{1,5}resets?\b",
        # API rate limiting: "rate limit exceeded", "rate_limit_error", "429 ... rate limit".
        r"\brate_limit",
        r"\brate[ -]?limit(?:s)? (?:exceeded|reached|hit|error)",
        r"\b(?:exceeded|hit|reached) (?:the |your |an? )?(?:api )?rate[ -]?limit",
        r"\b(?:are|been|being|was|were|got) rate[ -]?limited",
        # API overload: "overloaded_error", "API Error: 529 ... Overloaded", "Error: 529 overloaded".
        r"\boverloaded_error",
        r"\bAPI Error\b[^\n]{0,200}\boverloaded",
        r'"message"\s*:\s*"Overloaded',
        _CODE + r"[^\n]{0,200}?(?:rate[ _-]?limit|overloaded|too many requests)",
        r"\bAPI Error:?\s*\(?" + _CODE,
        r"\b(?:HTTP(?:/[\d.]+)?|status(?: code)?)[ :=]{1,3}" + _CODE,
        # Capacity: low-priority capacity notices, "capacity constraints".
        r"\blow[ -]priority\b[^\n]{0,80}\bcapacity|\bcapacity\b[^\n]{0,80}\blow[ -]priority",
        r"\bcapacity constraints?\b",
    ]),
    re.I,
)


# Subtypes meaning the run hit a cap: it is too big or stuck, and retrying would spend as much again.
MAX_TURNS = "error_max_turns"
TIMEOUT = "timeout"
CAPPED = {MAX_TURNS, TIMEOUT}


@dataclass
class ClaudeResult:
    ok: bool
    session_id: str | None
    text: str
    structured: dict | None
    limited: bool
    cost_usd: float = 0.0
    subtype: str = ""
    denials: list[dict] = field(default_factory=list)  # [{tool_name, tool_input, ...}]
    # The account's usage windows as of this run (stream-json `rate_limit_event`), e.g.
    # {"five_hour": {"utilization": 0.82, "resetsAt": 1793797800, "status": "allowed"}, ...}
    rate: dict | None = None


def parse_output(stdout: str, stderr: str, code: int) -> ClaudeResult:
    """Read a run's result from its stdout: stream-json lines, or one plain json object.

    The result is the ``{"type": "result"}`` event, or (plain ``--output-format json``) an object
    with no ``type`` at all. Every stream event has a ``type``, so a run cut short before its
    result (a timeout, a crash) has none, and is reported from stderr and the exit code.
    """
    data = session = rate = None
    events = 0
    plain: list[str] = []  # stdout that is not a JSON event: a crash message, usually
    for line in stdout.strip().splitlines():
        line = line.strip()
        obj = None
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                pass
        if not isinstance(obj, dict):
            if line:
                plain.append(line)
            continue
        events += 1
        if isinstance(obj.get("session_id"), str) and obj["session_id"]:
            session = obj["session_id"]
        kind = obj.get("type")
        if kind == "result":
            data = obj
        elif kind is None and (data is None or data.get("type") != "result"):
            data = obj
        elif kind == "rate_limit_event":
            info = obj.get("rate_limit_info") or {}
            rate = info.get("unifiedWindows") or rate
    if data is None:
        msg = stderr.strip() or "\n".join(plain)
        if code == proc.TIMEOUT_CODE:
            return ClaudeResult(False, session, msg or "timed out", None, False,
                                subtype=TIMEOUT, rate=rate)
        if not msg:
            msg = f"exit {code}" + (f": no result after {events} stream events" if events else "")
        return ClaudeResult(False, session, msg, None, bool(LIMIT_RE.search(msg)), rate=rate)
    text = str(data.get("result", ""))
    is_error = bool(data.get("is_error")) or data.get("subtype") != "success"
    structured = data.get("structured_output")
    limited = is_error and bool(LIMIT_RE.search(text + " " + stderr))
    return ClaudeResult(
        ok=not is_error and structured is not None,
        session_id=data.get("session_id") or session,
        text=text,
        structured=structured,
        limited=limited,
        cost_usd=float(data.get("total_cost_usd") or 0.0),
        subtype=str(data.get("subtype", "")),
        denials=list(data.get("permission_denials") or []),
        rate=rate,
    )


# Where an agent's pushes end up: a URL scheme git has no helper for, so the push fails at once
# ("git: 'remote-no-push' is not a git command") before anything reaches the network.
NO_PUSH_URL = "no-push://the-dispatcher-pushes-for-agents/"
PUSH_PREFIXES = ("https://", "http://", "ssh://", "git://", "git@")
NO_GH_TOKEN = "agentpipe-agents-have-no-gh"


def guard_env(env: dict | None, no_gh_dir: Path) -> dict:
    """``env`` (default: this process's) plus what keeps an agent run off GitHub however it
    phrases the command; the deny rules in pipeline.toml only match command prefixes.

    - gh: an invalid GH_TOKEN (it beats the keyring login: every call gets HTTP 401) and
      GH_CONFIG_DIR at an empty directory (no login at all if only the token is unset).
    - git push: `url.<NO_PUSH_URL>.pushInsteadOf` for every network URL prefix, passed as
      GIT_CONFIG_* variables, rewrites the push URL of any remote or explicit URL. Fetches,
      commits and pushes to a local path (tests that push to a scratch repository) are untouched.
      A remote with its own `pushurl` is not rewritten; agents are denied setting one.
    git itself authenticates through its credential helper, not GH_TOKEN, so fetch keeps working
    (checked 2026-11-10 with Git Credential Manager on the desktop). An agent that unsets these
    variables on purpose gets past them: like the deny rules, this guards against mistakes."""
    env = dict(os.environ if env is None else env)
    no_gh_dir.mkdir(parents=True, exist_ok=True)
    env["GH_TOKEN"] = NO_GH_TOKEN
    env["GH_CONFIG_DIR"] = str(no_gh_dir)
    try:
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        n = 0
    for prefix in PUSH_PREFIXES:
        env[f"GIT_CONFIG_KEY_{n}"] = f"url.{NO_PUSH_URL}.pushInsteadOf"
        env[f"GIT_CONFIG_VALUE_{n}"] = prefix
        n += 1
    env["GIT_CONFIG_COUNT"] = str(n)
    return env


# ------------------------------------------------------------------ deletes outside the checkout

_BASH_DELETE = {"rm", "rmdir", "unlink", "shred"}
_PS_DELETE = {"remove-item", "rm", "rmdir", "del", "erase", "rd", "ri"}
_CD = {"cd", "pushd", "set-location", "sl", "chdir"}
# PowerShell parameters of Remove-Item that take a value which is not a path.
_PS_VALUED = {"-erroraction", "-ea", "-include", "-exclude", "-filter", "-stream", "-credential"}
_PS_PATH = {"-path", "-literalpath", "-lp"}
_SEPARATORS = re.compile(r"&&|\|\||[;|\n]")
_UNKNOWN = re.compile(r"[$%`(){}]")  # variables, subexpressions: can't tell where they point


def checkout_root(cwd: Path) -> Path:
    """The git checkout ``cwd`` is in (a worktree has a ``.git`` file), else ``cwd`` itself."""
    cwd = Path(os.path.abspath(cwd))
    for d in (cwd, *cwd.parents):
        if (d / ".git").exists():
            return d
    return cwd


def _resolve(token: str, cwd: Path | None) -> Path | None:
    """Where a path argument points, or None when that can't be known."""
    if not token or _UNKNOWN.search(token):
        return None
    token = re.split(r"[*?\[]", token, maxsplit=1)[0] or "."  # a glob: the folder it starts in
    if os.name == "nt":
        m = re.match(r"^/([a-zA-Z])(?:/|$)(.*)", token)  # Git Bash: /e/Source -> E:/Source
        if m:
            token = f"{m.group(1).upper()}:/{m.group(2)}"
        elif token.startswith("/"):
            return Path(token)  # /tmp and friends: Git Bash's own tree, never the checkout
    p = Path(os.path.expanduser(token))
    if not p.is_absolute():
        if cwd is None:
            return None
        p = cwd / p
    return Path(os.path.normpath(p))


def _inside(p: Path, root: Path) -> bool:
    a, b = os.path.normcase(str(p)), os.path.normcase(str(root))
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:  # different drives
        return False


def _words(segment: str, posix: bool) -> list[str]:
    try:
        return shlex.split(segment, posix=posix)
    except ValueError:
        return segment.split()


def delete_targets(command: str, tool: str, cwd: Path) -> list[Path]:
    """The paths a shell command deletes that can be resolved, following ``cd`` along the way.

    A heuristic, like the deny rules: it reads rm/rmdir/unlink (Bash) and Remove-Item and its
    aliases (PowerShell), and skips arguments built from variables."""
    ps = tool == "PowerShell"
    here: Path | None = cwd
    out: list[Path] = []
    for segment in _SEPARATORS.split(command):
        words = [w.strip("'\"") if ps else w for w in _words(segment.strip(), posix=not ps)]
        while words and re.match(r"^\w+=", words[0]):  # FOO=1 rm ...
            words = words[1:]
        if words and words[0] in ("sudo", "command", "builtin", "&"):
            words = words[1:]
        if not words:
            continue
        verb = words[0].lower() if ps else words[0]
        if os.path.basename(verb) in _CD or verb in _CD:
            target = words[1] if len(words) > 1 else "~"
            here = _resolve(target, here)
            continue
        if verb not in (_PS_DELETE if ps else _BASH_DELETE):
            continue
        args, skip, opts_done = words[1:], False, False
        for i, w in enumerate(args):
            if skip:
                skip = False
                continue
            lw = w.lower()
            if ps and lw.startswith("-"):
                skip = lw in _PS_VALUED or lw in _PS_PATH
                if lw in _PS_PATH and i + 1 < len(args):
                    out += [p for p in (_resolve(a, here) for a in args[i + 1].split(",")) if p]
                continue
            if not ps and not opts_done and w.startswith("-"):
                opts_done = w == "--"
                continue
            for part in (w.split(",") if ps else [w]):
                if (p := _resolve(part, here)) is not None:
                    out.append(p)
    return out


def delete_guard(command: str, tool: str, cwd: Path, root: Path) -> str | None:
    """Why this command may not run (it deletes outside ``root``), or None."""
    outside = [p for p in delete_targets(command, tool, cwd) if not _inside(p, root)]
    if not outside:
        return None
    return (f"Deleting outside your checkout ({root}) is not allowed: {', '.join(map(str, outside))}. "
            "Delete only inside your working directory; leave scratch in git-ignored folders there.")


# ------------------------------------------------------------------ one run's hooks

WRAP_UP = ("You have used {used} of your {cap} turns; the run stops at {cap}. Stop starting new work. "
           "Commit what you have, then give your final answer in the structured output now. If the "
           "work isn't finished, say exactly what remains (an implementer returns status "
           "\"incomplete\" with notes for the next run), rather than running out of turns.")
# The tool Claude Code gives the agent for its final answer when a run has an output schema.
STRUCTURED_OUTPUT = "StructuredOutput"
LAST_CALL = ("{left} turns left before the run is cut off with nothing reported. Give your final "
             "structured output on your next turn.")


@dataclass
class RunState:
    """What one agent run's hooks share with its message stream."""
    cwd: Path
    root: Path
    cap: int
    wrap_up: int  # turns before the cap at which the agent is told to finish; 0: never
    # Agent.check_report: why the structured output may not go out yet, or None.
    check_report: Callable[[dict], str | None] | None = None
    turns: int = 0
    reports_sent_back: int = 0
    warned: int = 0  # notes sent: 1 the wrap-up, 2 the last call
    hook_denials: list[dict] = field(default_factory=list)
    _ids: set = field(default_factory=set)

    def see(self, msg: dict) -> dict:
        """Counts the agent's own turns (one per API message), and adds the hook's denials to the
        result so `agentpipe denials` lists them beside the deny rules'."""
        kind = msg.get("type")
        if kind == "assistant" and not msg.get("parent_tool_use_id"):
            mid = (msg.get("message") or {}).get("id") or msg.get("uuid")
            if mid not in self._ids:
                self._ids.add(mid)
                self.turns += 1
        elif kind == "result" and self.hook_denials:
            # The CLI lists a hook's denial itself (without saying why); tag that entry, and add
            # any it left out.
            ours = {d["tool_use_id"]: d for d in self.hook_denials}
            listed = []
            for d in msg.get("permission_denials") or []:
                hook = ours.pop(d.get("tool_use_id"), None)
                listed.append({**d, "agentpipe_hook": hook["agentpipe_hook"]} if hook else d)
            msg = {**msg, "permission_denials": [*listed, *ours.values()]}
        return msg

    def note(self) -> str | None:
        """The wrap-up text due after the current tool call, at most once per stage."""
        if not self.wrap_up:
            return None
        left = self.cap - self.turns
        if self.warned < 2 and left <= 2:
            self.warned = 2
            return LAST_CALL.format(left=max(left, 0))
        if self.warned < 1 and left <= self.wrap_up:
            self.warned = 1
            return WRAP_UP.format(used=self.turns, cap=self.cap)
        return None

    def hooks(self) -> dict:
        async def pre(inp, tool_use_id, _ctx):
            tool = inp.get("tool_name", "")
            command = str((inp.get("tool_input") or {}).get("command") or "")
            reason = delete_guard(command, tool, Path(inp.get("cwd") or self.cwd), self.root) if command else None
            if not reason:
                return {}
            self.hook_denials.append({"tool_name": tool, "tool_use_id": tool_use_id,
                                      "tool_input": inp.get("tool_input"), "agentpipe_hook": "delete outside checkout"})
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": reason}}

        async def post(_inp, _tool_use_id, _ctx):
            text = self.note()
            if not text:
                return {}
            return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}

        async def report(inp, tool_use_id, _ctx):
            reason = self.report_gap(inp.get("tool_input") or {})
            if not reason:
                return {}
            self.hook_denials.append({"tool_name": STRUCTURED_OUTPUT, "tool_use_id": tool_use_id,
                                      "tool_input": inp.get("tool_input"), "agentpipe_hook": "report check"})
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": reason}}

        hooks = {"PreToolUse": [HookMatcher(matcher="Bash|PowerShell", hooks=[pre])],
                 "PostToolUse": [HookMatcher(hooks=[post])]}
        if self.check_report:
            hooks["PreToolUse"].append(HookMatcher(matcher=STRUCTURED_OUTPUT, hooks=[report]))
        return hooks

    def report_gap(self, out: dict) -> str | None:
        """Why the agent's final report goes back to it, or None. A denied StructuredOutput call
        keeps the session going with its cache warm (a Stop hook can't: the result is final by
        then, checked 2026-11-16). Once per run, and never near the turn cap, so a run that can't
        satisfy the check still reports rather than being cut off with nothing."""
        if not self.check_report or self.reports_sent_back >= 1 or self.cap - self.turns <= 3:
            return None
        reason = self.check_report(out)
        if reason:
            self.reports_sent_back += 1
        return reason


# ------------------------------------------------------------------ the SDK session

_CONTROL = {"control_request", "control_response", "control_cancel_request"}


class _TeeTransport(SubprocessCLITransport):
    """The SDK's own transport, also handing every raw stream-json message to ``on_message`` (for
    the run's log, unchanged from the `claude -p` days) and able to kill the CLI with everything
    it started (a gate or cargo-mutants run by the agent), which the SDK's terminate doesn't."""

    def __init__(self, prompt, options, on_message: Callable[[dict], None]):
        super().__init__(prompt=prompt, options=options)
        self._on_message = on_message

    async def _tee(self):
        async for data in super().read_messages():
            if data.get("type") not in _CONTROL:
                self._on_message(data)
            yield data

    def read_messages(self):
        return self._tee()

    def kill_tree(self) -> None:
        p = getattr(self, "_process", None)
        if p is None or p.returncode is not None:
            return
        kill_pid_tree(p.pid)


def kill_pid_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        return
    for child in [*_descendants(pid), pid]:
        try:
            os.kill(child, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _descendants(pid: int) -> list[int]:
    """Every process below ``pid``, from /proc (the CLI isn't started in its own process group)."""
    children: dict[int, list[int]] = {}
    for d in Path("/proc").glob("[0-9]*"):
        try:
            ppid = int((d / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(d.name))
    out, todo = [], [pid]
    while todo:
        for c in children.get(todo.pop(), []):
            out.append(c)
            todo.append(c)
    return out


KILL_GRACE = 60  # seconds a killed run gets to wind down before its session is abandoned


def sdk_run(options: ClaudeAgentOptions, *, input: str, timeout: int | None,
            on_message: Callable[[dict], Any] | None = None) -> proc.Result:
    """Run one agent session; the result looks like `proc.run`'s did for `claude -p`: stdout is
    the stream-json lines, a timeout kills the whole process tree and exits TIMEOUT_CODE."""
    lines: list[str] = []
    errs: list[str] = []

    def keep(d: dict) -> None:
        if on_message:
            d = on_message(d) or d
        lines.append(json.dumps(d, ensure_ascii=False))

    options = replace(options, stderr=errs.append)
    code = 0
    timed_out = False

    async def main() -> None:
        nonlocal code, timed_out
        transport = _TeeTransport(input, options, keep)
        async with anyio.create_task_group() as tg:
            async def watchdog() -> None:
                nonlocal timed_out
                await anyio.sleep(timeout)
                timed_out = True
                transport.kill_tree()  # ends the stream; the SDK then closes up
                await anyio.sleep(KILL_GRACE)
                tg.cancel_scope.cancel()

            if timeout:
                tg.start_soon(watchdog)
            try:
                async for _ in query(prompt=input, options=options, transport=transport):
                    pass
            except ProcessError as e:
                code = e.exit_code if e.exit_code not in (None, 0) else 1
            except ClaudeSDKError as e:
                code = 1
                errs.append(str(e))
            finally:
                tg.cancel_scope.cancel()

    anyio.run(main)
    if timed_out:
        return proc.Result(proc.TIMEOUT_CODE, "\n".join(lines), f"timed out after {timeout}s")
    return proc.Result(code, "\n".join(lines), "\n".join(errs))


class Claude:
    def __init__(self, bin: str, log_dir: Path, timeout: int, run=sdk_run, on_denials=None,
                 mcp_config: Path | None = None, on_rate=None, guard=None):
        self.bin = bin
        # Called with each run's env (None: inherit) and returns the env to use: the place for
        # what only agent runs get (see guard_env), since every agent starts through `run`.
        self.guard = guard
        self.on_rate = on_rate  # called with ClaudeResult.rate after a run that reported it
        # Agents get only these MCP servers, not the user's own (connectors, browser, ...):
        # fewer tool definitions per turn, and no mail or drive access for unattended runs.
        self.mcp_config = mcp_config
        self.on_denials = on_denials  # called with (log_name, denials) after a run that had any
        self.log_dir = log_dir
        self.timeout = timeout
        self._run = run

    def options(self, cwd: Path, agent: Agent, schema: dict, resume: str | None, env, hooks) -> ClaudeAgentOptions:
        """What `claude -p` got as flags. The SDK's defaults differ from the CLI's in two places,
        both set back here: no system prompt (the preset is Claude Code's own) and, depending on
        the version, no settings files (agents need the target's CLAUDE.md, permissions and skills)."""
        return ClaudeAgentOptions(
            cli_path=os.path.expanduser(self.bin),
            cwd=str(cwd),
            model=agent.model,
            system_prompt={"type": "preset", "preset": "claude_code"},
            setting_sources=["user", "project", "local"],
            permission_mode="dontAsk",
            max_turns=agent.max_turns,
            output_format={"type": "json_schema", "schema": schema},
            resume=resume,
            mcp_servers=str(self.mcp_config) if self.mcp_config else {},
            strict_mcp_config=bool(self.mcp_config),
            allowed_tools=list(agent.tools),
            disallowed_tools=list(agent.deny),
            env=dict(env) if env else {},  # merged over this process's environment
            hooks=hooks,
        )

    def run(self, prompt: str, cwd: Path, agent: Agent, schema: dict, *, log_name: str,
            resume: str | None = None, env=None) -> ClaudeResult:
        if self.guard:
            env = self.guard(env)
        state = RunState(Path(cwd), checkout_root(Path(cwd)), agent.max_turns, agent.wrap_turns(),
                         check_report=agent.check_report)
        opts = self.options(cwd, agent, schema, resume, env, state.hooks())
        r = self._run(opts, input=prompt, timeout=self.timeout, on_message=state.see)
        # Windows treats ':' in a file name as an alternate data stream; keep names plain.
        log_name = safe_name(log_name)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.log_dir / f"{log_name}.prompt.md").write_text(prompt, encoding="utf-8")
        (self.log_dir / f"{log_name}.out.json").write_text(r.out + "\n" + r.err, encoding="utf-8")
        res = parse_output(r.out, r.err, r.code)
        if res.denials and self.on_denials:
            self.on_denials(log_name, res.denials)
        if res.rate and self.on_rate:
            self.on_rate(res.rate)
        return res


def safe_name(name: str) -> str:
    return re.sub(r"[^\w.-]", "_", name)
