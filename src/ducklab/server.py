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
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from ptyprocess import PtyProcessUnicode
from watchfiles import awatch

from .cells import parse_cells, replace_cell_source
from .kernel import FileKernel

STATIC = Path(__file__).parent / "static"
GLOBAL_CONFIG = Path.home() / ".ducklab.json"
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
    "Images render in the browser and appear elided in the API."
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


def build_term_cmd(cfg: dict, file: Path, rel: str, host: str, port: int) -> str | None:
    agent = (cfg.get("agent") or "").strip()
    if not agent:
        return None
    tmpl = cfg.get("prompt_template") or ""
    if not tmpl.strip():
        return agent
    prompt = (tmpl.replace("{file}", str(file))
                  .replace("{url}", f"http://{host}:{port}")
                  .replace("{rel}", rel))
    return f"{agent} {shlex.quote(prompt)}"


# --------------------------------------------------------------- session ----
class Session:
    """One open file: kernel, parsed cells, kept outputs, connected clients."""

    def __init__(self, file: Path, rel: str):
        self.file = file
        self.rel = rel
        self.kernel: FileKernel | None = None
        self.cells = parse_cells(file.read_text())
        self.outputs: dict[str, list[dict]] = {}
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
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
            self.kernel = FileKernel()
        return self.kernel

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


# ------------------------------------------------------------------- hub ----
class Hub:
    """The workspace: lazily opens a Session per .py file."""

    def __init__(self, root: Path, initial: str | None):
        self.root = root
        self.initial = initial
        self.sessions: dict[str, Session] = {}
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
            s = Session(self.root / rel, rel)
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
        fut = s.executor.submit(s.run_cell_blocking, cell.id)
        if wait:
            await asyncio.wrap_future(fut)
            return {"ok": True, "cell": cell.idx, "outputs": s.outputs_view(cell.id)}
        return {"ok": True, "cell": cell.idx, "queued": True}

    @app.post("/api/run_all")
    async def api_run_all(file: str | None = None, wait: bool = True):
        s = hub.session(file)
        futs = [(c, s.executor.submit(s.run_cell_blocking, c.id)) for c in s.cells]
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

    @app.get("/api/config")
    async def api_config_get(file: str | None = None):
        s = hub.session(file)
        cfg = load_dir_config(s.file.parent)
        g = load_global_config()
        return {**cfg, "default_prompt": DEFAULT_PROMPT,
                "workspace": g.get("workspace", ""), "root": str(hub.root),
                "effective_cmd": term_cmd_override if term_cmd_override is not None
                else build_term_cmd(cfg, s.file, s.rel, host, port),
                "overridden_by_cli": term_cmd_override is not None}

    @app.post("/api/config")
    async def api_config_set(cfg: dict):
        s = hub.session(cfg.get("file"))
        save_dir_config(s.file.parent,
                        {"agent": str(cfg.get("agent", "claude")),
                         "prompt_template": str(cfg.get("prompt_template", DEFAULT_PROMPT))})
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
                    session.executor.submit(session.run_cell_blocking, msg["cell"])
                elif msg["type"] == "run_all":
                    for cid in [c.id for c in session.cells]:
                        session.executor.submit(session.run_cell_blocking, cid)
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
                elif msg["type"] == "restart":
                    if session.kernel:
                        await asyncio.get_running_loop().run_in_executor(
                            session.executor, session.kernel.restart)
                    await session.send_all({"type": "status", "state": "restarted"})
        except WebSocketDisconnect:
            session.clients.discard(ws)

    @app.websocket("/ws/term")
    async def term_endpoint(ws: WebSocket):
        """A real shell over a pty, one per connection. This is arbitrary code
        execution — never expose beyond localhost/tailnet (docs 0001 §5.7)."""
        await ws.accept()
        try:
            session = hub.session(ws.query_params.get("file"))
        except FileNotFoundError:
            await ws.close()
            return
        loop = asyncio.get_running_loop()
        shell = os.environ.get("SHELL", "/bin/bash")
        pty = PtyProcessUnicode.spawn(
            [shell, "-l"], dimensions=(24, 80), cwd=str(session.file.parent))
        # auto-start the pairing AI (or any command); typed into the shell so
        # it is visible, and the shell remains after it exits. Config is read
        # per-connection, so settings changes apply to the next terminal.
        cmd = term_cmd_override if term_cmd_override is not None else \
            build_term_cmd(load_dir_config(session.file.parent),
                           session.file, session.rel, host, port)
        if cmd:
            pty.write(cmd + "\n")

        def reader():
            while True:
                try:
                    data = pty.read(65536)
                except Exception:
                    break
                asyncio.run_coroutine_threadsafe(ws.send_text(data), loop)
        threading.Thread(target=reader, daemon=True).start()

        try:
            while True:
                msg = json.loads(await ws.receive_text())
                if msg["type"] == "in":
                    pty.write(msg["data"])
                elif msg["type"] == "resize":
                    pty.setwinsize(msg["rows"], msg["cols"])
        except WebSocketDisconnect:
            pass
        finally:
            try:
                pty.terminate(force=True)
            except Exception:
                pass

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
