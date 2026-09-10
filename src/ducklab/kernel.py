"""One kernel per file (the ducklab isolation model).

The kernel is a stock IPython kernel driven over the Jupyter protocol via
jupyter_client; outputs come back tagged with the request's msg_id, which the
caller maps to a cell. Environment resolution (uv, PEP 723) lands post-M0 —
M0 launches the kernel on a chosen interpreter (default: the server's own).
"""

from __future__ import annotations

import queue
import re
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable

from jupyter_client import KernelManager

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

Output = dict  # {"kind": "stream"|"result"|"image"|"error", "data": str}


def _strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


@dataclass
class FileKernel:
    python: str = field(default_factory=lambda: sys.executable)

    def __post_init__(self):
        self.km = KernelManager()
        self.km.kernel_cmd = [self.python, "-m", "ipykernel_launcher", "-f", "{connection_file}"]
        self.km.start_kernel()
        self.kc = self.km.client()
        self.kc.start_channels()
        self.kc.wait_for_ready(timeout=60)
        self._lock = threading.Lock()  # one execution at a time per kernel
        self.execute("%matplotlib inline", lambda o: None)

    def execute(self, code: str, on_output: Callable[[Output], None]) -> None:
        """Run code, streaming outputs to on_output until the kernel goes idle."""
        with self._lock:
            msg_id = self.kc.execute(code)
            while True:
                try:
                    msg = self.kc.get_iopub_msg(timeout=120)
                except queue.Empty:
                    on_output({"kind": "error", "data": "[ducklab] timeout waiting for kernel"})
                    return
                if msg.get("parent_header", {}).get("msg_id") != msg_id:
                    continue
                t, c = msg["msg_type"], msg["content"]
                if t == "status" and c.get("execution_state") == "idle":
                    return
                if t == "stream":
                    on_output({"kind": "stream", "data": c["text"]})
                elif t in ("execute_result", "display_data"):
                    data = c.get("data", {})
                    if "image/png" in data:
                        on_output({"kind": "image", "data": data["image/png"]})
                    elif "text/plain" in data:
                        on_output({"kind": "result", "data": data["text/plain"]})
                elif t == "error":
                    on_output({"kind": "error", "data": _strip_ansi("\n".join(c["traceback"]))})

    def interrupt(self):
        self.km.interrupt_kernel()

    def restart(self):
        self.km.restart_kernel(now=True)
        self.kc.wait_for_ready(timeout=60)
        self.execute("%matplotlib inline", lambda o: None)

    def shutdown(self):
        try:
            self.kc.stop_channels()
            self.km.shutdown_kernel(now=True)
        except Exception:
            pass
