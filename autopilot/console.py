"""The AutoDL web console's HTTP API, for the console's part of the work.

This is the API the console's own web page uses, not a documented public one; the requests and answers were
checked on a real account (dev/probe/FINDINGS.md). Only the requests in ENDPOINTS are ever sent: reading the
instance list, the balance, the billing detail, the server time, the account's member level, the GPU market
(machines, regions, GPU models) and the images; renting one pay-as-you-go instance on a market machine; adding data disk space to an instance or
taking it away; powering on (with or without GPU) and off; setting or cancelling the timed shutdown. Nothing here
can release, reset, clone, migrate, renew or recharge.

Amounts in the console's answers are in li (a thousandth of a yuan); this module turns them into fen (a
hundredth of a yuan, integers) or yuan. Times are Beijing time (UTC+8).
"""
from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .creds import TokenError

BASE = "https://www.autodl.com"
TIMEOUT_S = 30
BJ = dt.timezone(dt.timedelta(hours=8))
NOGPU_FEN_H = 10        # no-GPU mode: 0.1 yuan an hour on every host
RELEASE_DAYS = 15       # an instance shut down this long is released by the platform

# name -> (method, path, changes something)
ENDPOINTS = {
    "time": ("GET", "/api/v1/time_sync", False),
    "balance": ("GET", "/api/v1/wallet/balance", False),
    "level": ("GET", "/api/v1/user/level", False),            # the account's member level, which sets its prices
    "instances": ("POST", "/api/v1/instance", False),        # a list query the console sends as POST
    "bills": ("POST", "/api/v1/bill/balance_statement/list", False),
    "market": ("POST", "/api/v1/user/machine/list", False),     # the market's machine list, also a POST query
    "regions": ("GET", "/api/v1/region/list", False),
    "gpu_models": ("GET", "/api/v1/machine/gpu_type", False),
    "images": ("POST", "/api/v1/image/get", False),
    "private_images": ("POST", "/api/v1/image/private/get", False),
    "rent": ("POST", "/api/v1/order/instance/create/payg", True),   # creates and starts a pay-as-you-go instance
    "snapshot": ("GET", "/api/v1/instance/snapshot", False),
    "disk_quote": ("POST", "/api/v1/order/data_disk/change/preview", False),
    "disk_change": ("POST", "/api/v1/order/data_disk/change", True),
    "power_on": ("POST", "/api/v1/instance/power_on", True),
    "power_off": ("POST", "/api/v1/instance/power_off", True),
    "timer": ("POST", "/api/v1/instance/timed/shutdown", True),
}
PAYLOAD = {"gpu": "gpu", "nogpu": "non_gpu"}
MARKET_PAGE = 100
NOT_RENTABLE = {3, 4, 5, 6}   # on_line values the market page treats as offline or under maintenance
GIB = 1 << 30

URLOPEN = urllib.request.urlopen   # replaced in tests
SLEEP = time.sleep                 # replaced in tests
MONO = time.monotonic              # replaced in tests


class ApiError(Exception):
    """The console answered with a code other than Success: the request was not carried out."""

    def __init__(self, name: str, code: str, msg: str):
        super().__init__(f"{name}: {code} {msg}".strip())
        self.code, self.msg = code, msg


class NoAnswer(Exception):
    """No answer could be read. A request that changes something may or may not have been carried out."""


def request(token: str, name: str, payload: dict | None = None):
    """Send one request of ENDPOINTS once; return the answer's data."""
    if name not in ENDPOINTS:
        raise ValueError(f"{name!r} is not a request Autopilot sends")
    method, path, _ = ENDPOINTS[name]
    url, body = BASE + path, None
    if method == "GET" and payload:
        url += "?" + urllib.parse.urlencode(payload)
    elif payload is not None:
        body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", token)
    req.add_header("Content-Type", "application/json;charset=UTF-8")
    req.add_header("User-Agent", "autodl-autopilot")
    try:
        with URLOPEN(req, timeout=TIMEOUT_S) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:   # still an answer: its code says what happened
        raw = e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise NoAnswer(f"{name}: no answer ({getattr(e, 'reason', e)})") from None
    try:
        answer = json.loads(raw)
    except json.JSONDecodeError:
        raise NoAnswer(f"{name}: the answer is not JSON") from None
    code = answer.get("code") if isinstance(answer, dict) else None
    if code == "Success":
        return answer.get("data")
    if code == "AuthorizeFailed":
        raise TokenError("the console refused the token (expired or logged out): take a new one from the logged-in "
                         "console and run autopilot token set")
    raise ApiError(name, str(code), str(answer.get("msg", "")) if isinstance(answer, dict) else "")


