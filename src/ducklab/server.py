"""ducklab server: a workspace of .py files, one kernel per file, live cells.

- Serves a workspace directory; every .py in it is browsable and each opened
  file gets its own Session (kernel + cells + outputs) — the isolation model.
- Watches the workspace; edits to an open file reparse and push its cells
  (render-reactive; execution stays an explicit trigger).
- Agent-facing HTTP API to list/run/read cells, and a terminal that auto-starts
  the configured agent with context.
- Binds to 127.0.0.1 by default (see docs 0001 §5.7 before exposing anything).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import functools
import subprocess
import sys as _sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from ptyprocess import PtyProcessUnicode
from watchfiles import awatch

from .cells import delete_cell, insert_cell, parse_cells, replace_cell_source
from .kernel import FileKernel

STATIC = Path(__file__).parent / "static"
GLOBAL_CONFIG = Path.home() / ".ducklab.json"
GLOBAL_PROMPTS = Path.home() / ".ducklab" / "prompts"
PROMPTS_SUBDIR = ".ducklab/prompts"
SKIP_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".git", ".ipynb_checkpoints"}

DEFAULT_PROMPT = (
    "ducklab pairing session. The file {file} is served as live notebook "
    "cells at {url} — every edit you make to it re-renders in the browser "
    "instantly (cells split by '# %%'). You are the researcher pairing on "
    "this file: read it first, keep the '# %%' cell structure, prefer small "
    "incremental edits the human can watch land. "
    "To EXECUTE cells use the HTTP API (cells run on the file's own kernel; "
    "state persists across runs): "
    "list: curl -s {url}/api/cells?file={rel} ; "
    "run one (blocks, returns outputs): curl -s -XPOST '{url}/api/run/<idx>?file={rel}' ; "
    "run all: curl -s -XPOST '{url}/api/run_all?file={rel}' ; "
    "read last outputs: curl -s '{url}/api/outputs/<idx>?file={rel}' . "
    "Images render in the browser and appear elided in the API. "
    "Do NOT run cells or edit the file until the human asks — start by "
    "reading the file and briefly saying what you see."
)


# ---------------------------------------------------------------- config ----
def load_global_config() -> dict:
    if GLOBAL_CONFIG.exists():
        try:
            return json.loads(GLOBAL_CONFIG.read_text())
        except Exception:
            pass
    return {}


def save_global_config(cfg: dict) -> None:
    GLOBAL_CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


def load_dir_config(dirpath: Path) -> dict:
    """Per-directory settings (.ducklab.json): terminal agent + context prompt."""
    cfg = {"agent": "claude", "prompt_template": DEFAULT_PROMPT}
    p = dirpath / ".ducklab.json"
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text()))
        except Exception:
            pass
    return cfg


def save_dir_config(dirpath: Path, cfg: dict) -> None:
    (dirpath / ".ducklab.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


@functools.lru_cache(maxsize=64)
def _pyver(python: str) -> str:
    try:
        return subprocess.check_output([python, "-V"], text=True, timeout=5).strip()
    except Exception:
        return "?"


def _as_python(path: Path) -> Path | None:
    """A path to an env dir or interpreter -> the interpreter, if it exists."""
    if path.is_dir():
        for cand in (path / "bin" / "python", path / ".venv" / "bin" / "python"):
            if cand.exists():
                return cand
        return None
    return path if path.exists() else None


def effective_env(dirpath: Path, filename: str) -> str | None:
    """Per-file env override, else directory env, else None (server default)."""
    cfg = load_dir_config(dirpath)
    over = (cfg.get("files") or {}).get(filename) or {}
    return over.get("env") or cfg.get("env") or None


def effective_config(dirpath: Path, filename: str) -> tuple[dict, bool]:
    """Directory defaults with a per-file override layered on top.
    Returns (effective, has_file_override)."""
    cfg = load_dir_config(dirpath)
    over = (cfg.get("files") or {}).get(filename) or {}
    eff = {**cfg, **{k: v for k, v in over.items() if v}}
    return eff, bool(over)


def build_term_parts(cfg: dict, file: Path, rel: str, host: str, port: int) -> tuple[str, str | None] | None:
    """(agent, rendered prompt or None); None when no agent configured."""
    agent = (cfg.get("agent") or "").strip()
    if not agent:
        return None
    tmpl = cfg.get("prompt_template") or ""
    if not tmpl.strip():
        return (agent, None)
    prompt = (tmpl.replace("{file}", str(file))
                  .replace("{url}", f"http://{host}:{port}")
                  .replace("{rel}", rel))
    return (agent, prompt)


def build_term_cmd(cfg: dict, file: Path, rel: str, host: str, port: int) -> str | None:
    parts = build_term_parts(cfg, file, rel, host, port)
    if parts is None:
        return None
    agent, prompt = parts
    return f"{agent} {shlex.quote(prompt)}" if prompt else agent


# --------------------------------------------------------------- presets ----
def _preset_label(text: str, name: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip() or name
        if s:
            break
    return name


def list_presets(root: Path) -> list[dict]:
    """Named prompt snippets from <workspace>/.ducklab/prompts/*.md and
    ~/.ducklab/prompts/*.md. A workspace preset shadows a global one by name."""
    out: dict[str, dict] = {}
    for base, source in ((GLOBAL_PROMPTS, "global"), (root / PROMPTS_SUBDIR, "workspace")):
        if base.is_dir():
            for p in sorted(base.glob("*.md")):
                text = p.read_text()
                out[p.stem] = {"name": p.stem, "label": _preset_label(text, p.stem),
                               "source": source, "text": text}
    return list(out.values())


def preset_texts(root: Path, names: list[str]) -> list[str]:
    lib = {p["name"]: p["text"] for p in list_presets(root)}
    return [lib[n] for n in names if n in lib]


def effective_presets(dirpath: Path, filename: str) -> list[str]:
    """Selected preset names: file override if it sets `presets`, else dir."""
    cfg = load_dir_config(dirpath)
    over = (cfg.get("files") or {}).get(filename) or {}
    return over["presets"] if "presets" in over else (cfg.get("presets") or [])


def compose_prompt_text(root: Path, cfg: dict, dirpath: Path, filename: str,
                        file: Path, rel: str, host: str, port: int) -> str | None:
    """The rendered prompt string (base prompt ++ imported presets), or None."""
    def render(t: str) -> str:
        return (t.replace("{file}", str(file)).replace("{url}", f"http://{host}:{port}")
                 .replace("{rel}", rel))
    parts = []
    base = cfg.get("prompt_template") or ""
    if base.strip():
        parts.append(render(base))
    for txt in preset_texts(root, effective_presets(dirpath, filename)):
        if txt.strip():
            parts.append(render(txt))
    return "\n\n".join(parts) if parts else None


def compose_prompt(root: Path, cfg: dict, dirpath: Path, filename: str,
                   file: Path, rel: str, host: str, port: int) -> str | None:
    """Full terminal launch line: agent + shlex-quoted prompt (or bare agent)."""
    agent = (cfg.get("agent") or "").strip()
    if not agent:
        return None
    prompt = compose_prompt_text(root, cfg, dirpath, filename, file, rel, host, port)
    return f"{agent} {shlex.quote(prompt)}" if prompt else agent



# --------------------------------------------------------------- session ----
class Session:
    """One open file: kernel, parsed cells, kept outputs, connected clients."""

    def __init__(self, file: Path, rel: str, python: str | None = None):
        self.file = file
        self.rel = rel
        self.python = python          # interpreter for this file's kernel (None = server's)
        self.kernel: FileKernel | None = None
        self.cells = parse_cells(file.read_text())
        self.outputs: dict[str, list[dict]] = {}
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.last_run_at: float | None = None
        self.run_count = 0
        # one worker thread per session: runs are FIFO-ordered and the kernel
        # is only ever created/used from this thread (no creation race)
        self.executor = ThreadPoolExecutor(max_workers=1)

    async def send_all(self, msg: dict):
        dead = []
        for ws in self.clients:
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def send_threadsafe(self, msg: dict):
        if self.loop:
            asyncio.run_coroutine_threadsafe(self.send_all(msg), self.loop)

    def ensure_kernel(self) -> FileKernel:
        if self.kernel is None:
            self.kernel = FileKernel(self.python) if self.python else FileKernel()
        return self.kernel

    def submit_run(self, cell_id: str):
        """Enqueue a run on the session's FIFO worker, telling clients the cell
        is queued the moment it is enqueued -- a run waiting behind a long cell
        was previously indistinguishable from one that never started."""
        self.send_threadsafe({"type": "status", "state": "queued", "cell": cell_id})
        return self.executor.submit(self.run_cell_blocking, cell_id)

    def run_cell_blocking(self, cell_id: str):
        cell = next((c for c in self.cells if c.id == cell_id), None)
        if cell is None:
            return
        self.outputs[cell_id] = []
        self.send_threadsafe({"type": "clear", "cell": cell_id})
        self.send_threadsafe({"type": "status", "state": "busy", "cell": cell_id})

        def on_output(out: dict):
            self.outputs.setdefault(cell_id, []).append(out)
            self.send_threadsafe({"type": "output", "cell": cell_id, "out": out})

        self.last_run_at = time.time()
        self.run_count += 1
        try:
            self.ensure_kernel().execute(cell.source, on_output)
        except Exception as e:  # surface infra failures as a cell error, never swallow
            on_output({"kind": "error", "data": f"[ducklab] {type(e).__name__}: {e}"})
        finally:
            self.send_threadsafe({"type": "status", "state": "idle", "cell": cell_id})

    def reparse(self):
        self.cells = parse_cells(self.file.read_text())
        live = {c.id for c in self.cells}
        self.outputs = {k: v for k, v in self.outputs.items() if k in live}

    def find_cell(self, ref: str):
        for c in self.cells:
            if c.id == ref or str(c.idx) == ref:
                return c
        return None

    def outputs_view(self, cell_id: str) -> list[dict]:
        """Outputs with image payloads elided — agents read text, not base64."""
        out = []
        for o in self.outputs.get(cell_id, []):
            if o["kind"] == "image":
                out.append({"kind": "image",
                            "data": f"<png rendered in browser, {len(o['data'])} b64 chars>"})
            else:
                out.append(o)
        return out

    def cells_msg(self) -> dict:
        return {"type": "cells", "file": self.rel,
                "cells": [{"id": c.id, "idx": c.idx, "title": c.title,
                           "source": c.source, "lineno": c.lineno} for c in self.cells]}

    def shutdown(self):
        if self.kernel:
            self.kernel.shutdown()


# --------------------------------------------------------------- terminal ----
class TermSession:
    """A persistent pty for one file, managed like a kernel: it outlives the
    browser (refresh = detach, not kill) and dies only on explicit close or
    server exit. New clients replay a tail buffer and reattach to the SAME
    process, so the paired agent's session survives a reload."""

    MAX_BUF = 256 * 1024  # tail of raw output kept for reattach

    def __init__(self, cwd: Path, launch: str | None, loop):
        self.cwd = cwd
        self.loop = loop
        self.clients: set = set()
        self.buf = deque(maxlen=self.MAX_BUF)  # bytes-as-chars tail
        self.cols, self.rows = 80, 24
        shell = os.environ.get("SHELL", "/bin/bash")
        self.pty = PtyProcessUnicode.spawn([shell, "-l"], dimensions=(self.rows, self.cols), cwd=str(cwd))
        self.pid = self.pty.pid
        threading.Thread(target=self._reader, daemon=True).start()
        if launch:
            # start the agent once, after the shell settles
            loop.call_later(0.8, self._write, launch + "\n")

    def _reader(self):
        while True:
            try:
                data = self.pty.read(65536)
            except Exception:
                break
            self.buf.extend(data)
            for ws in list(self.clients):
                asyncio.run_coroutine_threadsafe(self._safe_send(ws, data), self.loop)

    async def _safe_send(self, ws, data):
        try:
            await ws.send_text(data)
        except Exception:
            self.clients.discard(ws)

    def _write(self, s: str):
        try:
            self.pty.write(s)
        except Exception:
            pass

    def snapshot(self) -> str:
        return "".join(self.buf)

    def attach(self, ws):
        self.clients.add(ws)

    def detach(self, ws):
        self.clients.discard(ws)

    def resize(self, rows, cols):
        self.rows, self.cols = rows, cols
        try:
            self.pty.setwinsize(rows, cols)
        except Exception:
            pass

    @property
    def alive(self) -> bool:
        return self.pty.isalive()

    def close(self):
        try:
            self.pty.terminate(force=True)
        except Exception:
            pass


# ------------------------------------------------------------------- hub ----
class Hub:
    """The workspace: lazily opens a Session per .py file."""

    def __init__(self, root: Path, initial: str | None):
        self.root = root
        self.initial = initial
        self.sessions: dict[str, Session] = {}
        self.terms: dict[str, TermSession] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    def resolve(self, rel: str | None) -> str:
        if not rel:
            rel = self.initial
        if not rel:
            files = self.list_files()
            if not files:
                raise FileNotFoundError("workspace has no .py files")
            rel = files[0]
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root) or p.suffix != ".py" or not p.exists():
            raise FileNotFoundError(rel)
        return str(p.relative_to(self.root))

    def session(self, rel: str | None) -> Session:
        rel = self.resolve(rel)
        if rel not in self.sessions:
            file = self.root / rel
            s = Session(file, rel, python=effective_env(file.parent, file.name))
            s.loop = self.loop
            self.sessions[rel] = s
        return self.sessions[rel]

    def list_files(self) -> list[str]:
        out = []
        for p in sorted(self.root.rglob("*.py")):
            parts = p.relative_to(self.root).parts
            if any(seg in SKIP_DIRS or seg.startswith(".") for seg in parts[:-1]):
                continue
            out.append(str(p.relative_to(self.root)))
        return out


# ------------------------------------------------------------------- app ----
def create_app(root: Path, initial: str | None = None, host: str = "127.0.0.1",
               port: int = 8787, term_cmd_override: str | None = None) -> FastAPI:
    app = FastAPI()
    hub = Hub(root, initial)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.on_event("startup")
    async def _startup():
        hub.loop = asyncio.get_running_loop()
        for s in hub.sessions.values():
            s.loop = hub.loop
        asyncio.create_task(_watch())

    async def _watch():
        async for changes in awatch(hub.root):
            touched = set()
            for _, path in changes:
                try:
                    rel = str(Path(path).resolve().relative_to(hub.root))
                except ValueError:
                    continue
                if rel in hub.sessions:
                    touched.add(rel)
            for rel in touched:
                s = hub.sessions[rel]
                try:
                    s.reparse()
                except Exception:
                    continue
                await s.send_all(s.cells_msg())

    @app.get("/")
    async def index():
        return HTMLResponse((STATIC / "index.html").read_text())

    # ---- agent/browser API ------------------------------------------------
    @app.get("/api/files")
    async def api_files():
        return {"root": str(hub.root), "files": hub.list_files(),
                "open": sorted(hub.sessions)}

    @app.post("/api/files/new")
    async def api_file_new(body: dict):
        name = str(body.get("name", "")).strip()
        if not name.endswith(".py"):
            name += ".py"
        p = (hub.root / name).resolve()
        if not p.is_relative_to(hub.root):
            return {"ok": False, "error": "path escapes workspace"}
        if p.exists():
            return {"ok": False, "error": f"{name} already exists"}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# %% {p.stem}\n\n")
        return {"ok": True, "file": str(p.relative_to(hub.root))}

    @app.get("/api/cells")
    async def api_cells(file: str | None = None):
        s = hub.session(file)
        return [{"idx": c.idx, "id": c.id, "title": c.title, "lineno": c.lineno,
                 "has_output": bool(s.outputs.get(c.id))} for c in s.cells]

    @app.post("/api/run/{ref}")
    async def api_run(ref: str, file: str | None = None, wait: bool = True):
        s = hub.session(file)
        cell = s.find_cell(ref)
        if cell is None:
            return {"ok": False, "error": f"no cell {ref!r}; see /api/cells"}
        fut = s.submit_run(cell.id)
        if wait:
            await asyncio.wrap_future(fut)
            return {"ok": True, "cell": cell.idx, "outputs": s.outputs_view(cell.id)}
        return {"ok": True, "cell": cell.idx, "queued": True}

    @app.post("/api/run_all")
    async def api_run_all(file: str | None = None, wait: bool = True):
        s = hub.session(file)
        futs = [(c, s.submit_run(c.id)) for c in s.cells]
        if wait:
            for _, fut in futs:
                await asyncio.wrap_future(fut)
            return {"ok": True, "outputs": {c.idx: s.outputs_view(c.id) for c, _ in futs}}
        return {"ok": True, "queued": len(futs)}

    @app.get("/api/outputs/{ref}")
    async def api_outputs(ref: str, file: str | None = None):
        s = hub.session(file)
        cell = s.find_cell(ref)
        if cell is None:
            return {"ok": False, "error": f"no cell {ref!r}; see /api/cells"}
        return {"ok": True, "cell": cell.idx, "outputs": s.outputs_view(cell.id)}

    def _rss_mb(pid: int | None) -> float | None:
        if not pid:
            return None
        try:
            kb = int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)],
                                             text=True).strip() or 0)
            return round(kb / 1024, 1)
        except Exception:
            return None

    @app.get("/api/kernels")
    async def api_kernels():
        """Which kernels are alive and which file each one belongs to."""
        rows = []
        for rel in sorted(hub.sessions):
            s = hub.sessions[rel]
            k = s.kernel
            rows.append({
                "file": rel, "alive": k is not None,
                "pid": getattr(k, "pid", None) if k else None,
                "rss_mb": _rss_mb(getattr(k, "pid", None)) if k else None,
                "started_at": getattr(k, "started_at", None) if k else None,
                "last_run_at": s.last_run_at, "runs": s.run_count,
                "clients": len(s.clients),
            })
        return {"kernels": rows}

    @app.post("/api/kernels/stop")
    async def api_kernel_stop(body: dict):
        s = hub.session(body.get("file"))
        if s.kernel:
            await asyncio.get_running_loop().run_in_executor(s.executor, s.kernel.shutdown)
            s.kernel = None
        await s.send_all({"type": "status", "state": "stopped"})
        return {"ok": True, "file": s.rel, "note": "cells/outputs kept; next run starts a fresh kernel"}

    @app.post("/api/kernels/restart")
    async def api_kernel_restart(body: dict):
        s = hub.session(body.get("file"))
        if s.kernel:
            await asyncio.get_running_loop().run_in_executor(s.executor, s.kernel.restart)
        await s.send_all({"type": "status", "state": "restarted"})
        return {"ok": True, "file": s.rel}

    @app.get("/api/envs")
    async def api_envs():
        """Known interpreters: server default + auto-discovered .venvs + manual."""
        envs: dict[str, dict] = {}
        def add(python: Path | None, source: str, label: str | None = None):
            if python is None:
                return
            key = str(python)
            if key not in envs:
                envs[key] = {"python": key, "source": source,
                             "label": label or key.replace(str(Path.home()), "~"),
                             "version": _pyver(key)}
        add(Path(_sys.executable), "default", "ducklab server")
        add(_as_python(hub.root / ".venv"), "auto", f"{hub.root.name}/.venv")
        for d in sorted(hub.root.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and d.name not in SKIP_DIRS:
                add(_as_python(d / ".venv"), "auto", f"{d.name}/.venv")
        for p in load_global_config().get("envs", []):
            add(_as_python(Path(p).expanduser()), "manual")
        return {"envs": list(envs.values())}

    @app.post("/api/envs/add")
    async def api_envs_add(body: dict):
        raw = str(body.get("path", "")).strip()
        py = _as_python(Path(raw).expanduser())
        if py is None:
            return {"ok": False, "error": f"no interpreter at {raw!r} (dir with bin/python or a python path)"}
        g = load_global_config()
        lst = g.get("envs", [])
        if str(py) not in lst:
            lst.append(str(py))
        g["envs"] = lst
        save_global_config(g)
        return {"ok": True, "python": str(py), "version": _pyver(str(py))}

    @app.get("/api/env")
    async def api_env_get(file: str | None = None):
        s = hub.session(file)
        return {"file": s.rel, "python": s.python, "version": _pyver(s.python) if s.python else _pyver(_sys.executable),
                "is_default": s.python is None, "kernel_alive": s.kernel is not None}

    @app.post("/api/env")
    async def api_env_set(body: dict):
        s = hub.session(body.get("file"))
        python = body.get("python") or None   # None/"" = server default
        if python:
            py = _as_python(Path(python).expanduser())
            if py is None:
                return {"ok": False, "error": f"no interpreter at {python!r}"}
            python = str(py)
        cur = load_dir_config(s.file.parent)
        files = cur.get("files") or {}
        over = files.get(s.file.name) or {}
        if python:
            over["env"] = python
        else:
            over.pop("env", None)
        if over:
            files[s.file.name] = over
        else:
            files.pop(s.file.name, None)
        cur["files"] = files
        save_dir_config(s.file.parent, cur)
        s.python = python
        if s.kernel:  # restart on next run with the new interpreter
            await asyncio.get_running_loop().run_in_executor(s.executor, s.kernel.shutdown)
            s.kernel = None
        await s.send_all({"type": "status", "state": "env_changed"})
        return {"ok": True, "python": python, "note": "kernel stopped; next run uses the new env"}

    @app.post("/api/presets/select")
    async def api_presets_select(body: dict):
        """Choose which presets a scope imports. scope=dir|file; names=[...]."""
        s = hub.session(body.get("file"))
        names = [str(n) for n in (body.get("names") or [])]
        cur = load_dir_config(s.file.parent)
        if body.get("scope") == "file":
            files = cur.get("files") or {}
            over = files.get(s.file.name) or {}
            over["presets"] = names
            if not over.get("agent") and not over.get("prompt_template") \
               and not over.get("env") and not names:
                files.pop(s.file.name, None)      # nothing left = drop override
            else:
                files[s.file.name] = over
            cur["files"] = files
        else:
            cur["presets"] = names
        save_dir_config(s.file.parent, cur)
        return {"ok": True}

    def _preset_path(name: str, source: str) -> Path:
        base = GLOBAL_PROMPTS if source == "global" else (hub.root / PROMPTS_SUBDIR)
        return base / f"{name}.md"

    @app.get("/api/presets/get")
    async def api_presets_get(name: str, source: str = "workspace"):
        f = _preset_path(name, source)
        if not f.exists():
            return {"ok": False, "error": "not found"}
        return {"ok": True, "name": name, "source": source, "text": f.read_text()}

    @app.post("/api/presets/save")
    async def api_presets_save(body: dict):
        name = str(body.get("name", "")).strip()
        source = body.get("source", "workspace")
        f = _preset_path(name, source)
        if not f.exists():
            return {"ok": False, "error": f"{name} 없음"}
        f.write_text(str(body.get("text", "")))
        return {"ok": True, "name": name, "source": source}

    @app.post("/api/presets/delete")
    async def api_presets_delete(body: dict):
        f = _preset_path(str(body.get("name", "")).strip(), body.get("source", "workspace"))
        if f.exists():
            f.unlink()
        return {"ok": True}

    @app.post("/api/presets/new")
    async def api_presets_new(body: dict):
        name = "".join(c for c in str(body.get("name", "")).strip()
                       if c.isalnum() or c in "-_").strip("-_")
        if not name:
            return {"ok": False, "error": "이름은 영숫자/-/_ 만"}
        scope = body.get("scope", "workspace")
        base = GLOBAL_PROMPTS if scope == "global" else (hub.root / PROMPTS_SUBDIR)
        base.mkdir(parents=True, exist_ok=True)
        f = base / f"{name}.md"
        if f.exists() and not body.get("overwrite"):
            return {"ok": False, "error": f"{name} 이미 있음"}
        f.write_text(str(body.get("text", "")))
        return {"ok": True, "name": name, "source": scope, "path": str(f)}

    @app.get("/api/config")
    async def api_config_get(file: str | None = None):
        s = hub.session(file)
        dir_cfg = load_dir_config(s.file.parent)
        over = (dir_cfg.get("files") or {}).get(s.file.name)
        eff, has_over = effective_config(s.file.parent, s.file.name)
        g = load_global_config()
        return {"dir": {"agent": dir_cfg.get("agent", "claude"),
                        "prompt_template": dir_cfg.get("prompt_template", DEFAULT_PROMPT),
                        "presets": dir_cfg.get("presets") or []},
                "file_override": over,
                "presets_available": [{"name": pp["name"], "label": pp["label"],
                                       "source": pp["source"], "text": pp["text"]}
                                      for pp in list_presets(hub.root)],
                "presets_dir": dir_cfg.get("presets") or [],
                "presets_file": (over or {}).get("presets") if over and "presets" in over else None,
                "agent": eff.get("agent", ""), "prompt_template": eff.get("prompt_template", ""),
                "default_prompt": DEFAULT_PROMPT, "has_file_override": has_over,
                "workspace": g.get("workspace", ""), "root": str(hub.root),
                "effective_cmd": term_cmd_override if term_cmd_override is not None
                else compose_prompt(hub.root, eff, s.file.parent, s.file.name, s.file, s.rel, host, port),
                "overridden_by_cli": term_cmd_override is not None}

    @app.post("/api/config")
    async def api_config_set(cfg: dict):
        s = hub.session(cfg.get("file"))
        cur = load_dir_config(s.file.parent)
        files = cur.get("files") or {}
        if cfg.get("remove_file_override"):
            files.pop(s.file.name, None)
        elif cfg.get("scope") == "file":
            agent = str(cfg.get("agent", "")).strip()
            prompt = str(cfg.get("prompt_template", "")).strip()
            over = files.get(s.file.name) or {}
            over["agent"] = agent; over["prompt_template"] = prompt
            over = {k: v for k, v in over.items() if v or k == "presets"}
            if over.get("agent") or over.get("prompt_template") or over.get("presets") or over.get("env"):
                files[s.file.name] = over
            else:  # nothing left = no override
                files.pop(s.file.name, None)
        else:  # directory defaults — omit values equal to the built-in defaults
            # so a later ducklab upgrade of DEFAULT_PROMPT flows through instead
            # of being frozen by an old save
            agent = str(cfg.get("agent", "claude"))
            prompt = str(cfg.get("prompt_template", DEFAULT_PROMPT))
            cur.pop("agent", None); cur.pop("prompt_template", None)
            if agent and agent != "claude":
                cur["agent"] = agent
            if prompt.strip() and prompt != DEFAULT_PROMPT:
                cur["prompt_template"] = prompt
        cur["files"] = files
        save_dir_config(s.file.parent, cur)
        if "workspace" in cfg:
            g = load_global_config()
            g["workspace"] = str(cfg["workspace"]).strip()
            save_global_config(g)
        return {"ok": True, "applies": "next terminal opened; workspace on next launch"}

    # ---- websockets -------------------------------------------------------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        try:
            session = hub.session(ws.query_params.get("file"))
        except FileNotFoundError as e:
            await ws.send_text(json.dumps({"type": "fatal", "error": str(e)}))
            await ws.close()
            return
        session.clients.add(ws)
        await ws.send_text(json.dumps(session.cells_msg()))
        for cell_id, outs in session.outputs.items():
            for out in outs:
                await ws.send_text(json.dumps({"type": "output", "cell": cell_id, "out": out}))
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                if msg["type"] == "run":
                    session.submit_run(msg["cell"])
                elif msg["type"] == "run_all":
                    for cid in [c.id for c in session.cells]:
                        session.submit_run(cid)
                elif msg["type"] == "interrupt":
                    # SIGINT the kernel: the running cell raises
                    # KeyboardInterrupt and the FIFO drains normally.
                    if session.kernel:
                        session.kernel.interrupt()
                elif msg["type"] == "edit":
                    # replace one cell's source on disk; a browser edit and an
                    # AI's file edit are the same path — the file is the truth
                    try:
                        old = next(c for c in session.cells if c.id == msg["cell"])
                        new_text = replace_cell_source(
                            session.file.read_text(), msg["cell"], msg["source"])
                    except (KeyError, StopIteration):
                        await ws.send_text(json.dumps(
                            {"type": "edit_rejected", "cell": msg["cell"],
                             "reason": "cell changed on disk; re-open it"}))
                        continue
                    session.file.write_text(new_text)
                    session.reparse()
                    await session.send_all(session.cells_msg())
                    if msg.get("run"):
                        cell = next((c for c in session.cells if c.idx == old.idx), None)
                        if cell:
                            session.executor.submit(session.run_cell_blocking, cell.id)
                elif msg["type"] == "insert":
                    try:
                        new_text = insert_cell(session.file.read_text(),
                                               msg.get("cell") or "", msg.get("where", "below"))
                    except KeyError:
                        continue
                    session.file.write_text(new_text)
                    session.reparse()
                    await session.send_all(session.cells_msg())
                elif msg["type"] == "delete":
                    try:
                        new_text = delete_cell(session.file.read_text(), msg["cell"])
                    except KeyError:
                        continue
                    session.file.write_text(new_text)
                    session.reparse()
                    await session.send_all(session.cells_msg())
                elif msg["type"] == "restart":
                    if session.kernel:
                        await asyncio.get_running_loop().run_in_executor(
                            session.executor, session.kernel.restart)
                    await session.send_all({"type": "status", "state": "restarted"})
        except WebSocketDisconnect:
            session.clients.discard(ws)

    def _term_launch(session: Session) -> str | None:
        eff, _ = effective_config(session.file.parent, session.file.name)
        if term_cmd_override is not None:
            return term_cmd_override or None
        agent = (eff.get("agent") or "").strip()
        if not agent:
            return None
        prompt = compose_prompt_text(hub.root, eff, session.file.parent,
                                     session.file.name, session.file, session.rel, host, port)
        if not prompt:
            return agent
        # long prompt via temp file so the typed line stays short (a giant
        # shlex-quoted string typed into a shell corrupts once editing wraps)
        pf = Path(tempfile.gettempdir()) / f"ducklab-prompt-{os.getpid()}-{abs(hash(session.rel))}.txt"
        pf.write_text(prompt)
        return f'{agent} "$(cat {shlex.quote(str(pf))})"'

    @app.get("/api/terminals")
    async def api_terminals():
        rows = []
        for rel in sorted(hub.terms):
            t = hub.terms[rel]
            rows.append({"file": rel, "alive": t.alive, "pid": t.pid,
                         "clients": len(t.clients)})
        return {"terminals": rows}

    @app.post("/api/terminals/close")
    async def api_terminal_close(body: dict):
        s = hub.session(body.get("file"))
        t = hub.terms.pop(s.rel, None)
        if t:
            t.close()
        return {"ok": True, "file": s.rel}

    @app.websocket("/ws/term")
    async def term_endpoint(ws: WebSocket):
        """Attach to the file's persistent pty (created on first attach). A
        refresh detaches without killing it; only an explicit close ends it.
        Arbitrary code execution — never expose beyond localhost/tailnet
        (docs 0001 §5.7)."""
        await ws.accept()
        try:
            session = hub.session(ws.query_params.get("file"))
        except FileNotFoundError:
            await ws.close()
            return
        term = hub.terms.get(session.rel)
        if term is None or not term.alive:
            term = TermSession(session.file.parent, _term_launch(session),
                               asyncio.get_running_loop())
            hub.terms[session.rel] = term
        # replay the tail so the reloaded page shows the ongoing session
        snap = term.snapshot()
        if snap:
            await ws.send_text(snap)
        term.attach(ws)
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                if msg["type"] == "in":
                    term._write(msg["data"])
                elif msg["type"] == "resize":
                    term.resize(msg["rows"], msg["cols"])
        except WebSocketDisconnect:
            pass
        finally:
            term.detach(ws)   # keep the pty alive across the refresh

    return app


# ------------------------------------------------------------------ main ----
def main():
    ap = argparse.ArgumentParser(prog="ducklab")
    ap.add_argument("path", nargs="?", default=None,
                    help="a .py file or a workspace directory "
                         "(default: the configured workspace, else the cwd)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--term-cmd", default=None,
                    help='command auto-typed when a terminal opens '
                         '(default: configured agent with file context; "" disables)')
    args = ap.parse_args()

    if args.path is None:
        args.path = load_global_config().get("workspace") or "."
    p = Path(args.path).expanduser().resolve()
    if p.is_dir():
        root, initial = p, None
    elif p.exists():
        root, initial = p.parent, p.name
    else:
        raise SystemExit(f"no such path: {p}")

    url = f"http://{args.host}:{args.port}"
    print(f"ducklab · workspace {root}" + (f" · {initial}" if initial else "") + f" · {url}")
    uvicorn.run(create_app(root, initial, host=args.host, port=args.port,
                           term_cmd_override=args.term_cmd),
                host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
