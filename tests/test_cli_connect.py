"""autopilot connect, and the key being added when the instance refuses it, against a fake console and ssh."""
import subprocess

import pytest

from autopilot import cli, remote
from conftest import ID, run_cli
from test_remote import DENIED, PUB


@pytest.fixture
def key(tmp_path, capsys):
    path = tmp_path / "id_ed25519_autodl"
    path.write_text("private\n")
    path.with_name(path.name + ".pub").write_text(PUB + "\n")
    assert run_cli(capsys, "config", "set", "key", path)[0] == 0
    return path


@pytest.fixture
def ssh(monkeypatch):
    """ssh refuses the key until a password login has added it."""
    calls, added = [], []

    def run(argv, input=None, capture_output=None, timeout=None, **kw):
        password = kw.get("env", {}).get("AUTOPILOT_SSH_PASSWORD")
        calls.append({"argv": argv, "script": input.decode(), "password": password})
        if password is not None:
            if password != "secret-pw":
                return subprocess.CompletedProcess(argv, 255, b"", DENIED.encode())
            added.append(True)
        elif not added:
            return subprocess.CompletedProcess(argv, 255, b"", DENIED.encode())
        return subprocess.CompletedProcess(argv, 0, b"", remote.RAN.encode())
    monkeypatch.setattr(remote, "RUN", run)
    monkeypatch.setattr(remote, "ssh_binary", lambda: "ssh")
    return calls


def test_connect_adds_the_key_once(fake, key, ssh, capsys):
    fake.inst["status"] = "running"
    rc, got = run_cli(capsys, "connect", ID)
    assert rc == 0 and got["key_added"] is True and got["login"] == "key"
    assert got["ssh"] == f"ssh -i {key} -p 41234 root@connect.example.seetacloud.com"
    assert [c["password"] for c in ssh] == [None, "secret-pw", None]
    assert "secret-pw" not in repr(got) and PUB in ssh[1]["script"]
    rc, got = run_cli(capsys, "connect", ID)
    assert rc == 0 and got["key_added"] is False and len(ssh) == 4


def test_any_remote_command_adds_the_key(fake, key, ssh, capsys, monkeypatch):
    fake.inst["status"] = "running"
    monkeypatch.setattr(remote, "status", lambda r: (r.run("true"), {"daemon_running": True})[1])
    rc, got = run_cli(capsys, "status", ID, "--guard")
    assert rc == 0 and [c["password"] for c in ssh] == [None, "secret-pw", None]


def test_add_key_off_leaves_the_refusal(fake, key, ssh, capsys):
    fake.inst["status"] = "running"
    assert run_cli(capsys, "config", "set", "add_key", "false")[0] == 0
    rc, got = run_cli(capsys, "connect", ID)
    assert rc == cli.EXIT_UNREACHABLE and "refused" in got["error"] and "设置SSH免密登录" in got["next"]
    assert [c["password"] for c in ssh] == [None]


def test_wrong_password_is_reported_without_it(fake, key, ssh, capsys):
    fake.inst.update(status="running", root_password="changed")
    rc, got = run_cli(capsys, "connect", ID)
    assert rc == cli.EXIT_UNREACHABLE and "changed" not in repr(got)
    assert [c["password"] for c in ssh] == [None, "changed"]


def test_connect_needs_a_running_instance(fake, key, ssh, capsys):
    rc, got = run_cli(capsys, "connect", ID)
    assert rc == cli.EXIT_ERR and "not running" in got["error"] and ssh == []
