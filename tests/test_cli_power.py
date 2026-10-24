"""autopilot on / off / arm / timer against a fake console, with the SSH side replaced."""
import pytest

from autopilot import cli, console, remote
from conftest import ID, T0, run_cli


@pytest.fixture
def ssh(monkeypatch, fake):
    """Record what would go to the instance; the guard starts, shutdown succeeds unless told otherwise."""
    calls = {"deploy": [], "shutdown": []}

    def deploy(r, settings):
        calls["deploy"].append((r.host, r.port, settings))
        if calls.get("unreachable"):
            calls["unreachable"] -= 1
            raise remote.Unreachable("sshd not up yet")
        return {"daemon_running": True}

    def shutdown(r, force):
        calls["shutdown"].append(force)
        if calls.get("jobs") and not force:
            return 3, {"ok": False, "refused": "jobs are running", "running": "train"}
        fake.inst["status"] = "shutdown"      # the instance goes down a little later
        return 0, {"ok": True}
    monkeypatch.setattr(remote, "deploy_and_start", deploy)
    monkeypatch.setattr(remote, "shutdown", shutdown)
    return calls


def grant(capsys, modes="both", budget="100"):
    assert run_cli(capsys, "grant", ID, "--modes", modes, "--budget", budget, "--said", "the user's words")[0] == 0


def test_on_without_grant_sends_nothing(fake, ssh, capsys):
    rc, got = run_cli(capsys, "on", ID, "--mode", "nogpu", "--hours", "1")
    assert rc == cli.EXIT_NO_GRANT and got["sent"] is False
    assert fake.writes() == []


def test_on_nogpu_full_sequence(fake, ssh, capsys):
    grant(capsys)
    rc, got = run_cli(capsys, "on", ID, "--mode", "nogpu", "--hours", "1")
    assert rc == 0, got
    paths = [c[0] for c in fake.writes()]
    # the fallback timer comes first, the power-on second, the timer is cancelled once the guard runs
    assert paths == ["/api/v1/instance/timed/shutdown", "/api/v1/instance/power_on",
                     "/api/v1/instance/timed/shutdown"]
    assert fake.writes()[0][1]["shutdown_at"] == "2026-10-23 21:31"   # 21:00:40 + 30 min, rounded up
    assert fake.writes()[1][1] == {"instance_uuid": ID, "payload": "non_gpu"}
    assert fake.writes()[2][1]["shutdown_at"] is None
    assert ssh["deploy"] == [("connect.example.seetacloud.com", 41234, {"idle": 900, "mode": "nogpu"})]
    assert got["guard"] == {"daemon_running": True} and got["timer"] is None


def test_on_keeps_a_hard_limit(fake, ssh, capsys):
    grant(capsys)
    rc, got = run_cli(capsys, "on", ID, "--mode", "nogpu", "--hours", "1", "--hard-limit", "8h")
    assert rc == 0 and fake.writes()[-1][1]["shutdown_at"] == "2026-10-24 05:01"


def test_on_waits_for_ssh(fake, ssh, capsys):
    grant(capsys)
    ssh["unreachable"] = 2
    rc, got = run_cli(capsys, "on", ID, "--mode", "nogpu", "--hours", "1")
    assert rc == 0 and len(ssh["deploy"]) == 3


def test_on_guard_failure_leaves_the_fallback_timer(fake, ssh, capsys, monkeypatch):
    grant(capsys)
    monkeypatch.setattr(cli, "SSH_READY_S", 0)
    ssh["unreachable"] = 99
    rc, got = run_cli(capsys, "on", ID, "--mode", "nogpu", "--hours", "1")
    assert rc == cli.EXIT_ERR and "fallback timer still" in got["note"]
    assert console.instance(cli.creds.find()[0], ID)["timer"] is not None


def test_on_refused_over_budget(fake, ssh, capsys):
    grant(capsys, budget="1")
    rc, got = run_cli(capsys, "on", ID, "--mode", "gpu", "--hours", "1")
    assert rc == cli.EXIT_BUDGET and fake.writes() == []


def test_on_refused_without_free_gpu(fake, ssh, capsys):
    grant(capsys)
    fake.inst["gpu_idle_num"] = 0
    rc, got = run_cli(capsys, "on", ID, "--mode", "gpu", "--hours", "1")
    assert rc == cli.EXIT_REFUSED and "no free GPU" in got["refused"] and fake.writes() == []


