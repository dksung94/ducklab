"""Parse a .py file into `# %%` cells (percent format, jupytext-compatible).

A cell's id is derived from its content hash plus an occurrence counter, so an
unchanged cell keeps its id (and therefore its outputs) across reparses, while
an edited cell gets a new id — its stale outputs simply fall away.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_MARKER = re.compile(r"^#\s*%%(.*)$")


@dataclass
class Cell:
    id: str
    idx: int
    title: str
    source: str
    lineno: int  # 1-based line of the cell's first source line


def parse_cells(text: str) -> list[Cell]:
    lines = text.splitlines()
    # boundaries: list of (marker_lineno or None, title)
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _MARKER.match(line)
        if m:
            starts.append((i, m.group(1).strip()))
    if not starts or starts[0][0] > 0:
        starts.insert(0, (-1, ""))  # implicit preamble cell

    cells: list[Cell] = []
    seen: dict[str, int] = {}
    for n, (mark_i, title) in enumerate(starts):
        begin = mark_i + 1
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        source = "\n".join(lines[begin:end]).strip("\n")
        if not source.strip() and not title:
            continue
        h = hashlib.sha1(source.encode()).hexdigest()[:8]
        occ = seen.get(h, 0)
        seen[h] = occ + 1
        cells.append(Cell(id=f"{h}-{occ}", idx=len(cells), title=title,
                          source=source, lineno=begin + 1))
    return cells
