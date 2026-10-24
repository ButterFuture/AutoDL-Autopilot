"""autopilot storage against a fake console and a fake regional storage server: regions, init only with --yes,
chunked upload of files and folders, download, delete (refused for a non-empty folder without --recursive, and
for the root), and no download link or token in the output."""
import json
import posixpath
import re
import urllib.error

import pytest

from autopilot import cli, console, storage
from conftest import ID, TOKEN, Resp, run_cli

SERVER = "region-nmb1.autodl.com:7007"


class FakeStorage:
    """The console's storage list and init, and one region's storage server with an in-memory tree."""

    def __init__(self, inst):
        self.inst = inst
        self.status = {"nm-B1": "created", "bj-B1": "uninitialized"}
        self.files = {"/ACDC.zip": b"z" * 10}
        self.dirs = {"/", "/alhvr"}
        self.total = 1000
        self.chunks = {}
        self.calls = []
        self.tokens = {}

    def __call__(self, req, timeout=None):
        url = req.full_url
        host, path = url.split("//", 1)[1].split("/", 1)
        path, _, query = ("/" + path).partition("?")
        self.calls.append((req.get_method(), host, path))
        if host == "www.autodl.com":
            assert req.get_header("Authorization") == TOKEN
            if path == "/api/v1/file_storage/list":
                return self.ok([{"sign": s, "name": {"nm-B1": "内蒙B区", "bj-B1": "北京B区"}[s], "status": st,
                                 "fs_type": "autofs", "export_addr_1": SERVER.split(":")[0], "port": 7007,
                                 "free_size": 20 << 30, "initialized_at": None} for s, st in self.status.items()])
            if path == "/api/v1/file_storage/init":
                self.status[json.loads(req.data)["sign"]] = "created"
                return self.ok(None)
            if path == "/api/v1/instance":
                return self.ok({"list": [self.inst], "max_page": 1})
            raise AssertionError(path)
        assert host == SERVER and path.startswith("/api/v1/file_storage")
        path = path[len("/api/v1/file_storage"):]
        q = dict(p.split("=", 1) for p in query.split("&") if p)
        q = {k: console.urllib.parse.unquote(v) for k, v in q.items()}
        if path.startswith("/download/file/"):
            assert q.get("auth") == TOKEN
            return Resp(self.files[self.tokens[path.rsplit("/", 1)[1]]])
        assert req.get_header("Authorization") == TOKEN
        if path == "/usage":
            return self.ok({"current_usage": sum(len(v) for v in self.files.values()), "total": self.total})
        if path == "/dir/v2":
            d = q["dir_path"].rstrip("/") or "/"
            rows = [{"name": posixpath.basename(f), "size": len(v), "is_dir": False,
                     "updated_at": "2026-10-24T05:02:11.889Z"} for f, v in self.files.items()
                    if posixpath.dirname(f) == d]
            rows += [{"name": posixpath.basename(x), "size": 4096, "is_dir": True, "updated_at": "0001-01-01T00:00:00Z"}
                     for x in self.dirs if x != "/" and posixpath.dirname(x) == d]
            return self.ok(rows)
        if path == "/file" and req.get_method() == "POST":
            fields, content = parse(req)
            key = (fields["file_uuid"], fields["dir_path"], fields["filename"])
            self.chunks.setdefault(key, {})[int(fields["chunk_offset"])] = content
            if fields["is_last"] == "1":
                parts = self.chunks.pop(key)
                d = fields["dir_path"]
                while d not in self.dirs:           # the server makes the folders on the way
                    self.dirs.add(d)
                    d = posixpath.dirname(d)
                self.files[posixpath.join(fields["dir_path"], fields["filename"])] = b"".join(
                    parts[i] for i in sorted(parts))
            return self.ok(int(fields["chunk_offset"]))
        body = json.loads(req.data) if req.data else {}
        if path == "/file/download_token":
            tok = f"tok{len(self.tokens)}"
            self.tokens[tok] = posixpath.join(body["dir_path"], body["filename"])
            return self.ok(tok)
        if path == "/file" and req.get_method() == "DELETE":
            del self.files[posixpath.join(body["dir_path"], body["filename"])]
            return self.ok(None)
        if path == "/dir" and req.get_method() == "DELETE":
            d = posixpath.join(body["dir_path"], body["dir_name"])
            assert not [f for f in self.files if f.startswith(d + "/")], "the server keeps non-empty folders"
            assert not [x for x in self.dirs if x.startswith(d + "/")], "the server keeps non-empty folders"
            self.dirs.discard(d)
            return self.ok(None)
        raise AssertionError(path)

    @staticmethod
    def ok(data):
        return Resp(json.dumps({"code": "Success", "data": data, "msg": ""}).encode())


def parse(req):
    boundary = re.search(r"boundary=(\S+)", req.get_header("Content-type")).group(1).encode()
    fields, content = {}, None
    for part in req.data.split(b"--" + boundary)[1:-1]:
        head, _, value = part[2:].partition(b"\r\n\r\n")
        value = value[:-2]
        name = re.search(rb'name="([^"]+)"', head).group(1).decode()
        if b"filename=" in head:
            content = value
        else:
            fields[name] = value.decode()
    return fields, content


@pytest.fixture
def fs(fake, monkeypatch):
    f = FakeStorage(fake.inst)
    monkeypatch.setattr(console, "URLOPEN", f)
    monkeypatch.setattr(storage, "CHUNK", 4)           # small chunks, so a few bytes span several
    return f