def test_on_refused_when_running(fake, ssh, capsys):
    grant(capsys)
    fake.inst["status"] = "running"
    rc, got = run_cli(capsys, "on", ID, "--mode", "nogpu", "--hours", "1")
    assert rc == cli.EXIT_REFUSED and "arm" in got["refused"] and fake.writes() == []


def test_on_refused_by_the_console_cancels_the_fallback(fake, ssh, capsys):
    grant(capsys)
    fake.fail["/api/v1/instance/power_on"] = ("InsufficientBalance", "余额不足")
    rc, got = run_cli(capsys, "on", ID, "--mode", "nogpu", "--hours", "1")
    assert rc == cli.EXIT_REFUSED and got["code"] == "InsufficientBalance"
    assert fake.writes()[-1][1]["shutdown_at"] is None


def test_on_without_answer_is_settled_by_reading(fake, ssh, capsys):
    grant(capsys)
    fake.fail["/api/v1/instance/power_on"] = "noanswer"         # it did power on, the answer was lost
    rc, got = run_cli(capsys, "on", ID, "--mode", "nogpu", "--hours", "1")
    assert rc == 0 and len([c for c in fake.writes() if c[0].endswith("power_on")]) == 1


def test_on_without_answer_that_did_nothing_is_uncertain(fake, ssh, capsys):
    grant(capsys)
    fake.fail["/api/v1/instance/power_on"] = "noanswer"
    fake.on_power_on = "shutdown"
    rc, got = run_cli(capsys, "on", ID, "--mode", "nogpu", "--hours", "1")
    assert rc == cli.EXIT_UNCERTAIN and got["state"] == "shutdown"
    assert len([c for c in fake.writes() if c[0].endswith("power_on")]) == 1


def test_off_over_ssh(fake, ssh, capsys):
    fake.inst["status"] = "running"
    fake.inst["timed_shutdown_at"] = {"Time": "2026-10-23T22:00:00+08:00", "Valid": True}
    rc, got = run_cli(capsys, "off", ID)
    assert rc == 0 and ssh["shutdown"] == [False] and got["states"] == ["shutdown"]
    assert fake.writes() == [("/api/v1/instance/timed/shutdown", {"instance_uuid": ID, "shutdown_at": None})]
    assert "bills" in got


def test_off_refused_while_jobs_run(fake, ssh, capsys):
    fake.inst["status"] = "running"
    ssh["jobs"] = True
    rc, got = run_cli(capsys, "off", ID)
    assert rc == cli.EXIT_REFUSED and got["running"] == "train" and ssh["shutdown"] == [False]


def test_off_console_needs_force(fake, ssh, capsys):
    fake.inst["status"] = "running"
    rc, got = run_cli(capsys, "off", ID, "--console")
    assert rc == cli.EXIT_REFUSED and fake.writes() == []
    rc, got = run_cli(capsys, "off", ID, "--console", "--force")
    assert rc == 0 and fake.writes() == [("/api/v1/instance/power_off", {"instance_uuid": ID})]


def test_timer_command(fake, capsys):
    rc, got = run_cli(capsys, "timer", ID, "--in", "2h")
    assert rc == 0 and got["timer"] == "2026-10-23 23:01:00"
    rc, got = run_cli(capsys, "timer", ID, "--cancel")
    assert rc == 0 and got["timer"] is None


def test_no_token_is_exit_14(capsys):
    rc, got = run_cli(capsys, "balance")
    assert rc == cli.EXIT_TOKEN and got["token"] is False


# ---- the guard's switch: guard_default, the instance's own setting, the flag of one command ----

def on(capsys, *extra):
    return run_cli(capsys, "on", ID, "--mode", "nogpu", "--hours", "1", *extra)


def test_guard_default_on(fake, ssh, capsys):
    grant(capsys)
    rc, got = on(capsys)
    assert rc == 0 and got["guard_setting"] == {"on": True, "from": "guard_default"} and len(ssh["deploy"]) == 1


def test_guard_default_off_starts_no_guard_and_removes_the_fallback(fake, ssh, capsys):
    grant(capsys)
    assert run_cli(capsys, "config", "set", "guard_default", "false")[1]["guard_default"] is False
    rc, got = on(capsys)
    assert rc == 0 and got["guard"] is None and ssh["deploy"] == []
    assert got["guard_setting"] == {"on": False, "from": "guard_default"} and "until autopilot off" in got["warning"]
    assert fake.writes()[-1][1]["shutdown_at"] is None          # no fallback left to cut it


