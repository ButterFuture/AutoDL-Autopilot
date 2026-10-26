"""autopilot remind: reminders 30, 20 and 10 minutes before the console timer, against a fake console and clock."""
import json
import os

import pytest

from autopilot import cli, remote
from conftest import ID, T0, run_cli

TIMER = "2026-10-23 21:45:00"          # T0 is 21:00:40, so 44 min 20 s left at the start
TIMER_T = T0 + 44 * 60 + 20


@pytest.fixture
def clock(fake, monkeypatch):
    """Sleeps move the console's clock; the instance goes down when its timer is reached."""
    fake.inst.update(status="running", start_mode="gpu")

    def sleep(s):
        fake.server += s
        t = cli.console.unix(fake.inst["timed_shutdown_at"])
        if t and fake.server >= t:
            fake.inst["status"] = "shutdown"
    monkeypatch.setattr(cli, "SLEEP", sleep)
    return fake


@pytest.fixture
def jobs(monkeypatch):
    running = {"names": ["train"]}

    def status(r):
        return {"jobs": [{"name": n, "running": True} for n in running["names"]] + [{"name": "old", "running": False}]}
    monkeypatch.setattr(remote, "status", status)
    return running


def set_timer(fake, text):
    fake.inst["timed_shutdown_at"] = {"Time": text.replace(" ", "T") + "+08:00", "Valid": True} if text else \
        {"Time": "0001-01-01T00:00:00Z", "Valid": False}


def test_marks_parse_and_check():
    assert cli.remind_marks("10,30,20,30") == [30, 20, 10]
    for bad in ("", "0", "a,b", "2000"):
        with pytest.raises(ValueError):
            cli.remind_marks(bad)


def test_due_mark_gives_the_nearest_once():
    assert cli.due_mark(45 * 60, [30, 20, 10], []) is None
    assert cli.due_mark(30 * 60, [30, 20, 10], []) == 30
    assert cli.due_mark(15 * 60, [30, 20, 10], []) == 20          # not 30 and then 20
    assert cli.due_mark(15 * 60, [30, 20, 10], [30, 20]) is None
    assert cli.due_mark(9 * 60, [30, 20, 10], [30, 20]) == 10


def test_no_timer_says_so_and_ends(clock, jobs, capsys):
    rc, got = run_cli(capsys, "remind", ID)
    assert rc == 0 and got["event"] == "timer" and got["timer"] is None


def test_waits_for_30_then_20_then_10(clock, jobs, capsys):
    set_timer(clock, TIMER)
    rc, got = run_cli(capsys, "remind", ID)
    assert rc == 0 and got["event"] == "reminder" and got["mark_min"] == 30
    assert TIMER_T - clock.server <= 30 * 60
    assert got["jobs_running"] == ["train"] and "will be cut: train" in got["message"]
    assert f"autopilot timer {ID} --in" in got["message"]
    rc, got = run_cli(capsys, "remind", ID)                # a restarted run does not repeat the 30
    assert got["mark_min"] == 20
    rc, got = run_cli(capsys, "remind", ID)
    assert got["mark_min"] == 10 and got["minutes_left"] <= 10
    rc, got = run_cli(capsys, "remind", ID)
    assert got["event"] == "off"


def test_started_late_gives_one_reminder(clock, jobs, capsys):
    set_timer(clock, TIMER)
    clock.server = TIMER_T - 15 * 60
    rc, got = run_cli(capsys, "remind", ID)
    assert got["mark_min"] == 20 and got["minutes_left"] == 15
    rc, got = run_cli(capsys, "remind", ID)
    assert got["mark_min"] == 10


def test_moved_timer_counts_again(clock, jobs, capsys):
    set_timer(clock, TIMER)
    assert run_cli(capsys, "remind", ID)[1]["mark_min"] == 30
    set_timer(clock, "2026-10-23 23:00:00")                # extended: the 30 comes again before the new time
    rc, got = run_cli(capsys, "remind", ID)
    assert got["mark_min"] == 30 and got["timer"] == "2026-10-23 23:00:00"


def test_no_jobs_running_and_no_ssh(clock, jobs, capsys, monkeypatch):
    set_timer(clock, TIMER)
    jobs["names"] = []
    rc, got = run_cli(capsys, "remind", ID)
    assert got["jobs_running"] == [] and "will be cut" not in got["message"]

    def unreachable(r):
        raise remote.Unreachable("no ssh")
    monkeypatch.setattr(remote, "status", unreachable)
    rc, got = run_cli(capsys, "remind", ID)
    assert got["jobs_running"] is None
    rc, got = run_cli(capsys, "remind", ID, "--no-jobs")
    assert got["jobs_running"] is None and got["mark_min"] == 10


def test_off_instance_ends_at_once(fake, capsys):
    rc, got = run_cli(capsys, "remind", ID)
    assert rc == 0 and got["event"] == "off" and got["state"] == "shutdown"


def test_follow_prints_one_line_per_event(clock, jobs, capsys):
    set_timer(clock, TIMER)
    assert cli.main(["remind", ID, "--follow"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["event"] for e in events] == ["timer", "reminder", "reminder", "reminder", "off"]
    assert [e["mark_min"] for e in events if e["event"] == "reminder"] == [30, 20, 10]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell command")
def test_notify_cmd_gets_the_message_in_the_environment(clock, jobs, capsys, tmp_path):
    set_timer(clock, TIMER)
    sink = tmp_path / "said.txt"
    run_cli(capsys, "remind", ID, "--notify-cmd", f'printf "%s" "$AUTOPILOT_REMINDER" > "{sink}"')
    assert "shuts down at 2026-10-23 21:45:00" in sink.read_text()


def test_timer_and_hard_limit_point_to_remind(fake, capsys):
    rc, got = run_cli(capsys, "timer", ID, "--in", "2h")
    assert f"autopilot remind {ID}" in got["remind"]
    rc, got = run_cli(capsys, "timer", ID, "--cancel")
    assert "remind" not in got