def test_regions_and_ls(fs, capsys):
    rc, got = run_cli(capsys, "storage", "list")
    assert [(r["sign"], r["status"]) for r in got["regions"]] == [("nm-B1", "created"), ("bj-B1", "uninitialized")]
    assert got["regions"][0]["server"] == f"https://{SERVER}/api/v1/file_storage"
    rc, got = run_cli(capsys, "storage", "ls", "内蒙")
    assert rc == 0 and sorted(e["name"] for e in got["entries"]) == ["ACDC.zip", "alhvr"]
    fs.inst["region_name"] = "内蒙B区"
    assert run_cli(capsys, "storage", "ls", ID)[1]["region"] == "内蒙B区"     # an instance stands for its region


def test_uninitialized_region_says_init(fs, capsys):
    rc, got = run_cli(capsys, "storage", "ls", "北京B区")
    assert rc == cli.EXIT_ERR and "storage init bj-B1" in got["error"]


def test_init_needs_yes(fs, capsys):
    rc, got = run_cli(capsys, "storage", "init", "北京B区")
    assert rc == 0 and got["sent"] is False and "--yes" in got["next"]
    assert ("POST", "www.autodl.com", "/api/v1/file_storage/init") not in fs.calls
    rc, got = run_cli(capsys, "storage", "init", "北京B区", "--yes")
    assert rc == 0 and got["status"] == "created" and fs.status["bj-B1"] == "created"


def test_put_and_get_a_folder(fs, capsys, tmp_path):
    src = tmp_path / "data"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"hello world")                 # 11 bytes: 3 chunks of 4
    (src / "sub" / "中文 名.bin").write_bytes(b"\x00\x01\x02")
    rc, got = run_cli(capsys, "storage", "put", "nm-B1", src, "/proj")
    assert rc == 0 and got["files"] == 2 and got["bytes"] == 14 and got["on_instances"] == "/root/autodl-fs/proj"
    assert fs.files["/proj/data/a.txt"] == b"hello world" and fs.files["/proj/data/sub/中文 名.bin"] == b"\x00\x01\x02"
    uploads = [c for c in fs.calls if c[0] == "POST" and c[2].endswith("/file")]
    assert len(uploads) == 4
    out = tmp_path / "back"
    rc, got = run_cli(capsys, "storage", "get", "nm-B1", "/proj/data", out)
    assert rc == 0 and got["files"] == 2
    assert (out / "data" / "a.txt").read_bytes() == b"hello world"
    assert (out / "data" / "sub" / "中文 名.bin").read_bytes() == b"\x00\x01\x02"
    assert "tok" not in json.dumps(got) and TOKEN not in json.dumps(got)     # no download link, no token
    rc, got = run_cli(capsys, "storage", "get", "nm-B1", "/ACDC.zip", out)
    assert (out / "ACDC.zip").read_bytes() == b"z" * 10 and not list(out.glob("*.part"))


def test_put_refuses_when_it_does_not_fit(fs, capsys, tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 2000)
    rc, got = run_cli(capsys, "storage", "put", "nm-B1", big)
    assert rc == cli.EXIT_ERR and "free" in got["error"] and "/big.bin" not in fs.files


def test_rm(fs, capsys, tmp_path):
    src = tmp_path / "d"
    (src / "e" / "f").mkdir(parents=True)
    (src / "e" / "f" / "x").write_bytes(b"1")
    (src / "y").write_bytes(b"2")
    run_cli(capsys, "storage", "put", "nm-B1", src, "/")
    fs.dirs.add("/d/empty")
    rc, got = run_cli(capsys, "storage", "rm", "nm-B1", "/d")
    assert rc == cli.EXIT_ERR and "--recursive" in got["error"] and "/d/y" in fs.files
    rc, got = run_cli(capsys, "storage", "rm", "nm-B1", "/d", "--recursive")
    assert rc == 0 and got["files"] == 2 and not [f for f in fs.files if f.startswith("/d")]
    assert not [x for x in fs.dirs if x.startswith("/d")]
    rc, got = run_cli(capsys, "storage", "rm", "nm-B1", "/ACDC.zip")
    assert rc == 0 and "/ACDC.zip" not in fs.files
    for bad in ("/", "/nope"):
        assert run_cli(capsys, "storage", "rm", "nm-B1", bad, "--recursive")[0] == cli.EXIT_ERR


def test_paths_are_cleaned():
    assert storage.clean("a/b/") == "/a/b" and storage.clean("") == "/"
    assert storage.clean("/a/../b") == "/b"                    # normalized inside the storage, never above it
    assert storage.clean("../../etc") == "/etc"


def test_full_storage_is_reported(fs, capsys, tmp_path, monkeypatch):
    f = tmp_path / "x"
    f.write_bytes(b"abc")
    real = fs.__call__

    def full(req, timeout=None):
        if req.get_method() == "POST" and req.full_url.endswith("/file"):
            raise urllib.error.HTTPError(req.full_url, 507, "full", {}, None)
        return real(req, timeout)
    monkeypatch.setattr(console, "URLOPEN", full)
    rc, got = run_cli(capsys, "storage", "put", "nm-B1", f)
    assert rc == cli.EXIT_ERR and "full" in got["error"]
