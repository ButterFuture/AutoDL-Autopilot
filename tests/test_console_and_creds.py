"""The console API client and the token lookup."""
import os
import subprocess

import pytest

from autopilot import console, creds
from conftest import ID, T0, TOKEN, bill, run_cli


def test_only_listed_requests(fake):
    for name in ("release", "clone", "recharge", "/api/v1/instance/release"):
        with pytest.raises(ValueError, match="not a request"):
            console.request(TOKEN, name)
    assert fake.calls == []


def test_view_has_the_facts_and_no_secret(fake):
    fake.inst["timed_shutdown_at"] = {"Time": "2026-10-23T22:10:00+08:00", "Valid": True}
    it = console.instance(TOKEN, ID)
    assert it["state"] == "shutdown" and it["mode"] == "nogpu" and it["gpu_free"] is True
    assert it["price_fen_h"] == {"gpu": 114, "nogpu": 10}
    assert console.stamp(it["timer"]) == "2026-10-23 22:10:00"
    assert console.stamp(it["release_at"]) == "2026-11-07 20:30:00"
    assert it["ssh_host"] == "connect.example.seetacloud.com" and it["ssh_port"] == 41234
    assert "secret" not in repr(it) and "ssh -p" not in repr(it)


def test_refused_token_is_a_token_error(fake):
    fake.token_ok = False
    with pytest.raises(creds.TokenError, match="refused"):
        console.balance_fen(TOKEN)


def test_charges_from_bills():
    rows = [bill("3", "2026-10-23T21:11:09+08:00", 10, "charge_shutdown", to="2026-10-23 21:11:00"),
            bill("2", "2026-10-23T20:59:59+08:00", 20),
            bill("1", "2026-09-29T23:59:59+08:00", 230, "data_disk_settle", charge_type="payg")]
    got = console.charges(rows, since=console.unix("2026-10-01 00:00:00"))
    assert [(c["serial"], c["fen"], c["kind"], c["gpu"]) for c in got] == [
        ("3", 1, "charge_shutdown", False), ("2", 2, "charge_settle", False)]
    assert got[0]["to"] == console.unix("2026-10-23 21:11:00")


def test_timer_rounds_up_to_the_minute(fake):
    console.set_timer(TOKEN, ID, T0 + 30 * 60)          # 21:30:40 -> 21:31
    assert fake.writes() == [("/api/v1/instance/timed/shutdown", {"instance_uuid": ID,
                                                                  "shutdown_at": "2026-10-23 21:31"})]


# ---- the token ----

def test_token_order_env_then_file_then_store(monkeypatch):
    monkeypatch.setenv("AUTODL_WEB_JWT", "eyJenv.a.b")
    creds.save(TOKEN)
    assert creds.find() == ("eyJenv.a.b", "env")
    monkeypatch.delenv("AUTODL_WEB_JWT")
    called = []
    monkeypatch.setattr(creds, "RUN", lambda *a, **k: called.append(a))
    assert creds.find() == (TOKEN, "file") and called == []


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
def test_token_file_others_can_read_is_refused():
    path = creds.save(TOKEN)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    path.chmod(0o644)
    with pytest.raises(creds.TokenError, match="chmod 600"):
        creds.find()


@pytest.mark.parametrize("result", ["hang", "refuse"])
def test_store_that_hangs_or_refuses_is_skipped(monkeypatch, result):
    monkeypatch.setattr(creds.sys, "platform", "darwin")

    def store(cmd, **kw):
        assert kw["timeout"] == creds.STORE_TIMEOUT and kw["stdin"] == subprocess.DEVNULL
        if result == "hang":
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return subprocess.CompletedProcess(cmd, 36, "", "User interaction is not allowed.")
    monkeypatch.setattr(creds, "RUN", store)
    with pytest.raises(creds.TokenError, match="no console token"):
        creds.find()


@pytest.mark.parametrize("bad", ["", "not-a-token", "eyJa.b", "eyJa.b.c d"])
def test_save_refuses_what_is_not_a_token(bad):
    with pytest.raises(ValueError):
        creds.save(bad)
    assert not creds.credentials_path().exists()


def test_token_set_reads_stdin_and_never_prints_it(monkeypatch, capsys):
    import io
    import sys
    monkeypatch.setattr(sys, "stdin", io.StringIO(TOKEN + "\n"))
    rc, got = run_cli(capsys, "token", "set")
    assert rc == 0 and got["exp"] == 1797886800
    assert TOKEN not in str(got) and TOKEN not in capsys.readouterr().err


def test_token_times_only():
    t = creds.times(TOKEN)
    assert t["exp"] == 1797886800 and "uid" not in t
