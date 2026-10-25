"""The power-on mode against the host's free GPUs: few to spare and no-GPU mode, or plenty and GPU mode, stops
before anything is sent, unless --skip-mode-check or mode_check off. The rent plan's tip follows the same rule."""
import pytest

from autopilot import cli, remote
from conftest import ID, market_record, run_cli


@pytest.fixture
def ssh(monkeypatch):
    monkeypatch.setattr(remote, "deploy_and_start", lambda r, settings: {"daemon_running": True})


def grant(capsys):
    assert run_cli(capsys, "grant", ID, "--modes", "both", "--budget", "50", "--said", "随便开")[0] == 0


def on(capsys, mode, *extra):
    return run_cli(capsys, "on", ID, "--mode", mode, "--hours", "1", *extra)


def test_few_free_gpus_stop_a_nogpu_power_on(fake, ssh, capsys):
    grant(capsys)
    fake.inst["gpu_idle_num"] = 2                     # one GPU needed, one to spare
    rc, got = on(capsys, "nogpu")
    assert rc == cli.EXIT_REFUSED and got["sent"] is False and "--mode gpu" in got["next"]
    assert "--skip-mode-check" in got["next"] and fake.writes() == []
    assert on(capsys, "gpu")[0] == 0                  # GPU mode is the advice here


def test_skip_mode_check_powers_on_all_the_same(fake, ssh, capsys):
    grant(capsys)
    fake.inst["gpu_idle_num"] = 1
    rc, got = on(capsys, "nogpu", "--skip-mode-check")
    assert rc == 0 and got["mode"] == "nogpu"


def test_plenty_of_free_gpus_stop_a_gpu_power_on(fake, ssh, capsys):
    grant(capsys)
    fake.inst["gpu_idle_num"] = 4
    rc, got = on(capsys, "gpu")
    assert rc == cli.EXIT_REFUSED and "--mode nogpu" in got["next"] and fake.writes() == []
    assert on(capsys, "nogpu")[0] == 0


def test_no_gpu_free_for_it_leaves_no_gpu_mode_alone(fake, ssh, capsys):
    grant(capsys)
    fake.inst["gpu_idle_num"] = 0                     # GPU mode cannot start anyway
    assert on(capsys, "nogpu")[0] == 0


def test_settings(fake, ssh, capsys):
    grant(capsys)
    fake.inst["gpu_idle_num"] = 4
    run_cli(capsys, "config", "set", "spare_gpus", "4")    # 3 to spare is now few
    assert on(capsys, "nogpu")[0] == cli.EXIT_REFUSED
    run_cli(capsys, "config", "set", "mode_check", "false")
    assert on(capsys, "nogpu")[0] == 0


def test_rent_tip_follows_the_free_gpus(fake, capsys):
    fake.machines = [market_record("few", idle=2, cuda="12.4"), market_record("many", idle=6, cuda="12.4")]
    tip = run_cli(capsys, "rent", "few", "--hours", "1", "--budget", "30")[1]["tip"]
    assert "staying in GPU mode" in tip
    tip = run_cli(capsys, "rent", "many", "--hours", "1", "--budget", "30")[1]["tip"]
    assert "--mode nogpu" in tip
