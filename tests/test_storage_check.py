"""Before a power-on or a rent: the region's file storage. Not initialized, `on` and `rent --yes` send nothing and
say to initialize it first (an instance mounts /root/autodl-fs only when it starts); --without-storage or
storage_check false go on without it; a region without file storage, or a list that cannot be read, does not stop
anything."""
import pytest

from autopilot import cli, remote
from conftest import ID, market_record, run_cli


@pytest.fixture
def ssh(monkeypatch):
    monkeypatch.setattr(remote, "deploy_and_start", lambda r, settings: {"daemon_running": True})


def grant(capsys):
    assert run_cli(capsys, "grant", ID, "--modes", "both", "--budget", "100", "--said", "ok")[0] == 0


def test_on_refused_until_storage_is_initialized(fake, ssh, capsys):
    grant(capsys)
    fake.storage["西北B区"] = ("west-B", "uninitialized")
    rc, got = run_cli(capsys, "on", ID, "--mode", "nogpu", "--hours", "1")
    assert rc == cli.EXIT_REFUSED and got["sent"] is False and fake.writes() == []
    assert "not initialized" in got["refused"] and got["storage"]["sign"] == "west-B"
    assert "storage init west-B" in got["next"] and "--without-storage" in got["next"]
    assert run_cli(capsys, "storage", "init", "west-B", "--yes")[0] == 0
    rc, got = run_cli(capsys, "on", ID, "--mode", "nogpu", "--hours", "1")
    assert rc == 0, got


def test_on_without_storage_goes_on(fake, ssh, capsys):
    grant(capsys)
    fake.storage["西北B区"] = ("west-B", "uninitialized")
    rc, got = run_cli(capsys, "on", ID, "--mode", "nogpu", "--hours", "1", "--without-storage")
    assert rc == 0, got


def test_storage_check_off(fake, ssh, capsys):
    grant(capsys)
    fake.storage["西北B区"] = ("west-B", "uninitialized")
    assert run_cli(capsys, "config", "set", "storage_check", "false")[1]["storage_check"] is False
    rc, got = run_cli(capsys, "on", ID, "--mode", "nogpu", "--hours", "1")
    assert rc == 0, got
    assert not [c for c in fake.calls if c[0] == "/api/v1/file_storage/list"]


@pytest.mark.parametrize("case", ["no storage in the region", "no answer"])
def test_unknown_storage_does_not_stop_a_power_on(fake, ssh, capsys, case):
    grant(capsys)
    if case == "no answer":
        fake.fail["/api/v1/file_storage/list"] = "noanswer"
    else:
        del fake.storage["西北B区"]
    rc, got = run_cli(capsys, "on", ID, "--mode", "nogpu", "--hours", "1")
    assert rc == 0, got


def test_rent_plan_says_it_and_yes_is_refused(fake, ssh, capsys):
    fake.machines = [market_record("m1", idle=2, li=1140, cuda="12.4")]          # in 内蒙B区
    fake.storage["内蒙B区"] = ("nm-B1", "uninitialized")
    rent = ("rent", "m1", "--hours", "2", "--budget", "30")
    rc, got = run_cli(capsys, *rent)
    assert rc == 0 and got["sent"] is False and "storage init nm-B1" in got["plan"]["storage"]["note"]
    rc, got = run_cli(capsys, *rent, "--yes", "--said", "ok")
    assert rc == cli.EXIT_REFUSED and got["sent"] is False and fake.rented == []
    rc, got = run_cli(capsys, *rent, "--yes", "--said", "ok", "--without-storage")
    assert rc == 0 and len(fake.rented) == 1, got


def test_rent_plan_is_quiet_when_storage_is_ready(fake, ssh, capsys):
    fake.machines = [market_record("m1", idle=2, li=1140, cuda="12.4")]
    rc, got = run_cli(capsys, "rent", "m1", "--hours", "2", "--budget", "30")
    assert rc == 0 and "storage" not in got["plan"]
