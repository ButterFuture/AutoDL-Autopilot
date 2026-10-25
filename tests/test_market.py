"""autopilot market: names resolved against the console's lists, every page read, filters and sort applied here
(the console ignores its free-GPU filter), and nothing but reads sent."""
from autopilot import cli, console
from conftest import ID, market_record, run_cli


def ids(got):
    return [m["id"] for m in got["machines"]]


def test_finds_rentable_machines_cheapest_first(fake, capsys):
    fake.machines = [market_record("a", li=2290), market_record("b", li=1980), market_record("c", idle=0),
                     market_record("d", on_line=5), market_record("e", slots=0), market_record("f", gpu="RTX 3080 Ti")]
    rc, got = run_cli(capsys, "market", "--gpu", "RTX 4090")
    assert rc == 0 and ids(got) == ["b", "a"] and got["read"] in ("all", "early")
    m = got["machines"][0]
    assert m["price_yuan_h"] == 1.98 and m["gpu_free"] == 1 and m["gpu_memory_gb"] == 24
    assert m["available_until"] == "2027-05-01" and "ip_address" not in repr(got)
    assert fake.writes() == []


def test_prices_follow_the_member_level(fake, capsys):
    # a vGPU-32GB host on a real account: list price 1660 li, a member pays 95%, 1577, billed as 1580
    fake.machines = [market_record("v", li=1660, member_li=1577), market_record("x", li=1600)]
    fake.level = "member_user"
    rc, got = run_cli(capsys, "market")
    m = {r["id"]: r for r in got["machines"]}
    assert m["v"]["price_yuan_h"] == 1.58 and m["v"]["list_price_yuan_h"] == 1.66
    assert m["x"]["price_yuan_h"] == 1.6                  # no price listed for the level: the list price
    assert ids(got) == ["v", "x"] and "member_user" in got["prices"]
    assert ids(run_cli(capsys, "market", "--max-price", "1.59")[1]) == ["v"]
    fake.level = "normal_user"
    rc, got = run_cli(capsys, "market")
    assert ids(got) == ["x", "v"] and got["machines"][1]["price_yuan_h"] == 1.66
    fake.fail["/api/v1/user/level"] = ("Unknown", "")
    rc, got = run_cli(capsys, "market")
    assert rc == 0 and got["machines"][1]["price_yuan_h"] == 1.66 and "could not be read" in got["prices"]
    assert fake.writes() == []


def test_member_price_rounds_to_the_fen():
    assert console.payg_li(market_record("a", li=1140, member_li=1083), "member_user") == 1080
    assert console.payg_li(market_record("a", li=1660, member_li=1577), "member_user") == 1580
    assert console.payg_li(market_record("a", li=1660, member_li=1577), None) == 1660
    assert console.payg_li(market_record("a", li=1660, member_li=1577), "member_level_9") == 1660


def test_partial_names_and_region_names(fake, capsys):
    fake.machines = [market_record("a"), market_record("b", gpu="RTX 4090D", region=("内蒙B区", "nm-B2")),
                     market_record("c", region=("西北B区", "west-B"))]
    rc, got = run_cli(capsys, "market", "--gpu", "4090", "--region", "内蒙")
    assert got["filters"]["gpu_models"] == ["RTX 4090", "RTX 4090D"]
    assert got["filters"]["regions"] == ["nm-B1", "nm-B2"] and sorted(ids(got)) == ["a", "b"]
    rc, got = run_cli(capsys, "market", "--region", "nm-B2")
    assert got["filters"]["regions"] == ["nm-B2"] and ids(got) == ["b"]


def test_unknown_name_lists_the_choices(fake, capsys):
    rc, got = run_cli(capsys, "market", "--gpu", "H100")
    assert rc == cli.EXIT_ERR and "RTX 4090D" in got["error"]


def test_filters_and_sort_by_free(fake, capsys):
    fake.machines = [market_record("a", idle=1, li=1000), market_record("b", idle=4, li=3000, cuda="12.2"),
                     market_record("c", idle=3, li=2000, cuda="12.8"), market_record("d", idle=2, li=9000)]
    assert ids(run_cli(capsys, "market", "--gpus", "2", "--max-price", "3")[1]) == ["c", "b"]
    assert ids(run_cli(capsys, "market", "--sort", "free", "--limit", "2")[1]) == ["b", "c"]
    assert ids(run_cli(capsys, "market", "--min-cuda", "12.4")[1]) == ["a", "c", "d"]
    assert ids(run_cli(capsys, "market", "--min-memory", "48")[1]) == []
    fake.machines.append(market_record("e", idle=0, li=10))
    assert ids(run_cli(capsys, "market", "--all")[1])[0] == "e"


def test_like_takes_the_instance_model_and_region(fake, capsys):
    fake.inst.update(snapshot_gpu_alias_name="RTX 3080 Ti", region_name="内蒙B区")
    fake.machines = [market_record("a", gpu="RTX 3080 Ti"), market_record("b"),
                     market_record("c", gpu="RTX 3080 Ti", region=("西北B区", "west-B"))]
    rc, got = run_cli(capsys, "market", "--like", ID)
    assert ids(got) == ["a"] and got["filters"]["gpu_models"] == ["RTX 3080 Ti"]


def pages_read(fake):
    return [b["page_index"] for p, b in fake.calls if p == "/api/v1/user/machine/list"]


def test_stops_after_the_free_block(fake, capsys, monkeypatch):
    monkeypatch.setattr(console, "MARKET_PAGE", 2)
    fake.machines = [market_record(f"z{i}", idle=0) for i in range(6)] + [market_record("a", li=2000),
                                                                         market_record("b", li=1000)]
    rc, got = run_cli(capsys, "market")
    assert ids(got) == ["b", "a"] and got["read"] == "early" and "--thorough" in got["read_note"]
    assert pages_read(fake) == [1, 2]                  # page 2 had nothing free: no need for pages 3 and 4
    assert all(b["gpu_idle_num"] == 1 for p, b in fake.calls if p == "/api/v1/user/machine/list")


def test_thorough_reads_every_page_and_finds_late_ones(fake, capsys, monkeypatch):
    monkeypatch.setattr(console, "MARKET_PAGE", 2)
    late = market_record("late", li=500, idle=1)
    late["listed_idle"] = 0                            # freed after the console counted it: listed late
    fake.machines = [market_record("a", li=2000)] + [market_record(f"z{i}", idle=0) for i in range(4)] + [late]
    rc, got = run_cli(capsys, "market")
    assert ids(got) == ["a"]
    fake.calls.clear()
    rc, got = run_cli(capsys, "market", "--thorough")
    assert ids(got) == ["late", "a"] and got["read"] == "all" and pages_read(fake) == [1, 2, 3]


def test_nothing_in_the_free_block_reads_on(fake, capsys, monkeypatch):
    monkeypatch.setattr(console, "MARKET_PAGE", 2)
    late = market_record("late", idle=1)
    late["listed_idle"] = 0
    fake.machines = [market_record(f"z{i}", idle=0) for i in range(4)] + [late]
    rc, got = run_cli(capsys, "market")
    assert ids(got) == ["late"] and got["read"] == "all"


def test_lists(fake, capsys):
    rc, got = run_cli(capsys, "market", "--list", "gpus")
    assert [g["name"] for g in got["gpu_models"]][:2] == ["A800-80GB", "RTX 4090"]
    rc, got = run_cli(capsys, "market", "--list", "regions")
    assert got["regions"][0] == {"name": "内蒙B区", "signs": ["nm-B1", "nm-B2"]}
