"""autopilot rent: a plan that sends nothing, then with --yes and the user's words one rent request, the grant
for the new instance, the fallback timer, the guard, and the timer cancelled. Against a fake console and ssh."""
import pytest

from autopilot import cli, console, remote
from conftest import market_record, run_cli

NEW = "newinst001-0a1b2c3d"


@pytest.fixture
def ssh(monkeypatch):
    calls = []

    def deploy(r, settings):
        calls.append((r.instance, settings))
        return {"daemon_running": True}
    monkeypatch.setattr(remote, "deploy_and_start", deploy)
    return calls


@pytest.fixture
def market(fake):
    fake.machines = [market_record("m1", idle=2, li=1140, cuda="12.4"), market_record("busy", idle=0)]
    return fake


def rent(capsys, *extra):
    return run_cli(capsys, "rent", "m1", "--hours", "2", "--budget", "30", *extra)


def test_plan_sends_nothing(market, ssh, capsys):
    rc, got = rent(capsys, "--gpus", "2")
    assert rc == 0 and got["sent"] is False and "--yes" in got["next"] and "--mode nogpu" in got["tip"]
    plan = got["plan"]
    assert plan["price_yuan_h"] == 2.28 and plan["planned_yuan"] == 4.56 and plan["gpus"] == 2
    assert plan["image"]["uuid"] == "base-image-mid"            # the newest PyTorch the driver (CUDA 12.4) runs
    assert market.writes() == [] and market.rented == [] and ssh == []


def test_image_choice(market, ssh, capsys):
    assert rent(capsys, "--image", "PyTorch:1.11")[1]["plan"]["image"]["uuid"] == "base-image-old"
    assert rent(capsys, "--image", "miniconda")[1]["plan"]["image"]["uuid"] == "base-image-conda"
    got = rent(capsys, "--image", "image-abc123")[1]["plan"]["image"]
    assert got == {"uuid": "image-abc123", "name": "my-env", "private": True}
    for bad in ("base-image-arm", "base-image-new", "base-image-gone", "TensorFlow"):
        rc, got = rent(capsys, "--image", bad)
        assert rc == cli.EXIT_ERR, bad
    assert market.writes() == []


def test_refused_without_free_gpus_or_over_budget(market, ssh, capsys):
    rc, got = run_cli(capsys, "rent", "busy", "--hours", "1", "--budget", "30", "--yes", "--said", "ok")
    assert rc == cli.EXIT_REFUSED and "0 free GPUs" in got["refused"]
    rc, got = rent(capsys, "--gpus", "3", "--yes", "--said", "ok")
    assert rc == cli.EXIT_REFUSED
    rc, got = run_cli(capsys, "rent", "m1", "--hours", "10", "--budget", "5", "--yes", "--said", "ok")
    assert rc == cli.EXIT_BUDGET
    market.balance_li = 1000
    rc, got = rent(capsys, "--yes", "--said", "ok")
    assert rc == cli.EXIT_REFUSED and "balance" in got["refused"]
    assert market.writes() == []


def test_yes_needs_the_users_words(market, ssh, capsys):
    rc, got = rent(capsys, "--yes")
    assert rc == cli.EXIT_ERR and "--said" in got["error"] and market.writes() == []


def test_rent_full_sequence(market, ssh, capsys):
    rc, got = rent(capsys, "--gpus", "2", "--yes", "--said", "租 m1 两张卡，跑两小时")
    assert rc == 0, got
    assert got["instance"] == NEW and got["rented"] and got["guard"] == {"daemon_running": True}
    paths = [p for p, _ in market.writes()]
    assert paths == ["/api/v1/order/instance/create/payg", "/api/v1/instance/timed/shutdown",
                     "/api/v1/instance/timed/shutdown"]
    body = market.rented[0]
    assert body["instance_info"]["machine_id"] == "m1" and body["instance_info"]["req_gpu_amount"] == 2
    assert body["instance_info"]["base_image_uuid"] == "base-image-mid" and body["price_info"]["num"] == 2
    assert market.writes()[1][1]["shutdown_at"] is not None and market.writes()[2][1]["shutdown_at"] is None
    assert ssh == [(NEW, {"idle": 900, "mode": "gpu"})]
    rc, grants = run_cli(capsys, "grants")
    assert grants["grants"][NEW]["budget_fen"] == 3000 and grants["grants"][NEW]["said"] == "租 m1 两张卡，跑两小时"


def test_private_image_goes_in_its_own_field(market, ssh, capsys):
    rent(capsys, "--image", "image-abc123", "--expand-disk", "20", "--yes", "--said", "ok")
    info = market.rented[0]["instance_info"]
    assert info["private_image_uuid"] == "image-abc123" and info["base_image_uuid"] == ""
    assert info["expand_data_disk"] == 20 << 30


def test_console_refusal_is_reported(market, ssh, capsys):
    market.fail["/api/v1/order/instance/create/payg"] = ("MachineGpuNumUpdateFailed", "该主机空闲GPU数量不足")
    rc, got = rent(capsys, "--yes", "--said", "ok")
    assert rc == cli.EXIT_REFUSED and got["code"] == "MachineGpuNumUpdateFailed" and "search again" in got["next"]
    rc, grants = run_cli(capsys, "grants")
    assert NEW not in grants["grants"] and ssh == []


def test_no_answer_finds_the_new_instance(market, ssh, capsys, monkeypatch):
    real = console.rent

    def lost(*args, **kw):
        real(*args, **kw)
        raise console.NoAnswer("rent: no answer (timed out)")
    monkeypatch.setattr(console, "rent", lost)
    rc, got = rent(capsys, "--yes", "--said", "ok")
    assert rc == 0 and got["instance"] == NEW and len(market.rented) == 1


def test_no_answer_and_nothing_listed_is_uncertain(market, ssh, capsys, monkeypatch):
    def lost(*args, **kw):
        raise console.NoAnswer("rent: no answer (timed out)")
    monkeypatch.setattr(console, "rent", lost)
    rc, got = rent(capsys, "--yes", "--said", "ok")
    assert rc == cli.EXIT_UNCERTAIN and got["uncertain"] and ssh == []


def test_images_lists(market, capsys):
    rc, got = run_cli(capsys, "images", "--framework", "pytorch")
    assert "base-image-gone" not in [i["uuid"] for i in got["images"]] and len(got["images"]) == 4
    rc, got = run_cli(capsys, "images", "--private")
    assert got["private_images"] == [{"uuid": "image-abc123", "name": "my-env"}]
