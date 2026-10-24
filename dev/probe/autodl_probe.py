#!/usr/bin/env python3
"""autodl_probe — read-only probe of the AutoDL web console's internal API.

Calls a fixed list of read-only endpoints with the console login JWT and prints
each response with secrets masked, so the response shapes can be checked
before the real client is written. It never powers anything on or off, never
changes settings, and has no way to: the endpoint list below is the only one.

Usage:
  python3 -I autodl_probe.py [--uuid INSTANCE_UUID] [--source auto|env|store|file]
                             [--save-raw DIR] [--app-version VER] [--only NAME ...]

JWT lookup (first hit wins with --source auto):
  1. env    AUTODL_WEB_JWT
  2. file   $AUTODL_REMOTE_CONFIG or
            ~/.config/autodl-autopilot/credentials   (macOS/Linux, must be 0600)
            %APPDATA%\\autodl-autopilot\\credentials   (Windows)
            line format: AUTODL_WEB_JWT=<token>
  3. store  OS credential store, service "autodl-autopilot", account "web-jwt"
            macOS: Keychain (security) | Linux: libsecret (secret-tool)
            Windows: Credential Manager, target "autodl-autopilot:web-jwt"
The file comes before the store because the store is not reliably readable
without a desktop login: the macOS login keychain refuses SSH and background
sessions ("User interaction is not allowed"), libsecret needs an unlocked
session bus. A store read that fails or hangs is skipped, never waited on.

Output is meant to be pasted back for review: passwords, tokens, phone numbers,
e-mails, names and SSH commands are masked, lists are cut to 3 items. Full
unmasked responses are written only with --save-raw (dir 0700, files 0600);
keep that dir out of synced folders and never paste it.
"""
import argparse
import base64
import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.autodl.com"
SERVICE, ACCOUNT = "autodl-autopilot", "web-jwt"
WIN_TARGET = f"{SERVICE}:{ACCOUNT}"
ENV_KEY = "AUTODL_WEB_JWT"
STORE_TIMEOUT = 5  # seconds; a store that wants to show a prompt must not stall a headless run

# The whole surface of this script: name -> (method, path, body or query).
# All are reads; the two POSTs are list queries the console itself sends as POST.
ENDPOINTS = {
    "time_sync": ("GET", "/api/v1/time_sync", None),
    "balance": ("GET", "/api/v1/wallet/balance", {"charge_type": "payg", "product_type": "instance"}),
    "instances": ("POST", "/api/v1/instance",
                  {"date_from": "", "date_to": "", "page_index": 1, "page_size": 10,
                   "status": [], "charge_type": []}),
    "detail": ("GET", "/api/v1/instance/detail", {"instance_uuid": "<uuid>"}),
    "bills": ("POST", "/api/v1/bill/balance_statement/list",
              {"date_from": "", "date_to": "", "page_index": 1, "page_size": 10,
               "balance_statement_type": "", "bill_type": ""}),
}

SECRET_KEY = re.compile(
    r"pass|pwd|token|secret|jupyter|ssh_command|private|pub_key|phone|mobile|email|"
    r"id_card|idcard|real_name|username|nickname|user_name|wechat|openid|invite|remittance", re.I)
PHONE = re.compile(r"(?<![0-9A-Za-z])1[3-9]\d{9}(?![0-9A-Za-z])")  # not inside hex ids
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
JWT_LIKE = re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]+")


def die(msg, rc=1):
    print(msg, file=sys.stderr)
    sys.exit(rc)


# ---------------- credential lookup ----------------

def config_path():
    if os.environ.get("AUTODL_REMOTE_CONFIG"):
        return os.path.expanduser(os.environ["AUTODL_REMOTE_CONFIG"])
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), SERVICE, "credentials")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, SERVICE, "credentials")


def from_env():
    return os.environ.get(ENV_KEY) or None


def from_store():
    if sys.platform == "darwin":
        cmd = ["security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"]
    elif os.name == "nt":
        return _win_cred_read(WIN_TARGET)
    elif shutil.which("secret-tool"):
        cmd = ["secret-tool", "lookup", "service", SERVICE, "account", ACCOUNT]
    else:
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=STORE_TIMEOUT,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _win_cred_read(target):
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    pcred = ctypes.POINTER(CREDENTIAL)()
    if not advapi.CredReadW(target, 1, 0, ctypes.byref(pcred)):  # 1 = CRED_TYPE_GENERIC
        return None
    try:
        c = pcred.contents
        raw = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
    finally:
        advapi.CredFree(pcred)
    # cmdkey stores the secret as UTF-16-LE; tools that store bytes use UTF-8.
    for enc in ("utf-16-le", "utf-8"):
        try:
            s = raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
        if s.startswith("eyJ"):
            return s
    return raw.decode("utf-8", "replace").strip() or None


