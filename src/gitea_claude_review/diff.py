"""Unified diff parsing: which lines of a pull request can carry an inline comment.

Gitea anchors a review comment to a line number in the new file (``new_position``)
or in the old file (``old_position``). Only lines that appear in the diff hunks
can be anchored, so findings are validated against this map before posting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FileLines:
    """Commentable line numbers of one file."""

    new: set[int] = field(default_factory=set)  # added + context lines (new side)
    old: set[int] = field(default_factory=set)  # removed + context lines (old side)


def _strip_prefix(path: str) -> str:
    path = path.strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def parse_unified_diff(text: str) -> dict[str, FileLines]:
    """Map each changed file (new path; old path for deletions) to its commentable lines."""
    files: dict[str, FileLines] = {}
    current: FileLines | None = None
    old_path = new_path = None
    old_line = new_line = 0
    in_hunk = False

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            current, old_path, new_path, in_hunk = None, None, None, False
            continue
        if not in_hunk and raw.startswith("--- "):
            old_path = None if raw[4:].strip() == "/dev/null" else _strip_prefix(raw[4:])
            continue
        if not in_hunk and raw.startswith("+++ "):
            new_path = None if raw[4:].strip() == "/dev/null" else _strip_prefix(raw[4:])
            key = new_path or old_path
            current = files.setdefault(key, FileLines()) if key else None
            continue
        match = _HUNK.match(raw)
        if match:
            old_line = int(match.group(1))
            new_line = int(match.group(3))
            in_hunk = current is not None
            continue
        if not in_hunk or current is None:
            continue
        if raw.startswith("+"):
            current.new.add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            current.old.add(old_line)
            old_line += 1
        elif raw.startswith(" ") or raw == "":
            current.new.add(new_line)
            current.old.add(old_line)
            new_line += 1
            old_line += 1
        elif raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            in_hunk = False
    return files


def changed_files(text: str) -> list[str]:
    return sorted(parse_unified_diff(text).keys())
