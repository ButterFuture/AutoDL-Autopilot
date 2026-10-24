"""The local state: grants, the bill cache and the event log, in one JSON file under the state directory.

Every change happens inside `with State() as st:` which holds an exclusive lock on a separate lock file (fcntl on
POSIX, msvcrt on Windows) and writes the file atomically on a clean exit. Readers take the same lock, so two AI
sessions on one computer never interleave their changes.

  grants: {instance: {"modes": "gpu"|"nogpu"|"both", "budget_fen": int|None, "at": unix, "said": str}}
  bills:  {instance: {serial: {"at": unix, "fen": int, "kind": str, "gpu": bool, "to": unix|None}}}
  events: [{"at": unix, "instance": str, "event": str, ...}]   (the newest MAX_EVENTS)
  instances: {instance: {"guard": true|false}}   per-instance settings that override config.json
"""
from __future__ import annotations

import json
import os
import time

from . import paths

VERSION = 1
MAX_EVENTS = 1000
LOCK_TIMEOUT_S = 30


def empty() -> dict:
    return {"version": VERSION, "grants": {}, "bills": {}, "events": [], "instances": {}}


class StateError(Exception):
    pass


class _Lock:
    def __init__(self, path):
        self.path = path
        self.fd = None

    def __enter__(self):
        paths.private_dir(self.path.parent)
        self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    raise StateError(f"another Autopilot command holds {self.path} for more than {LOCK_TIMEOUT_S}s")
                time.sleep(0.1)

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(self.fd, 0, 0)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
        return False


class State:
    def __init__(self):
        self.dir = paths.state_dir()
        self.file = self.dir / "state.json"
        self.lock = _Lock(self.dir / "state.lock")
        self.data = None

    def __enter__(self):
        self.lock.__enter__()
        try:
            if self.file.exists():
                data = json.loads(self.file.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("version") != VERSION:
                    raise StateError(f"{self.file} is not a state file of this version; move it aside to start anew")
                for key, value in empty().items():
                    data.setdefault(key, value)
            else:
                data = empty()
            self.data = data
            self._before = json.dumps(data, sort_keys=True)
        except BaseException:
            self.lock.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, *rest):
        try:
            if exc_type is None and json.dumps(self.data, sort_keys=True) != self._before:
                self.data["events"] = self.data["events"][-MAX_EVENTS:]
                paths.write_private(self.file, json.dumps(self.data, ensure_ascii=False, indent=1))
        finally:
            self.lock.__exit__(None, None, None)
        return False

    # ---- helpers ----

    def event(self, iid: str, event: str, at: int | None = None, **fields) -> None:
        self.data["events"].append({"at": int(at if at is not None else time.time()), "instance": iid,
                                    "event": event, **fields})

    def add_bills(self, rows: list) -> int:
        """Cache charge rows (console.charges) by serial; returns how many were new."""
        added = 0
        for r in rows:
            book = self.data["bills"].setdefault(r["instance"], {})
            if r["serial"] not in book:
                book[r["serial"]] = {k: r[k] for k in ("at", "fen", "kind", "gpu", "to")}
                added += 1
        return added
