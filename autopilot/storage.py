"""AutoDL file storage (文件存储), as the console's web page uses it.

File storage is per region: once initialized there, every instance of the region mounts it at /root/autodl-fs,
and it can be filled and emptied from this computer while every instance is off. The console lists the regions
and initializes one; each region's own storage server (export_addr_1:port) lists, uploads, downloads and deletes
files. The requests and answers were checked on a real account (dev/probe/FINDINGS.md). Only the requests in
CONSOLE and SERVER are sent. Storage itself cannot be deleted (the console has no such request); emptying it
stops what it costs beyond the free size.

A download link carries its own one-time credential (it works without the login token), so it is never printed.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import posixpath
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import console

CONSOLE = {"list": ("GET", "/api/v1/file_storage/list", False),
           "init": ("POST", "/api/v1/file_storage/init", True)}
SERVER = {"usage": ("GET", "/usage"), "ls": ("GET", "/dir/v2"), "upload": ("POST", "/file"),
          "progress": ("GET", "/file/progress/"), "token": ("POST", "/file/download_token"),
          "download": ("GET", "/download/file/"), "rm_file": ("DELETE", "/file"), "rm_dir": ("DELETE", "/dir")}
CHUNK = 5 * 1024 * 1024          # the web page's chunk size; the server expects it
MD5_HEAD = 1024 * 1024           # the web page sends the MD5 of the first MiB
TIMEOUT_S = 120
TRIES = 3


class StorageError(Exception):
    """The storage server refused or failed a request."""


console.ENDPOINTS.update({"storage_" + k: v for k, v in CONSOLE.items()})


def regions(token: str) -> list:
    """[{sign, name, status, server, free_gb, initialized_at}]; status is created or uninitialized."""
    found = []
    for r in console.request(token, "storage_list") or []:
        server = f"https://{r['export_addr_1']}:{r['port']}/api/v1/file_storage" if r.get("export_addr_1") else None
        found.append({"sign": r.get("sign"), "name": r.get("name"), "status": r.get("status"),
                      "kind": r.get("fs_type"), "server": server,
                      "free_gb": int(r.get("free_size") or 0) // console.GIB,
                      "initialized_at": console.unix(r.get("initialized_at"))})
    return found


def init(token: str, sign: str) -> None:
    """Initialize file storage in the region; sent once."""
    console.request(token, "storage_init", {"sign": sign})


def clean(path: str) -> str:
    """An absolute path on the storage, without . or .. parts."""
    p = posixpath.normpath("/" + (path or "").strip().lstrip("/"))
    if any(part in ("..", ".") for part in p.split("/")):
        raise ValueError(f"path {path!r}: no . or .. parts")
    return p


class Store:
    def __init__(self, token: str, region: dict):
        if region["status"] != "created" or not region["server"]:
            raise StorageError(f"file storage in {region['name']} is {region['status']}: autopilot storage init "
                               f"{region['sign']} first (with the user's agreement)")
        self.token, self.region, self.base = token, region, region["server"]

    def call(self, name: str, suffix: str = "", query: dict | None = None, body: dict | None = None,
             raw: bytes | None = None, ctype: str = "application/json;charset=UTF-8"):
        method, path = SERVER[name]
        url = self.base + path + suffix + ("?" + urllib.parse.urlencode(query) if query else "")
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self.token)
        req.add_header("Content-Type", ctype)
        req.add_header("User-Agent", "autodl-autopilot")
        try:
            with console.URLOPEN(req, timeout=TIMEOUT_S) as r:
                text = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            full = {501: "the net disk is full", 507: "the file storage is full"}.get(e.code, "")
            raise StorageError(f"{name}: HTTP {e.code} {full}".strip()) from None
        except (urllib.error.URLError, OSError) as e:
            raise console.NoAnswer(f"storage {name}: no answer ({getattr(e, 'reason', e)})") from None
        try:
            answer = json.loads(text)
        except json.JSONDecodeError:
            raise console.NoAnswer(f"storage {name}: the answer is not JSON") from None
        if answer.get("code") != "Success":
            raise StorageError(f"{name}: {answer.get('code')} {answer.get('msg', '')}".strip())
        return answer.get("data")

    def usage(self) -> dict:
        u = self.call("usage") or {}
        used, total = int(u.get("current_usage") or 0), int(u.get("total") or 0)
        return {"used_bytes": used, "total_bytes": total, "free_bytes": max(0, total - used),
                "used_gb": round(used / console.GIB, 2), "total_gb": round(total / console.GIB, 2)}

    def ls(self, path: str = "/") -> list:
        """[{name, dir, size, updated_at}] in path ([] for a missing folder, as the server answers)."""
        rows = self.call("ls", query={"dir_path": clean(path)}) or []
        return [{"name": r.get("name"), "dir": bool(r.get("is_dir")), "size": int(r.get("size") or 0),
                 "updated_at": None if r.get("is_dir") else console.unix(r.get("updated_at"))} for r in rows]

    def kind(self, path: str) -> str | None:
        """'file', 'dir' or None (missing). The root is a dir."""
        p = clean(path)
        if p == "/":
            return "dir"
        for r in self.ls(posixpath.dirname(p)):
            if r["name"] == posixpath.basename(p):
                return "dir" if r["dir"] else "file"
        return None

    # ---- upload ----

    def put_file(self, local: pathlib.Path, remote_dir: str, name: str | None = None) -> int:
        """Upload one file into remote_dir (made if missing) in 5 MiB chunks; return its size."""
        name = name or local.name
        if any(c in name for c in '/"\r\n') or name in ("", ".", ".."):
            raise ValueError(f"file name {name!r}")
        size = local.stat().st_size
        with open(local, "rb") as f:
            md5 = hashlib.md5(f.read(MD5_HEAD)).hexdigest()
        chunks = max(1, -(-size // CHUNK))
        file_uuid = f"{int(time.time() * 1000)}{uuid.uuid4().int % 1000:03d}"
        with open(local, "rb") as f:
            for i in range(chunks):
                part = f.read(CHUNK)
                fields = {"id": "0", "name": name, "type": "application/octet-stream", "size": str(size),
                          "chunks": str(chunks), "chunk": str(i), "chunk_offset": str(i), "md5": md5,
                          "is_last": "1" if i == chunks - 1 else "0", "file_uuid": file_uuid,
                          "dir_path": clean(remote_dir), "filename": name}
                body, ctype = multipart(fields, name, part)
                for attempt in range(TRIES):        # a chunk is sent again only when no answer came
                    try:
                        self.call("upload", raw=body, ctype=ctype)
                        break
                    except console.NoAnswer:
                        if attempt == TRIES - 1:
                            raise
                        console.SLEEP(2 * (attempt + 1))
        return size

    def put(self, local: str, remote_dir: str) -> dict:
        """Upload a file, or a folder with everything in it, into remote_dir."""
        src = pathlib.Path(local).expanduser()
        if not src.exists():
            raise ValueError(f"{local} does not exist")
        files = [(src, clean(remote_dir))] if src.is_file() else [
            (p, clean(posixpath.join(remote_dir, src.name, *p.relative_to(src).parent.parts)))
            for p in sorted(src.rglob("*")) if p.is_file() and not p.is_symlink()]
        total = sum(p.stat().st_size for p, _ in files)
        free = self.usage()["free_bytes"]
        if total > free:
            raise StorageError(f"{total} bytes to upload, {free} free on the file storage")
        done = 0
        for p, d in files:
            done += self.put_file(p, d)
        return {"files": len(files), "bytes": done, "to": clean(remote_dir)}

    # ---- download ----

    def get_file(self, remote: str, local: pathlib.Path) -> int:
        p = clean(remote)
        tok = self.call("token", body={"filename": posixpath.basename(p), "dir_path": posixpath.dirname(p)})
        url = self.base + SERVER["download"][1] + urllib.parse.quote(str(tok)) + "?" + urllib.parse.urlencode(
            {"auth": self.token})
        local.parent.mkdir(parents=True, exist_ok=True)
        part = local.with_name(local.name + ".part")
        size = 0
        try:
            with console.URLOPEN(urllib.request.Request(url, headers={"User-Agent": "autodl-autopilot"}),
                                 timeout=TIMEOUT_S) as r, open(part, "wb") as f:
                while True:
                    block = r.read(1 << 20)
                    if not block:
                        break
                    f.write(block)
                    size += len(block)
        except urllib.error.HTTPError as e:
            if part.exists():
                part.unlink()
            raise StorageError(f"download of {p}: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            if part.exists():
                part.unlink()
            raise console.NoAnswer(f"download of {p}: no answer ({getattr(e, 'reason', e)})") from None
        os.replace(part, local)
        return size

    def get(self, remote: str, local_dir: str) -> dict:
        """Download a file, or a folder with everything in it, into local_dir."""
        p, dst = clean(remote), pathlib.Path(local_dir).expanduser()
        kind = self.kind(p)
        if kind is None:
            raise StorageError(f"{p} is not on the file storage")
        if kind == "file":
            return {"files": 1, "bytes": self.get_file(p, dst / posixpath.basename(p)), "to": str(dst)}
        files = bytes_ = 0
        for rel in self.walk(p):
            bytes_ += self.get_file(posixpath.join(p, rel), dst.joinpath(posixpath.basename(p) or "autodl-fs",
                                                                         *rel.split("/")))
            files += 1
        return {"files": files, "bytes": bytes_, "to": str(dst)}

    def walk(self, folder: str) -> list:
        """Every file under folder, as paths relative to it."""
        found = []
        for r in self.ls(folder):
            if r["dir"]:
                found += [r["name"] + "/" + x for x in self.walk(posixpath.join(folder, r["name"]))]
            else:
                found.append(r["name"])
        return found

    # ---- delete ----

    def rm(self, path: str, recursive: bool = False) -> dict:
        p = clean(path)
        if p == "/":
            raise ValueError("refusing to delete the whole file storage; name a file or folder")
        kind = self.kind(p)
        if kind is None:
            raise StorageError(f"{p} is not on the file storage")
        parent, name = posixpath.dirname(p), posixpath.basename(p)
        if kind == "file":
            self.call("rm_file", body={"filename": name, "dir_path": parent})
            return {"deleted": p, "files": 1}
        inside = self.ls(p)
        if inside and not recursive:
            raise StorageError(f"{p} is not empty ({len(self.walk(p))} files): --recursive deletes them too")
        return {"deleted": p, "files": self.empty(p)}

    def empty(self, folder: str) -> int:
        """Delete folder after everything in it, deepest first; return the number of files deleted."""
        files = 0
        for r in self.ls(folder):
            if r["dir"]:
                files += self.empty(posixpath.join(folder, r["name"]))
            else:
                self.call("rm_file", body={"filename": r["name"], "dir_path": folder})
                files += 1
        self.call("rm_dir", body={"dir_name": posixpath.basename(folder), "dir_path": posixpath.dirname(folder)})
        return files


def multipart(fields: dict, filename: str, content: bytes) -> tuple:
    boundary = "----autopilot" + uuid.uuid4().hex
    head = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                    for k, v in fields.items())
    head += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
             "Content-Type: application/octet-stream\r\n\r\n").encode()
    return head + content + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"
