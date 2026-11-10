"""Run headless Claude Code (`claude -p`) with structured output."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

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


class Claude:
    def __init__(self, bin: str, log_dir: Path, timeout: int, run=proc.run, on_denials=None,
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

    def run(self, prompt: str, cwd: Path, agent: Agent, schema: dict, *, log_name: str,
            resume: str | None = None, env=None) -> ClaudeResult:
        # stream-json (needs --verbose) is the only output that carries the usage windows.
        cmd = [self.bin, "-p", "--output-format", "stream-json", "--verbose", "--model", agent.model,
               "--permission-mode", "dontAsk", "--max-turns", str(agent.max_turns),
               "--json-schema", json.dumps(schema)]
        if resume:
            cmd += ["--resume", resume]
        if self.mcp_config:
            cmd += ["--strict-mcp-config", "--mcp-config", str(self.mcp_config)]
        if agent.deny:
            cmd += ["--disallowedTools", *agent.deny]
        if agent.tools:
            cmd += ["--allowedTools", *agent.tools]
        if self.guard:
            env = self.guard(env)
        # The prompt goes on stdin: --allowedTools is variadic and would swallow it.
        r = self._run(cmd, cwd, input=prompt, env=env, timeout=self.timeout, check=False)
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
