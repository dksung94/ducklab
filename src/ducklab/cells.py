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
    lineno: int      # 1-based line of the cell's first source line
    src_begin: int = 0  # 0-based line index where the cell's source starts
    src_end: int = 0    # 0-based exclusive end (next marker or EOF)


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
                          source=source, lineno=begin + 1,
                          src_begin=begin, src_end=end))
    return cells


def replace_cell_source(text: str, cell_id: str, new_source: str) -> str:
    """Return the file text with one cell's source replaced (marker preserved).
    Raises KeyError when the id no longer exists (file changed underneath)."""
    lines = text.splitlines()
    for c in parse_cells(text):
        if c.id == cell_id:
            new_lines = new_source.rstrip("\n").splitlines()
            out = lines[: c.src_begin] + new_lines + lines[c.src_end:]
            return "\n".join(out) + ("\n" if text.endswith("\n") else "")
    raise KeyError(cell_id)
