"""ducklab M0 server: one .py file, one kernel, live cells in the browser.

- Serves a single-page UI and a websocket.
- Watches the file; on change the cells are reparsed and pushed (render-reactive,
  execution stays explicit — a run is always a user/AI trigger).
- Executes cells on the file's kernel, streaming outputs tagged per cell.
- Binds to 127.0.0.1 by default (see docs 0001 §5.7 before exposing anything).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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


class Session:
    """State for one served file: kernel, parsed cells, kept outputs, clients."""

    def __init__(self, file: Path):
        self.file = file
        self.kernel: FileKernel | None = None
        self.cells = parse_cells(file.read_text())
        self.outputs: dict[str, list[dict]] = {}
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        # one worker thread per session: runs are FIFO-ordered and the kernel
        # is only ever created/used from this thread (no creation race)
        self.executor = ThreadPoolExecutor(max_workers=1)

    # -- broadcasting ------------------------------------------------------
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

    # -- kernel ------------------------------------------------------------
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

    # -- file watching -----------------------------------------------------
    def reparse(self):
        self.cells = parse_cells(self.file.read_text())
        live = {c.id for c in self.cells}
        self.outputs = {k: v for k, v in self.outputs.items() if k in live}

    def cells_msg(self) -> dict:
        return {"type": "cells", "file": self.file.name,
                "cells": [{"id": c.id, "idx": c.idx, "title": c.title,
                           "source": c.source, "lineno": c.lineno} for c in self.cells]}


def create_app(file: Path, term_cmd: str | None = "claude") -> FastAPI:
    app = FastAPI()
    session = Session(file)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.on_event("startup")
    async def _startup():
        session.loop = asyncio.get_running_loop()
        asyncio.create_task(_watch())

    async def _watch():
        async for _ in awatch(session.file):
            try:
                session.reparse()
            except Exception:
                continue
            await session.send_all(session.cells_msg())

    @app.get("/")
    async def index():
        return HTMLResponse((STATIC / "index.html").read_text())

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
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
                    # replace one cell's source on disk; the browser edit and an
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
        loop = asyncio.get_running_loop()
        shell = os.environ.get("SHELL", "/bin/bash")
        pty = PtyProcessUnicode.spawn(
            [shell, "-l"], dimensions=(24, 80), cwd=str(session.file.parent))
        if term_cmd:
            # auto-start the pairing AI (or any command); typed into the shell
            # so it is visible, and the shell remains after it exits
            pty.write(term_cmd + "\n")

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


def main():
    ap = argparse.ArgumentParser(prog="ducklab")
    ap.add_argument("file", help="the .py file to serve (one file = one analysis = one kernel)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--term-cmd", default="claude",
                    help='command auto-typed when a terminal opens (default: claude; "" disables)')
    args = ap.parse_args()
    file = Path(args.file).resolve()
    if not file.exists():
        raise SystemExit(f"no such file: {file}")
    print(f"ducklab · {file} · http://{args.host}:{args.port}")
    uvicorn.run(create_app(file, term_cmd=args.term_cmd or None), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