def from_file():
    path = config_path()
    if not os.path.exists(path):
        return None
    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            die(f"{path} is readable by others (mode {mode:o}); run: chmod 600 '{path}'")
    with open(path, encoding="utf-8") as f:
        for line in f:
            k, sep, v = line.strip().partition("=")
            if sep and k.strip() == ENV_KEY and v.strip():
                return v.strip()
    return None


SOURCES = {"env": from_env, "store": from_store, "file": from_file}


def load_jwt(source):
    order = ["env", "file", "store"] if source == "auto" else [source]
    for name in order:
        tok = SOURCES[name]()
        if tok:
            return tok, name
    hint = ("\nThe OS credential store is often unreadable over SSH, from background jobs or with the"
            "\nscreen locked; for unattended use put the JWT in the credentials file instead."
            if "store" in order else "")
    die("No JWT found (tried: %s). See dev/probe/README.md for how to store it.%s" % (", ".join(order), hint))


def jwt_summary(tok):
    """Only the timing claims; the rest of the payload may identify the user."""
    try:
        part = tok.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {"decodable": False}
    out = {"decodable": True, "claim_keys": sorted(claims)}
    for k in ("iat", "nbf", "exp"):
        if isinstance(claims.get(k), (int, float)):
            t = dt.datetime.fromtimestamp(claims[k], dt.timezone(dt.timedelta(hours=8)))
            out[k] = t.strftime("%Y-%m-%d %H:%M:%S +08:00")
    if isinstance(claims.get("exp"), (int, float)):
        out["days_left"] = round((claims["exp"] - dt.datetime.now().timestamp()) / 86400, 1)
    return out


# ---------------- requests ----------------

def call(method, path, payload, tok, app_version):
    url = BASE + path
    data = None
    if method == "GET" and payload:
        url += "?" + urllib.parse.urlencode(payload)
    elif payload is not None:
        data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", tok)
    req.add_header("Content-Type", "application/json;charset=UTF-8")
    req.add_header("User-Agent", "autodl-autopilot-probe/0.1")
    if app_version:
        req.add_header("AppVersion", app_version)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, raw = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return 0, {"code": "NetworkError", "msg": str(e.reason)}
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, {"code": "NotJSON", "msg": mask_str(raw[:300])}


# ---------------- masking ----------------

def mask_str(s):
    s = JWT_LIKE.sub("<jwt>", s)
    s = EMAIL.sub("<email>", s)
    s = PHONE.sub("<phone>", s)
    return s if len(s) <= 200 else s[:200] + f"...<{len(s)} chars>"


def mask(v, key=""):
    if isinstance(v, dict):
        return {k: mask(x, k) for k, x in v.items()}
    if isinstance(v, list):
        out = [mask(x, key) for x in v[:3]]
        if len(v) > 3:
            out.append(f"<{len(v) - 3} more>")
        return out
    if key and SECRET_KEY.search(key) and v not in (None, "", 0, False):
        return f"<masked {type(v).__name__}>"
    if isinstance(v, str):
        return mask_str(v)
    return v


def save_raw(dirpath, name, obj):
    os.makedirs(dirpath, mode=0o700, exist_ok=True)
    p = os.path.join(dirpath, name + ".json")
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def first_uuid(resp):
    lst = ((resp or {}).get("data") or {}).get("list") or []
    return lst[0].get("uuid") if lst and isinstance(lst[0], dict) else None


def main():
    p = argparse.ArgumentParser(description="Read-only probe of the AutoDL console API")
    p.add_argument("--uuid", help="instance uuid for 'detail' (default: first instance listed)")
    p.add_argument("--source", choices=["auto", "env", "store", "file"], default="auto")
    p.add_argument("--save-raw", metavar="DIR", help="also write unmasked responses here (0600)")
    p.add_argument("--app-version", help="send this AppVersion header (the console sends one)")
    p.add_argument("--only", nargs="+", choices=list(ENDPOINTS), help="probe only these")
    args = p.parse_args()

    tok, src = load_jwt(args.source)
    report = {"jwt_source": src, "jwt": jwt_summary(tok), "results": {}}
    instances = None
    for name in args.only or list(ENDPOINTS):
        method, path, payload = ENDPOINTS[name]
        if name == "detail":
            uuid = args.uuid or first_uuid(instances)
            if not uuid:
                report["results"][name] = {"skipped": "no instance uuid (pass --uuid)"}
                continue
            payload = {"instance_uuid": uuid}
        status, body = call(method, path, payload, tok, args.app_version)
        if name == "instances":
            instances = body
        if args.save_raw:
            save_raw(args.save_raw, name, body)
        report["results"][name] = {"request": f"{method} {path}", "http": status,
                                   "code": body.get("code") if isinstance(body, dict) else None,
                                   "body": mask(body)}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
