"""Mechanical help with merge conflicts, so an agent is called only for real ones.

Two pieces:

- ``resolve_additive``: a conflict where both sides only *added* lines at the same spot (a new
  `mod` line, a new decision at the end of a doc, a new test next to another) has an obvious
  resolution: keep both. It needs the diff3 conflict style, which shows the common ancestor's
  lines; an empty ancestor section means nothing was changed or removed, only added.
- ``paths_overlap`` and ``ticket_paths``: which files a task will touch, so the scheduler can
  keep two tasks that edit the same file from running at once.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch

OURS, BASE, THEIRS, END = b"<<<<<<< ", b"||||||| ", b"=======", b">>>>>>> "


def _marker(line: bytes, marker: bytes) -> bool:
    body = line.rstrip(b"\r\n")
    # "=======" stands alone; the others are followed by a label (or nothing).
    return body == marker if marker == THEIRS else (body.startswith(marker) or body == marker.rstrip())


def resolve_additive(text: bytes) -> bytes | None:
    """Resolve every conflict hunk as ours then theirs, or None if any hunk isn't additive.

    A hunk is additive when its ancestor section is empty. Lines theirs adds that ours also
    added (the same import added on both sides) are kept once. Line endings are preserved as
    they are in each side.
    """
    lines = text.splitlines(keepends=True)
    out: list[bytes] = []
    i, found = 0, False
    while i < len(lines):
        if not _marker(lines[i], OURS):
            out.append(lines[i])
            i += 1
            continue
        found = True
        sections: dict[bytes, list[bytes]] = {OURS: [], BASE: [], THEIRS: []}
        cur, i, saw_base = OURS, i + 1, False
        while True:
            if i >= len(lines):
                return None  # unterminated hunk
            line = lines[i]
            if _marker(line, BASE) and cur is OURS:
                cur, saw_base = BASE, True
            elif _marker(line, THEIRS) and cur is not THEIRS:
                cur = THEIRS
            elif _marker(line, END) and cur is THEIRS:
                i += 1
                break
            elif _marker(line, OURS):
                return None  # nested markers: not ours to untangle
            else:
                sections[cur].append(line)
            i += 1
        if not saw_base or any(l.strip() for l in sections[BASE]):
            return None
        ours = sections[OURS]
        seen = {l.rstrip(b"\r\n") for l in ours}
        theirs = [l for l in sections[THEIRS] if l.rstrip(b"\r\n") not in seen or not l.strip()]
        if ours and theirs and not ours[-1].endswith(b"\n"):
            ours = ours[:-1] + [ours[-1] + _eol(theirs[0])]
        out += ours + theirs
    return b"".join(out) if found else None


def _eol(line: bytes) -> bytes:
    return b"\r\n" if line.endswith(b"\r\n") else b"\n"


def normalize(path: str) -> str:
    p = path.strip().strip("`").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def ignored(path: str, globs: list[str]) -> bool:
    """True if ``path`` matches one of ``globs`` (fnmatch; a leading `**/` also matches at the root)."""
    return any(fnmatch(path, g) or (g.startswith("**/") and fnmatch(path, g[3:])) for g in globs)


def paths_overlap(a: str, b: str) -> bool:
    """Same file, or one is a directory holding the other."""
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def overlapping(mine: list[str], theirs: list[str], ignore: list[str]) -> list[str]:
    """Paths of ``mine`` that overlap any of ``theirs``, leaving out ignored paths."""
    mine = [p for p in map(normalize, mine) if p and not ignored(p, ignore)]
    theirs = [p for p in map(normalize, theirs) if p and not ignored(p, ignore)]
    return [p for p in mine if any(paths_overlap(p, q) for q in theirs)]


# A repo path with at least one directory and a file extension, optionally followed by :line.
# The lookbehind keeps URLs out: a path inside one is preceded by `/` or a word character.
_PATH_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]+)(?::\d+)?")


def ticket_paths(body: str) -> list[str]:
    """Files a ticket names (`crates/sim/src/run.rs:120`), as the files it will likely touch."""
    seen: list[str] = []
    for m in _PATH_RE.finditer(body or ""):
        p = normalize(m.group(1))
        if p not in seen:
            seen.append(p)
    return seen
