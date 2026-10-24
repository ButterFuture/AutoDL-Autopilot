"""autopilot disk: the data disk shown, a plan that orders nothing and says when the change takes effect, then
with --yes and the user's words one order, read back. Removing space only while shut down."""
from autopilot import cli
from conftest import ID, run_cli


def orders(fake):
    return [b for p, b in fake.writes() if p == "/api/v1/order/data_disk/change"]


def test_show(fake, capsys):
    rc, got = run_cli(capsys, "disk", ID)
    assert rc == 0 and got["disk"]["included_gb"] == 50 and got["disk"]["extra_gb"] == 0
    assert got["disk"]["can_add_gb"] == 900 and got["mount"] == "/root/autodl-tmp" and fake.writes() == []


def test_add_plan_when_shut_down(fake, capsys):
    rc, got = run_cli(capsys, "disk", ID, "--add", "20")
    assert rc == 0 and got["sent"] is False and orders(fake) == []
    plan = got["plan"]
    assert plan["total_gb_after"] == 70 and plan["extra_yuan_day_after"] == 0.14      # 7 li per GB per day, x 20 = 140 li
    assert plan["takes_effect"] == "at the next power-on" and "15 days" in plan["reminder"]


def test_add_plan_when_running_says_restart(fake, capsys):
    fake.inst.update(status="running", start_mode="gpu")
    rc, got = run_cli(capsys, "disk", ID, "--add", "20")
    effect = got["plan"]["takes_effect"]
    assert effect.startswith("only after a restart") and "GPU is released" in effect
    fake.inst.update(start_mode="non_gpu")
    effect = run_cli(capsys, "disk", ID, "--add", "20")[1]["plan"]["takes_effect"]
    assert effect.startswith("only after a restart") and "GPU" not in effect


def test_add_with_yes(fake, capsys):
    rc, got = run_cli(capsys, "disk", ID, "--add", "20", "--yes")
    assert rc == cli.EXIT_ERR and "--said" in got["error"] and orders(fake) == []
    rc, got = run_cli(capsys, "disk", ID, "--add", "20", "--yes", "--said", "加 20G")
    assert rc == 0 and got["ok"] and got["extra_gb"] == 20 and got["total_gb"] == 70
    assert orders(fake) == [{"change_size": 20 << 30, "data_disk_expand_size": 20 << 30, "instance_uuid": ID,
                             "machine_id": None, "opt_type": "data_disk_expand"}]


def test_add_over_the_host_limit(fake, capsys):
    rc, got = run_cli(capsys, "disk", ID, "--add", "901", "--yes", "--said", "ok")
    assert rc == cli.EXIT_REFUSED and orders(fake) == []


def test_remove_only_when_shut_down(fake, capsys):
    fake.extra_gb = fake.removable_gb = 30
    fake.inst["status"] = "running"
    rc, got = run_cli(capsys, "disk", ID, "--remove", "10", "--yes", "--said", "ok")
    assert rc == cli.EXIT_REFUSED and "shut down" in got["refused"] and orders(fake) == []
    fake.inst["status"] = "shutdown"
    rc, got = run_cli(capsys, "disk", ID, "--remove", "10")
    assert got["plan"]["extra_gb_after"] == 20 and "back up" in got["plan"]["warning"]
    rc, got = run_cli(capsys, "disk", ID, "--remove-all", "--yes", "--said", "不要扩容了")
    assert rc == 0 and got["extra_gb"] == 0 and orders(fake)[-1]["opt_type"] == "data_disk_reduce"


def test_remove_more_than_is_free(fake, capsys):
    fake.extra_gb, fake.removable_gb = 30, 5
    rc, got = run_cli(capsys, "disk", ID, "--remove-all", "--yes", "--said", "ok")
    assert rc == cli.EXIT_REFUSED and "at most 5 GB" in got["refused"] and orders(fake) == []


def test_remove_needs_pay_as_you_go(fake, capsys):
    fake.extra_gb = fake.removable_gb = 10
    fake.inst["charge_type"] = "monthly"
    rc, got = run_cli(capsys, "disk", ID, "--remove", "5", "--yes", "--said", "ok")
    assert rc == cli.EXIT_REFUSED and orders(fake) == []


def test_off_reminds_to_back_up(fake, capsys, monkeypatch):
    from autopilot import remote
    fake.inst["status"] = "running"

    def shutdown(r, force):
        fake.inst["status"] = "shutdown"
        return 0, {"ok": True}
    monkeypatch.setattr(remote, "shutdown", shutdown)
    rc, got = run_cli(capsys, "off", ID)
    assert rc == 0 and "15 days" in got["reminder"] and "autopilot pull" in got["reminder"]
