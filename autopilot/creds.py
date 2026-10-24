"""The console login token (the JWT the AutoDL web console keeps in localStorage.token, valid about 60 days).

Looked up in this order, first hit wins:
  1. the environment variable AUTODL_WEB_JWT
  2. the credentials file <config>/credentials, one line AUTODL_WEB_JWT=<token>; on POSIX it must be 0600
  3. the OS credential store: service autodl-autopilot, account web-jwt (macOS Keychain, Linux secret-tool,
     Windows Credential Manager target autodl-autopilot:web-jwt)

The file comes before the store because a store is often unreadable without a desktop login: the macOS login
keychain refuses SSH sessions and background jobs, libsecret needs an unlocked session bus. A store read that fails
or hangs is skipped after STORE_TIMEOUT seconds. The token itself is never printed or logged.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time

from . import paths

ENV_KEY = "AUTODL_WEB_JWT"
SERVICE, ACCOUNT = "autodl-autopilot", "web-jwt"
STORE_TIMEOUT = 5
RUN = subprocess.run   # replaced in tests


class TokenError(Exception):
    """No usable token: none stored, a credentials file others can read, or one the console refused."""


def credentials_path():
    return paths.config_dir() / "credentials"


def _from_env():
    return os.environ.get(ENV_KEY) or None


def _from_file():
    path = credentials_path()
    if not path.exists():
        return None
    if paths.too_open(path):
        raise TokenError(f"{path} can be read by others: chmod 600 it (it holds the console login)")
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == ENV_KEY and value.strip():
            return value.strip()
    return None


def _from_store():
    if sys.platform == "darwin":
        cmd = ["security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"]
    elif os.name == "nt":
        return _win_read(f"{SERVICE}:{ACCOUNT}")
    elif shutil.which("secret-tool"):
        cmd = ["secret-tool", "lookup", "service", SERVICE, "account", ACCOUNT]
    else:
        return None
    try:
        r = RUN(cmd, capture_output=True, text=True, timeout=STORE_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _win_read(target: str):
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    cred = ctypes.POINTER(CREDENTIAL)()
    if not advapi.CredReadW(target, 1, 0, ctypes.byref(cred)):   # 1: a generic credential
        return None
    try:
        raw = ctypes.string_at(cred.contents.CredentialBlob, cred.contents.CredentialBlobSize)
    finally:
        advapi.CredFree(cred)
    for encoding in ("utf-16-le", "utf-8"):   # cmdkey stores UTF-16-LE, other tools UTF-8
        try:
            text = raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
        if text.startswith("eyJ"):
            return text
    return None


SOURCES = {"env": _from_env, "file": _from_file, "store": _from_store}


def find(source: str = "auto"):
    """(token, where it was found)."""
    order = ["env", "file", "store"] if source == "auto" else [source]
    for name in order:
        token = SOURCES[name]()
        if token:
            return token, name
    raise TokenError(f"no console token found ({', '.join(order)}). Store it once in your own terminal: "
                     f"autopilot token set (it is read from stdin into {credentials_path()})")


def looks_like_token(text: str) -> bool:
    return text.startswith("eyJ") and text.count(".") == 2 and not any(c.isspace() for c in text)


def save(token: str):
    token = token.strip()
    if not looks_like_token(token):
        raise ValueError("that is not the console token (three dot-separated parts starting with eyJ). In the "
                         "logged-in console run copy(localStorage.token) and paste what it copied")
    path = credentials_path()
    paths.write_private(path, f"{ENV_KEY}={token}\n")
    return path


def forget():
    path = credentials_path()
    if path.exists():
        path.unlink()
        return path
    return None


def times(token: str) -> dict:
    """The token's issue and expiry times only: the rest of its payload identifies the user."""
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {"readable": False}
    out = {"readable": True}
    for key in ("iat", "exp"):
        if isinstance(claims.get(key), (int, float)):
            out[key] = int(claims[key])
    if "exp" in out:
        out["days_left"] = round((out["exp"] - time.time()) / 86400, 1)
    return out
