"""Review verdicts and blocking findings from agentpipe review logs."""
import json, sys, re, pathlib, collections, datetime as dt

logs = pathlib.Path(sys.argv[1]); since = dt.datetime.fromisoformat(sys.argv[2])
dump = sys.argv[3] if len(sys.argv) > 3 else None
rows = []
for f in sorted(logs.glob("*-review-*.out.json"), key=lambda p: p.stat().st_mtime):
    if dt.datetime.fromtimestamp(f.stat().st_mtime) < since: continue
    out = None
    for line in f.open(encoding="utf-8", errors="replace"):
        if '"StructuredOutput"' not in line: continue
        try: ev = json.loads(line)
        except Exception: continue
        for c in (ev.get("message") or {}).get("content", []) or []:
            if c.get("type") == "tool_use" and c.get("name") == "StructuredOutput":
                out = c["input"]
    if not out: continue
    key = re.sub(r"-review-.*", "", f.name)
    rows.append((key, out))

verdicts = collections.Counter(o.get("verdict") for _, o in rows)
print("reviews:", len(rows), dict(verdicts))
by_key = collections.defaultdict(list)
for k, o in rows: by_key[k].append(o.get("verdict"))
first = collections.Counter(v[0] for v in by_key.values())
print("first-round verdict per PR:", dict(first), " PRs:", len(by_key))
cats = collections.Counter(); per_reject = []
for k, o in rows:
    if o.get("verdict") == "approve": continue
    b = [x for x in o.get("findings", []) if x.get("severity") == "blocking"]
    per_reject.append(len(b))
    for x in b: cats[x.get("category")] += 1
print("blocking findings by category:", cats.most_common())
print("blocking findings per rejection:", collections.Counter(per_reject).most_common())
if dump:
    for k, o in rows:
        for x in o.get("findings", []):
            if x.get("severity") == "blocking" and (dump == "all" or x.get("category") == dump):
                print(f"\n[{k}] {x.get('category')}: " + str(x.get("problem") or x)[:400])

