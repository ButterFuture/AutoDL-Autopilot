"""Where Autopilot keeps its files on this computer.

config: the console token and the settings (key file, work directory, environment setup)
state:  grants, the bill cache, the event log, the SSH known_hosts

  macOS, Linux: ~/.config/autodl-autopilot      ~/.local/state/autodl-autopilot   (XDG variables respected)
  Windows:      %APPDATA%\\autodl-autopilot      %LOCALAPPDATA%\\autodl-autopilot

AUTODL_AUTOPILOT_CONFIG and AUTODL_AUTOPILOT_STATE point elsewhere (tests use them). On POSIX the directories are
made 0700 and the files 0600; on Windows the per-user profile directories already keep them private.
"""
from __future__ import annotations

import os
import pathlib

APP = "autodl-autopilot"
POSIX = os.name != "nt"


def _base(env_override: str, win_var: str, xdg_var: str, xdg_default: str) -> pathlib.Path:
    if os.environ.get(env_override):
        return pathlib.Path(os.environ[env_override]).expanduser()
    if not POSIX:
        root = os.environ.get(win_var) or str(pathlib.Path.home())
        return pathlib.Path(root) / APP
    root = os.environ.get(xdg_var) or str(pathlib.Path.home() / xdg_default)
    return pathlib.Path(root) / APP


def config_dir() -> pathlib.Path:
    return _base("AUTODL_AUTOPILOT_CONFIG", "APPDATA", "XDG_CONFIG_HOME", ".config")


def state_dir() -> pathlib.Path:
    return _base("AUTODL_AUTOPILOT_STATE", "LOCALAPPDATA", "XDG_STATE_HOME", ".local/state")


def private_dir(p: pathlib.Path) -> pathlib.Path:
    """Create p (and parents) and, on POSIX, make it readable by its owner only."""
    p.mkdir(parents=True, exist_ok=True)
    if POSIX:
        os.chmod(p, 0o700)
    return p


def write_private(path: pathlib.Path, text: str) -> None:
    """Write text atomically to path, readable by its owner only."""
    private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    if POSIX:
        os.chmod(path, 0o600)


def too_open(path: pathlib.Path) -> bool:
    """True when a POSIX file can be read by its group or others."""
    return POSIX and bool(path.stat().st_mode & 0o077)
