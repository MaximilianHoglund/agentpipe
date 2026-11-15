#!/usr/bin/env bash
# Count the blocking-finding categories of pipeline reviews posted in a time window, from the
# review comments on a repo's PRs. Used to judge the agent-skills pilot (from 2026-11-15).
#   bash tools/review_categories.sh MaximilianHoglund/arpg-manager 2026-11-01T00:00:00Z 2026-11-15T13:30:00Z
# Arguments: repo, window start, window end (UTC, ISO 8601). Prints reviews counted, then
# "count [category]" lines.
set -euo pipefail
repo="$1"; from="$2"; to="$3"
gh="${GH:-E:/Programs/GitHubCLI/bin/gh.exe}"
"$gh" pr list -R "$repo" --state all --limit 300 --json comments \
    --jq ".[].comments[] | select(.createdAt >= \"$from\" and .createdAt < \"$to\")
          | select(.body | contains(\"pipeline-review\")) | .body | gsub(\"\n\"; \"\u0001\")" |
awk -v RS='\n' '
    { n_reviews++
      if (match($0, /verdict=[a-z_]+/)) verdict[substr($0, RSTART + 8, RLENGTH - 8)]++
      split($0, lines, "\001"); blocking = 0
      for (i = 1; i in lines; i++) {
          l = lines[i]
          if (l ~ /^\*\*Blocking\*\*/) { blocking = 1; continue }
          if (l ~ /^\*\*Nits\*\*/ || l ~ /^<details>/) blocking = 0
          if (blocking && match(l, /^[0-9]+\. \[[a-z-]+\]/)) {
              c = substr(l, RSTART, RLENGTH); sub(/^[0-9]+\. /, "", c); count[c]++
          }
      } }
    END { print n_reviews + 0, "review comments"; for (v in verdict) print verdict[v], "verdict " v
          for (c in count) print count[c], c }' | sort -rn