def test_one_instance_on_while_the_default_is_off(fake, ssh, capsys):
    grant(capsys)
    run_cli(capsys, "config", "set", "guard_default", "off")
    rc, got = run_cli(capsys, "guard", ID, "on")
    assert rc == 0 and got["guard"] is True and got["from"] == "instance" and "next autopilot on" in got["note"]
    rc, got = on(capsys)
    assert got["guard_setting"] == {"on": True, "from": "instance"} and len(ssh["deploy"]) == 1


def test_one_instance_off_while_the_default_is_on(fake, ssh, capsys):
    grant(capsys)
    run_cli(capsys, "guard", ID, "off")
    rc, got = on(capsys)
    assert got["guard_setting"] == {"on": False, "from": "instance"} and ssh["deploy"] == []
    run_cli(capsys, "guard", ID, "auto")                       # back to following the default
    fake.inst["status"] = "shutdown"
    rc, got = on(capsys)
    assert got["guard_setting"] == {"on": True, "from": "guard_default"}


def test_the_flag_of_one_command_wins(fake, ssh, capsys):
    grant(capsys)
    run_cli(capsys, "guard", ID, "off")
    rc, got = on(capsys, "--guard")
    assert got["guard_setting"] == {"on": True, "from": "this command"} and len(ssh["deploy"]) == 1


def test_switching_a_running_instance_acts_at_once(fake, ssh, capsys, monkeypatch):
    stopped = []
    monkeypatch.setattr(remote, "stop_guard", lambda r: stopped.append(r.instance) or {"daemon_running": False})
    fake.inst["status"] = "running"
    rc, got = run_cli(capsys, "guard", ID, "off")
    assert rc == 0 and stopped == [ID] and got["on_instance"] == {"daemon_running": False}
    rc, got = run_cli(capsys, "guard", ID, "on")
    assert rc == 0 and len(ssh["deploy"]) == 1


def test_guard_default_must_be_a_boolean(capsys):
    rc, got = run_cli(capsys, "config", "set", "guard_default", "maybe")
    assert rc == cli.EXIT_ERR


# ---- the whitelist commands ----

@pytest.fixture
def wl(monkeypatch, fake):
    sent = []

    def protect(r, name, kind, value, seconds):
        sent.append((name, kind, value, seconds))
        return {"ok": True, "whitelist": [{"name": name, "type": kind, "value": value, "expires_in": seconds}]}
    monkeypatch.setattr(remote, "protect", protect)
    fake.inst["status"] = "running"
    return sent


@pytest.mark.parametrize("args,expect", [(["--cmd", "jupyter-lab", "--for", "7d"], ("cmd", "jupyter-lab", 604800)),
                                         (["--pid", "1234", "--for", "1d"], ("pid", "1234", 86400)),
                                         (["--job", "train", "--for", "12h"], ("job", "train", 43200))])
def test_protect(wl, capsys, args, expect):
    rc, got = run_cli(capsys, "protect", ID, "keep1", *args)
    assert rc == 0 and wl == [("keep1", *expect)]


@pytest.mark.parametrize("args", [["--for", "1d"], ["--pid", "1", "--cmd", "abc", "--for", "1d"],
                                  ["--cmd", "ab", "--for", "1d"], ["--cmd", "jupyter", "--for", "91d"],
                                  ["--job", "../x", "--for", "1d"]])
def test_protect_refuses_bad_input(wl, capsys, args):
    rc, got = run_cli(capsys, "protect", ID, "keep1", *args)
    assert rc == cli.EXIT_ERR and wl == []


def test_run_with_protect(wl, capsys, monkeypatch):
    monkeypatch.setattr(remote, "start_job", lambda *a, **k: {"ok": True, "job": "train", "pid": 7})
    rc, got = run_cli(capsys, "run", ID, "train", "--protect", "1d", "--", "python", "train.py")
    assert rc == 0 and wl == [("train", "job", "train", 86400)] and got["protect"][0]["name"] == "train"


def test_run_options_before_the_command_are_options(wl, capsys, monkeypatch):
    seen = []
    monkeypatch.setattr(remote, "start_job",
                        lambda r, name, command, workdir, env, then_off: seen.append((command, then_off)) or {"ok": True})
    rc, got = run_cli(capsys, "run", ID, "train", "--then-off", "--", "python", "train.py", "--epochs", "3", "--", "x")
    assert rc == 0 and seen == [("python train.py --epochs 3 -- x", True)]