# ---- reading ----

def server_time(token: str) -> int:
    return int(request(token, "time"))


def balance_fen(token: str) -> int:
    data = request(token, "balance", {"charge_type": "payg", "product_type": "instance"}) or {}
    return int(data.get("assets") or 0) // 10


def level(token: str) -> str | None:
    """The account's member level as the console names it (normal_user, member_user, ...), or None when it
    cannot be read."""
    try:
        return str(request(token, "level") or "") or None
    except (ApiError, NoAnswer):
        return None


def records(token: str) -> list:
    """The instance list as the console sends it, secrets included: only view() of it leaves this module."""
    found = []
    for page in range(1, 21):
        data = request(token, "instances", {"date_from": "", "date_to": "", "page_index": page, "page_size": 50,
                                            "status": [], "charge_type": []}) or {}
        found += data.get("list") or []
        if page >= int(data.get("max_page") or 1):
            break
    return found


def instances(token: str) -> list:
    return [view(it) for it in records(token)]


def instance(token: str, iid: str) -> dict:
    for it in instances(token):
        if it["id"] == iid:
            return it
    raise LookupError(f"{iid} is not an instance of this account (released, or another account's token)")


def root_password(token: str, iid: str) -> str:
    """The instance's root password as the console shows it, read only to add the SSH key once; never printed
    or stored."""
    for it in records(token):
        if it.get("uuid") == iid:
            return str(it.get("root_password") or "")
    raise LookupError(f"{iid} is not an instance of this account (released, or another account's token)")


def stamp(t: int) -> str:
    """unix time -> 'YYYY-MM-DD HH:MM:SS' Beijing time."""
    return dt.datetime.fromtimestamp(t, BJ).strftime("%Y-%m-%d %H:%M:%S")


def unix(value) -> int | None:
    """The console's times ('2026-10-23T21:11:00+08:00', '2026-10-23 21:11:00', or {Time, Valid}) as unix time."""
    if isinstance(value, dict):
        if not value.get("Valid"):
            return None
        value = value.get("Time")
    if not value or str(value).startswith("0001-"):
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        t = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=BJ)
    return int(t.timestamp())


