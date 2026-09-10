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
    src_begin: int = 0   # 0-based line index where the cell's source starts
    src_end: int = 0     # 0-based exclusive end (next marker or EOF)
    marker_line: int = -1  # 0-based line of this cell's "# %%" (-1 = implicit preamble)


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
        # keep explicitly-marked cells even when empty (freshly inserted cells);
        # only drop the implicit preamble when it has nothing in it
        if mark_i == -1 and not source.strip():
            continue
        h = hashlib.sha1(source.encode()).hexdigest()[:8]
        occ = seen.get(h, 0)
        seen[h] = occ + 1
        cells.append(Cell(id=f"{h}-{occ}", idx=len(cells), title=title,
                          source=source, lineno=begin + 1,
                          src_begin=begin, src_end=end, marker_line=mark_i))
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


def _eol(text: str) -> str:
    return "\n" if text.endswith("\n") or not text else ""


def delete_cell(text: str, cell_id: str) -> str:
    """Remove a cell entirely (its marker line through its source)."""
    lines = text.splitlines()
    for c in parse_cells(text):
        if c.id == cell_id:
            start = c.marker_line if c.marker_line >= 0 else c.src_begin
            out = lines[:start] + lines[c.src_end:]
            return "\n".join(out) + _eol(text)
    raise KeyError(cell_id)


def insert_cell(text: str, ref_id: str, where: str = "below", title: str = "") -> str:
    """Insert a new empty '# %%' cell above/below the referenced cell.
    Returns (new_text). A bare ref of '' appends at end of file."""
    lines = text.splitlines()
    block = [f"# %% {title}".rstrip(), ""]
    cells = parse_cells(text)
    if not ref_id or not cells:
        at = len(lines)
        pad = [""] if lines and lines[-1].strip() else []
        out = lines + pad + block
        return "\n".join(out) + "\n"
    for c in cells:
        if c.id == ref_id:
            if where == "above":
                at = c.marker_line if c.marker_line >= 0 else c.src_begin
            else:  # below
                at = c.src_end
            out = lines[:at] + block + lines[at:]
            return "\n".join(out) + _eol(text)
    raise KeyError(ref_id)
