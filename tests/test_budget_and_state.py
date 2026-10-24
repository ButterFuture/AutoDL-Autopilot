"""Grants, the budget judgement and the local state."""
import json
import multiprocessing as mp

import pytest

from autopilot import budget, console, paths
from autopilot.state import State, StateError
from conftest import ID, T0, bill, run_cli

MONTH = budget.month_start(T0)


def inst(state="shutdown", mode="nogpu", started=None):
    return {"id": ID, "state": state, "mode": mode, "started_at": started,
            "price_fen_h": {"gpu": 114, "nogpu": 10}}


def test_month_starts_in_beijing_time():
    assert console.stamp(MONTH) == "2026-10-01 00:00:00"


@pytest.mark.parametrize("text,fen", [("100", 10000), ("12.5yuan", 1250), ("30元", 3000), ("none", None)])
def test_parse_yuan(text, fen):
    assert budget.parse_yuan(text) == fen


@pytest.mark.parametrize("text", ["-1", "0", "lots"])
def test_parse_yuan_refuses(text):
    with pytest.raises(ValueError):
        budget.parse_yuan(text)


def test_judge_needs_a_grant_for_the_mode():
    g = budget.make_grant("nogpu", 1000, "传数据用无卡", T0)
    assert budget.judge(None, inst(), "nogpu", 1, {}, T0)["ok"] is False
    assert budget.judge(g, inst(), "gpu", 1, {}, T0)["reason"].startswith("no grant")
    assert budget.judge(g, inst(), "nogpu", 1, {}, T0)["ok"] is True


def test_judge_counts_this_month_and_the_plan():
    g = budget.make_grant("both", 500, "本月最多 5 元", T0)
    book = {"a": {"at": MONTH + 10, "fen": 300, "to": None}, "old": {"at": MONTH - 10, "fen": 9999, "to": None}}
    ok = budget.judge(g, inst(), "gpu", 1.5, book, T0)        # 300 spent + 171 planned <= 500
    assert ok["ok"] and ok["spent_fen"] == 300 and ok["planned_fen"] == 171
    over = budget.judge(g, inst(), "gpu", 2, book, T0)        # 300 + 228 > 500
    assert not over["ok"] and over["reason"].startswith("over budget")


def test_judge_counts_a_running_instance_since_its_last_charge():
    g = budget.make_grant("both", 500, "x", T0)
    book = {"s": {"at": T0 - 1800, "fen": 50, "to": T0 - 1800}}
    running = inst(state="running", mode="gpu", started=T0 - 7200)
    res = budget.judge(g, running, "gpu", 1, book, T0)
    assert res["in_flight_fen"] == 57                         # half an hour at 1.14 yuan
    assert res["left_fen"] == 500 - 50 - 57


def test_no_money_limit():
    g = budget.make_grant("both", None, "不设预算", T0)
    assert budget.judge(g, inst(), "gpu", 100, {}, T0)["ok"]


def test_grant_needs_the_users_words():
    with pytest.raises(ValueError):
        budget.make_grant("both", None, "  ", T0)


def test_state_keeps_bills_once(capsys):
    rows = [{"serial": "1", "instance": ID, "at": T0, "fen": 2, "kind": "charge_settle", "gpu": False, "to": T0}]
    with State() as st:
        assert st.add_bills(rows) == 1
    with State() as st:
        assert st.add_bills(rows) == 0
        assert st.data["bills"][ID]["1"]["fen"] == 2


def test_state_file_is_private():
    with State() as st:
        st.event(ID, "x")
    f = paths.state_dir() / "state.json"
    assert json.loads(f.read_text())["events"][0]["event"] == "x"
    if paths.POSIX:
        assert oct(f.stat().st_mode & 0o777) == "0o600"
        assert oct(paths.state_dir().stat().st_mode & 0o777) == "0o700"


def test_state_from_another_version_is_refused():
    paths.private_dir(paths.state_dir())
    (paths.state_dir() / "state.json").write_text('{"version": 99}')
    with pytest.raises(StateError):
        with State():
            pass


def _hold_lock(ready, release, env):
    import os
    os.environ.update(env)
    from autopilot.state import State as S
    with S():
        ready.set()
        release.wait(10)


def test_state_lock_is_exclusive(monkeypatch):
    import os
    import autopilot.state as state_mod
    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    env = {k: os.environ[k] for k in ("AUTODL_AUTOPILOT_STATE", "AUTODL_AUTOPILOT_CONFIG")}
    p = ctx.Process(target=_hold_lock, args=(ready, release, env))
    p.start()
    try:
        assert ready.wait(20)
        monkeypatch.setattr(state_mod, "LOCK_TIMEOUT_S", 0.3)
        with pytest.raises(StateError, match="holds"):
            with State():
                pass
    finally:
        release.set()
        p.join(10)


def test_grant_and_spend_commands(fake, capsys):
    fake.bills = [bill("1", "2026-10-23T20:59:59+08:00", 200)]
    assert run_cli(capsys, "grant", ID, "--modes", "both", "--budget", "100", "--said", "本月最多 100 元")[0] == 0
    rc, got = run_cli(capsys, "sync")
    assert rc == 0 and got["added"] == 1
    rc, got = run_cli(capsys, "spend", ID)
    assert got["instances"] == [{"instance": ID, "spent_yuan": 0.2, "budget_yuan": 100.0, "left_yuan": 99.8}]