def view(it: dict) -> dict:
    """An instance as Autopilot uses it. Passwords, tokens and the SSH command are left out."""
    state = it.get("status")
    gpus = int(it.get("req_gpu_amount") or 0)
    idle = it.get("gpu_idle_num")
    stopped = unix(it.get("stopped_at"))
    return {
        "id": it.get("uuid"),
        "state": state,                     # running, starting, shutting_down, shutdown
        "mode": {"gpu": "gpu", "non_gpu": "nogpu"}.get(it.get("start_mode") or ""),   # the last start's mode
        "gpus": gpus,
        "gpu_idle": idle,
        "gpu_total": it.get("gpu_all_num"),
        "gpu_free": idle is not None and gpus > 0 and idle >= gpus,
        "price_fen_h": {"gpu": int(it.get("payg_price") or 0) // 10, "nogpu": NOGPU_FEN_H},
        "charge_type": it.get("charge_type"),
        "timer": unix(it.get("timed_shutdown_at")),
        "started_at": unix(it.get("started_at")),
        "stopped_at": stopped,
        "release_at": stopped + RELEASE_DAYS * 86400 if state == "shutdown" and stopped else None,
        "region": it.get("region_name"),
        "host_name": it.get("machine_alias"),
        "machine_id": it.get("machine_id"),
        "created_at": unix(it.get("created_at")),
        "gpu_model": it.get("snapshot_gpu_alias_name"),
        "ssh_host": it.get("proxy_host") or None,
        "ssh_port": it.get("ssh_port") or None,
    }


# ---- the GPU market ----

def regions(token: str) -> list:
    """[{sign, name}] for every region; one region name can have several signs (sub-areas)."""
    return [{"sign": r.get("region_sign"), "name": r.get("region_name")} for r in request(token, "regions") or []]


def gpu_models(token: str) -> list:
    """[{name, memory_gb, series}], the market's own order (newest and fastest first)."""
    found = sorted(request(token, "gpu_models") or [], key=lambda g: -int(g.get("gpu_rank") or 0))
    return [{"name": g.get("gpu_name"), "memory_gb": round(int(g.get("gpu_memory") or 0) / GIB),
             "series": g.get("gpu_series_type")} for g in found]


def payg_li(m: dict, level: str | None) -> int:
    """A market machine's pay-as-you-go price per GPU-hour in li for an account of that level. The market lists
    the list price (payg_price) and, in machine_sku_info, the price for each level (members pay 95%); the console
    bills that price rounded to the fen (1577 li is billed as 1580). The list price for a level it does not name."""
    for sku in m.get("machine_sku_info") or []:
        if sku.get("type") != "payg":
            continue
        for c in sku.get("level_config") or []:
            if level and c.get("level_name") == level and c.get("discounted_price"):
                return round(int(c["discounted_price"]) / 10) * 10
    return int(m.get("payg_price") or 0)


def machine(m: dict, level: str | None = None) -> dict:
    """A market machine as Autopilot shows it: what matters for choosing one, nothing about its network. Its
    price is the one an account of that level pays."""
    idle, total = int(m.get("gpu_idle_num") or 0), int(m.get("gpu_number") or 0)
    slots = int(m.get("max_instance_num") or 0) - int(m.get("binding_instance_num") or 0)
    return {
        "id": m.get("machine_id"),
        "region": m.get("region_name"),
        "region_sign": m.get("region_sign"),
        "host_name": m.get("machine_alias"),
        "gpu_model": m.get("gpu_name"),
        "gpu_memory_gb": round(int(m.get("gpu_memory") or 0) / GIB),
        "gpu_free": idle,
        "gpu_total": total,
        "price_yuan_h": payg_li(m, level) / 1000,                  # per GPU, pay as you go, at the level's price
        "list_price_yuan_h": int(m.get("payg_price") or 0) / 1000,
        "cuda": m.get("highest_cuda_version"),
        "cpu_per_gpu": m.get("cpu_per_gpu"),
        "mem_per_gpu_gb": round(int(m.get("mem_per_gpu") or 0) / GIB),
        "data_disk_gb": int(m.get("max_instance_disk_size") or 0) // GIB,
        "data_disk_expand_gb": int(m.get("max_data_disk_expand_size") or 0) // GIB,
        "chip": m.get("chip_corp"),
        "cpu_arch": m.get("cpu_arch"),
        "online": int(m.get("on_line") or 0) not in NOT_RENTABLE,
        "rentable": int(m.get("on_line") or 0) not in NOT_RENTABLE and idle > 0 and slots > 0,
        "available_until": stamp(unix(m.get("rent_deadline")))[:10] if unix(m.get("rent_deadline")) else None,
    }


def market_query(page: int, gpus=(), region_signs=(), need: int = 0, machine_id: str = "") -> dict:
    """The body the market page sends. With need > 0 the console lists the machines it counts as having that
    many free GPUs first and the others after them; it does not leave the others out."""
    body = {"charge_type": "payg", "region_sign_list": list(region_signs), "gpu_type_name": list(gpus),
            "machine_tag_name": [], "gpu_idle_num": need, "mount_net_disk": False, "cpu_arch": [], "chip_corp": [],
            "instance_disk_size_order": "", "date_range": "", "date_from": "", "date_to": "", "page_index": page,
            "page_size": MARKET_PAGE, "pay_price_order": "", "gpu_idle_type": "", "default_order": True}
    if machine_id:
        body["machine_id"] = machine_id
    return body


def market(token: str, gpus=(), region_signs=(), need: int = 0, early: bool = False, max_pages: int = 40,
           level: str | None = None) -> tuple:
    """(machines, how) on the pay-as-you-go market, filtered by GPU model names and region signs on the console's
    side, priced for an account of that level. how is "all" when every page was read, "early" when reading stopped at the first page without a
    rentable machine having need free GPUs (early=True; the console lists those first, so only machines freed
    in the last minutes can be missed), "capped" when max_pages ran out."""
    found = []
    for page in range(1, max_pages + 1):
        data = request(token, "market", market_query(page, gpus, region_signs, need)) or {}
        got = [machine(m, level) for m in data.get("list") or []]
        found += got
        if page >= int(data.get("max_page") or 1):
            return found, "all"
        if early and not any(m["rentable"] and m["gpu_free"] >= need for m in got):
            return found, "early"
    return found, "capped"


def market_machine(token: str, machine_id: str, level: str | None = None) -> dict:
    """One market machine, read fresh (free GPUs change by the minute), priced for an account of that level."""
    data = request(token, "market", market_query(1, machine_id=machine_id)) or {}
    for m in data.get("list") or []:
        if m.get("machine_id") == machine_id:
            return machine(m, level)
    raise LookupError(f"machine {machine_id} is not on the pay-as-you-go market")


def images(token: str) -> list:
    """The base images the console offers, newest last: [{uuid, framework, version, python, cuda, chip, cpu_arch}]."""
    return [{"uuid": i.get("uuid"), "framework": i.get("framework_name"), "version": i.get("framework_version"),
             "python": i.get("python_version"), "cuda": i.get("cuda_version"), "chip": i.get("chip_corp"),
             "cpu_arch": i.get("cpu_arch")}
            for i in request(token, "images", {}) or [] if not i.get("is_off")]


def private_images(token: str) -> list:
    """The account's own saved images: [{uuid, name}]."""
    return [{"uuid": i.get("image_uuid"), "name": i.get("name")} for i in request(token, "private_images", {}) or []]


def rent(token: str, machine_id: str, gpus: int, image: str, private: bool = False, expand_gb: int = 0) -> str:
    """Rent a pay-as-you-go instance with gpus GPUs on machine_id, sent once. The console creates it and starts
    it at once (billing starts); the answer is the new instance's ID."""
    extra = expand_gb * GIB
    data = request(token, "rent", {
        "instance_info": {"machine_id": machine_id, "charge_type": "payg", "req_gpu_amount": gpus,
                          "base_image_uuid": "" if private else image, "private_image_uuid": image if private else "",
                          "reproduction_uuid": "", "cg_application_uuid": "",
                          "cg_application_info": {"image_name": "", "current_version_id": 0, "current_version": "",
                                                  "image_id": 0},
                          "instance_name": "", "expand_data_disk": extra, "reproduction_id": 0},
        "price_info": {"coupon_id_list": [], "machine_id": machine_id, "charge_type": "payg", "duration": 1,
                       "num": gpus, "expand_data_disk": extra}})
    if not isinstance(data, str) or not data:
        raise NoAnswer(f"rent: the answer has no instance ID ({type(data).__name__})")
    return data


# ---- the data disk ----

SHUT = {"shutdown", "shutdown_by_starting_error", "shutdown_by_running_error"}


def disk_quote(token: str, iid: str, machine_id: str, change_gb: int, shrink: bool = False) -> dict:
    """What the data disk would be after adding (or, shrink=True, taking away) change_gb, read from the console's
    price preview without ordering anything: sizes in GB and the daily price in fen of the extra space."""
    snap = request(token, "snapshot", {"instance_uuid": iid}) or {}
    base = int((snap.get("machine_info_snapshot") or {}).get("max_instance_disk_size") or 0)
    extra = int(snap.get("expand_data_disk_size") or 0)
    change = change_gb * GIB
    after = extra - change if shrink else extra + change
    q = request(token, "disk_quote", {"change_size": change, "data_disk_expand_size": after, "instance_uuid": iid,
                                      "machine_id": machine_id,
                                      "opt_type": "data_disk_reduce" if shrink else "data_disk_expand"}) or {}
    return {"included_gb": base // GIB, "extra_gb": extra // GIB, "extra_after_gb": after // GIB,
            "used_gb": round(int(q.get("data_disk_used") or 0) / GIB, 2),
            "can_add_gb": int(q.get("max_expand_size") or 0) // GIB,
            "can_remove_gb": int(q.get("max_reduce_size") or 0) // GIB,
            "day_fen_after": int(q.get("data_disk_deal_daily_price") or 0) / 10,
            "prepaid": bool(q.get("prepay_expand_data_disk"))}


def disk_change(token: str, iid: str, machine_id: str, change_gb: int, shrink: bool = False) -> None:
    """Order the change once, with the body the console's own dialog sends."""
    change = change_gb * GIB
    request(token, "disk_change", {"change_size": change, "data_disk_expand_size": change, "instance_uuid": iid,
                                   "machine_id": machine_id,
                                   "opt_type": "data_disk_reduce" if shrink else "data_disk_expand"})


def bills(token: str, since: int, max_pages: int = 20) -> tuple:
    """Billing detail rows from newest back to since (unix time): (rows, complete). complete is False when
    max_pages ran out before reaching since."""
    rows = []
    for page in range(1, max_pages + 1):
        data = request(token, "bills", {"date_from": "", "date_to": "", "page_index": page, "page_size": 50,
                                        "balance_statement_type": "", "bill_type": ""}) or {}
        got = data.get("list") or []
        rows += got
        if not got or page >= int(data.get("max_page") or 1) or (unix(got[-1].get("created_at")) or 0) < since:
            return rows, True
    return rows, False


def charges(rows: list, since: int) -> list:
    """The charge rows at or after since, as {serial, instance, at, fen, kind, gpu, to}. kind is charge_settle (on
    the hour), charge_shutdown (at power-off) or data_disk_settle (an expanded disk's daily fee)."""
    found = []
    for r in rows:
        if r.get("bill_type") != "charge" or r.get("balance_statement_type") != "expenditure":
            continue
        at = unix(r.get("created_at"))
        li = int(r.get("asset") or 0)
        if at is None or at < since or li <= 0:
            continue
        found.append({"serial": str(r.get("uuid")), "instance": r.get("product_uuid"), "at": at,
                      "fen": (li + 5) // 10, "kind": r.get("bill_sub_type"),
                      "gpu": r.get("charge_type") != "payg_no_gpu",
                      "to": unix((r.get("details") or {}).get("charge_to"))})
    return found


# ---- changing ----

def power_on(token: str, iid: str, mode: str) -> None:
    request(token, "power_on", {"instance_uuid": iid, "payload": PAYLOAD[mode]})


def power_off(token: str, iid: str) -> None:
    request(token, "power_off", {"instance_uuid": iid})


def set_timer(token: str, iid: str, at: int | None) -> None:
    """Shut the instance down at unix time at (rounded up to the minute, as the console takes it); None cancels."""
    when = None
    if at is not None:
        at = -(-at // 60) * 60
        when = dt.datetime.fromtimestamp(at, BJ).strftime("%Y-%m-%d %H:%M")
    request(token, "timer", {"instance_uuid": iid, "shutdown_at": when})


def wait_state(token: str, iid: str, want: str, timeout_s: int, every_s: int = 3) -> tuple:
    """Read the instance until its state is want: (instance, the states seen on the way). TimeoutError otherwise."""
    seen, deadline = [], MONO() + timeout_s
    while True:
        it = instance(token, iid)
        if not seen or seen[-1] != it["state"]:
            seen.append(it["state"])
        if it["state"] == want:
            return it, seen
        if MONO() >= deadline:
            raise TimeoutError(f"{iid} is {it['state']}, not {want}, after {timeout_s}s (seen: {', '.join(seen)})")
        SLEEP(every_s)
