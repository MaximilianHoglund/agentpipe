"""Scan agentpipe stream-json logs for failed tool calls and group them."""
import json, sys, re, collections, pathlib

root = pathlib.Path(sys.argv[1])
errors = []  # (file, tool, input_summary, error_text)
tool_counts = collections.Counter()
runs = 0
for f in sorted(root.glob("*.out.json")):
    runs += 1
    pending = {}
    for line in f.open(encoding="utf-8", errors="replace"):
        try:
            ev = json.loads(line)
        except Exception:
            continue
        msg = ev.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for c in content:
            if c.get("type") == "tool_use":
                tool_counts[c["name"]] += 1
                inp = c.get("input", {})
                s = inp.get("command") or inp.get("file_path") or json.dumps(inp)[:200]
                pending[c["id"]] = (c["name"], s, inp)
            elif c.get("type") == "tool_result" and c.get("is_error"):
                t = c.get("content")
                if isinstance(t, list):
                    t = " ".join(x.get("text", "") for x in t if isinstance(x, dict))
                name, s, inp = pending.get(c.get("tool_use_id"), ("?", "", {}))
                errors.append((f.name, name, s, str(t)))

def cat(name, s, t):
    tl = t.lower()
    if "permission" in tl and ("denied" in tl or "deny" in tl): return "denied"
    if "string to replace not found" in tl or "old_string" in tl: return "edit-nomatch"
    if "found" in tl and "matches of the string" in tl: return "edit-multimatch"
    if "has not been read" in tl or "read it first" in tl or "file has been modified since" in tl or "modified since read" in tl: return "edit-stale/unread"
    if re.search(r"python[0-9.]*\b.*(not found|not recognized|was not found)|no module named|python was not found", tl): return "python-missing"
    if "command not found" in tl or "is not recognized" in tl: return "cmd-not-found"
    if "exit code" in tl: return f"{name}-exitcode"
    if "file does not exist" in tl or "no such file" in tl: return "no-such-file"
    if "exceeds maximum" in tl or "too large" in tl or "token" in tl and "exceed" in tl: return "too-large"
    if "parsererror" in tl or "unexpected token" in tl or "syntax error" in tl: return "shell-syntax"
    return f"{name}-other"

groups = collections.defaultdict(list)
for e in errors:
    groups[cat(e[1], e[2], e[3])].append(e)

print(f"runs={runs} tool_calls={sum(tool_counts.values())} errors={len(errors)}")
print("tools:", tool_counts.most_common(12))
for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1])):
    print(f"\n=== {k}: {len(v)} (in {len({x[0] for x in v})} runs)")
    for e in v[:int(sys.argv[2]) if len(sys.argv) > 2 else 4]:
        print(f"  [{e[0]}] {e[1]}: {e[2][:160]!r}\n     -> {e[3][:260]!r}")
