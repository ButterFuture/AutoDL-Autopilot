"""autopilot: the command line the AI (or a person) uses. Every command prints one JSON object.

Exit codes: 0 done; 1 error; 2 unreachable (nothing was done); 3 refused (nothing was done); 6 uncertain (a
request was sent and no answer came: read the state before doing anything else); 10 over budget; 12 no grant;
13 the SSH address leads to another machine; 14 no usable console token.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

from . import __version__, budget, console, creds, paths, remote, storage
from .state import State, StateError

EXIT_ERR, EXIT_UNREACHABLE, EXIT_REFUSED, EXIT_UNCERTAIN = 1, 2, 3, 6
EXIT_BUDGET, EXIT_NO_GRANT, EXIT_WRONG_HOST, EXIT_TOKEN = 10, 12, 13, 14
INSTANCE_RE = re.compile(r"^[0-9a-z]{6,16}-[0-9a-f]{6,12}\Z")
JOB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
DEFAULTS = {"key": "", "workdir": "/root/autodl-tmp", "env": "", "idle_min": 15, "idle_min_nogpu": 60, "fallback_min": 30,
            "guard_default": True, "add_key": True, "storage_check": True}
BOOL = {"true": True, "on": True, "yes": True, "1": True, "false": False, "off": False, "no": False, "0": False}
SSH_READY_S = 180          # how long to keep trying SSH after the console says running
NOW = time.time            # replaced in tests
SLEEP = time.sleep         # replaced in tests


def now() -> int:
    return int(NOW())


def out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def instance_id(text: str) -> str:
    if not INSTANCE_RE.match(text or ""):
        raise ValueError(f"{text!r} is not an instance ID as the console shows it (like abcd123456-1234abcd)")
    return text


def duration_s(text: str) -> int:
    """90s, 30m, 2h, 7d, or plain minutes."""
    m = re.match(r"^(\d{1,6})([smhd]?)\Z", text or "")
    if not m:
        raise ValueError(f"duration {text!r}: like 90s, 30m, 2h, 7d, or minutes")
    return int(m.group(1)) * {"s": 1, "m": 60, "": 60, "h": 3600, "d": 86400}[m.group(2)]


# ---- settings ----

def settings() -> dict:
    path = paths.config_dir() / "config.json"
    data = dict(DEFAULTS)
    if path.exists():
        data.update(json.loads(path.read_text(encoding="utf-8")))
    return data


def save_settings(data: dict) -> None:
    paths.write_private(paths.config_dir() / "config.json", json.dumps(data, ensure_ascii=False, indent=1))


def remote_for(inst: dict) -> remote.Remote:
    """SSH to the instance with the configured key. When the instance refuses the key and add_key is on, the key
    is added once with the root password the console shows for it, and the command goes on."""
    conf = settings()
    r = remote.Remote(inst["id"], inst["ssh_host"], inst["ssh_port"], conf["key"] or None)
    if conf["key"] and conf["add_key"]:
        def login(rem):
            pub = pathlib.Path(str(pathlib.Path(conf["key"]).expanduser()) + ".pub").read_text(encoding="utf-8")
            token, _ = creds.find()
            remote.add_key(rem, console.root_password(token, inst["id"]), pub)
        r.login = login
    return r


# ---- token, settings, doctor ----

def cmd_token(a) -> int:
    if a.action == "set":
        if sys.stdin.isatty():
            print("Paste the token (copy(localStorage.token) in the logged-in console), then press Enter:",
                  file=sys.stderr)
        path = creds.save(sys.stdin.readline())
        token, _ = creds.find("file")
        out({"ok": True, "saved": str(path), **creds.times(token)})
        return 0
    if a.action == "forget":
        gone = creds.forget()
        out({"ok": True, "removed": str(gone) if gone else None,
             "note": "a token in AUTODL_WEB_JWT or in the OS credential store is left as it is"})
        return 0
    token, source = creds.find(a.source)
    res = {"ok": True, "source": source, **creds.times(token)}
    if a.verify:
        console.balance_fen(token)     # needs the login; a refused token raises TokenError
        res["accepted"] = True
    out(res)
    return 0


def cmd_config(a) -> int:
    data = settings()
    if a.action == "set":
        if a.key not in DEFAULTS:
            raise ValueError(f"unknown setting {a.key!r}: one of {', '.join(DEFAULTS)}")
        value = a.value
        if a.key in ("idle_min", "idle_min_nogpu", "fallback_min"):
            value = int(value)
            if value < 1:
                raise ValueError(f"{a.key} must be at least 1")
        if a.key in ("guard_default", "add_key", "storage_check"):
            if str(value).lower() not in BOOL:
                raise ValueError(f"{a.key}: true or false")
            value = BOOL[str(value).lower()]
        if a.key == "workdir" and ("'" in value or not value.startswith("/")):
            raise ValueError("workdir: an absolute path on the instance, without quotes")
        data[a.key] = value
        save_settings(data)
    out(data)
    return 0


def cmd_keygen(a) -> int:
    path = pathlib.Path(a.path).expanduser()
    if not path.exists():
        if not shutil.which("ssh-keygen"):
            raise RuntimeError("no ssh-keygen on this computer (it comes with the OpenSSH client)")
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "autodl-autopilot", "-f", str(path)],
                       check=True, stdin=subprocess.DEVNULL)
    data = settings()
    data["key"] = str(path)
    save_settings(data)
    pub = pathlib.Path(str(path) + ".pub").read_text(encoding="utf-8").strip()
    out({"ok": True, "key": str(path), "public_key": pub,
         "next": "nothing to paste: the first connection adds the key to the instance (autopilot connect ID); "
                 "or paste public_key into the console's 设置SSH免密登录 for every instance of the account"})
    return 0


def cmd_doctor(a) -> int:
    checks = {"python": sys.version.split()[0], "python_ok": sys.version_info >= (3, 8),
              "ssh": shutil.which("ssh"), "scp": shutil.which("scp"), "config_dir": str(paths.config_dir()),
              "state_dir": str(paths.state_dir())}
    try:
        token, source = creds.find()
        checks["token"] = {"source": source, **creds.times(token)}
    except creds.TokenError as e:
        checks["token"] = {"error": str(e)}
    key = settings()["key"]
    checks["key"] = {"path": key or None, "exists": bool(key) and pathlib.Path(key).expanduser().exists()}
    ok = checks["python_ok"] and checks["ssh"] and checks["scp"] and "source" in checks["token"] and checks["key"]["exists"]
    out({"ok": bool(ok), **checks})
    return 0 if ok else EXIT_ERR


# ---- reading the console ----

def cmd_status(a) -> int:
    token, _ = creds.find()
    if a.instance is None:
        out({"instances": console.instances(token)})
        return 0
    inst = console.instance(token, instance_id(a.instance))
    res = dict(inst)
    if a.guard and inst["state"] == "running":
        res["guard"] = remote.status(remote_for(inst))
    out(res)
    return 0


def cmd_balance(a) -> int:
    token, _ = creds.find()
    out({"balance_yuan": console.balance_fen(token) / 100})
    return 0


def sync_bills(token: str, since: int) -> dict:
    rows, complete = console.bills(token, since)
    found = console.charges(rows, since)
    with State() as st:
        added = st.add_bills(found)
    return {"rows": len(found), "added": added, "complete": complete,
            "since": console.stamp(since)}


def cmd_sync(a) -> int:
    token, _ = creds.find()
    since = budget.month_start(now()) if a.since is None else console.unix(a.since + " 00:00:00")
    if since is None:
        raise ValueError(f"--since {a.since!r}: YYYY-MM-DD")
    out(sync_bills(token, since))
    return 0


def cmd_spend(a) -> int:
    start = budget.month_start(now())
    with State() as st:
        ids = [instance_id(a.instance)] if a.instance else sorted(set(st.data["grants"]) | set(st.data["bills"]))
        res = []
        for iid in ids:
            g = st.data["grants"].get(iid)
            spent = budget.spent_fen(st.data["bills"].get(iid, {}), start)
            row = {"instance": iid, "spent_yuan": spent / 100,
                   "budget_yuan": None if not g or g["budget_fen"] is None else g["budget_fen"] / 100}
            if row["budget_yuan"] is not None:
                row["left_yuan"] = round(row["budget_yuan"] - row["spent_yuan"], 2)
            res.append(row)
    out({"month_from": console.stamp(start), "instances": res,
         "note": "from the cached bills: run autopilot sync first for the latest"})
    return 0


# ---- grants and the budget ----

def cmd_grant(a) -> int:
    iid = instance_id(a.instance)
    g = budget.make_grant(a.modes, budget.parse_yuan(a.budget), a.said, now())
    with State() as st:
        st.data["grants"][iid] = g
        st.event(iid, "grant", modes=g["modes"], budget_fen=g["budget_fen"])
    out({"ok": True, "instance": iid, **g})
    return 0


def cmd_grants(a) -> int:
    with State() as st:
        out({"grants": st.data["grants"]})
    return 0


def cmd_revoke(a) -> int:
    iid = instance_id(a.instance)
    with State() as st:
        gone = st.data["grants"].pop(iid, None)
        if gone:
            st.event(iid, "revoke")
    out({"ok": True, "instance": iid, "revoked": gone is not None})
    return 0


def judge(token: str, inst: dict, mode: str, hours: float) -> dict:
    sync_bills(token, budget.month_start(now()))
    with State() as st:
        g = st.data["grants"].get(inst["id"])
        book = st.data["bills"].get(inst["id"], {})
        return budget.judge(g, inst, mode, hours, book, now())


def cmd_check(a) -> int:
    token, _ = creds.find()
    inst = console.instance(token, instance_id(a.instance))
    res = judge(token, inst, a.mode, a.hours)
    out({"instance": inst["id"], "mode": a.mode, "hours": a.hours, **res})
    if res["ok"]:
        return 0
    return EXIT_NO_GRANT if res["reason"].startswith("no grant") else EXIT_BUDGET


# ---- the GPU market ----

def pick(wanted: list, names: list, what: str) -> list:
    """The console's names for what the user typed: an exact match (any case) or else every name containing it,
    spaces ignored ("4090" gives RTX 4090 and RTX 4090D)."""
    def flat(x):
        return x.lower().replace(" ", "")
    chosen = []
    for w in wanted:
        hits = [n for n in names if n.lower() == w.lower()] or [n for n in names if flat(w) in flat(n)]
        if not hits:
            raise ValueError(f"no {what} matches {w!r}; the console has: {', '.join(names)}")
        chosen += [h for h in hits if h not in chosen]
    return chosen


def version(text) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(text or ""))[:2]) or (0,)


def fits(m: dict, a, need: int) -> bool:
    return ((a.all or (m["rentable"] and m["gpu_free"] >= need))
            and (a.max_price is None or m["price_yuan_h"] <= a.max_price)
            and (a.min_cuda is None or version(m["cuda"]) >= version(a.min_cuda))
            and (a.min_memory is None or m["gpu_memory_gb"] >= a.min_memory))


def cmd_market(a) -> int:
    token, _ = creds.find()
    if a.list == "gpus":
        out({"gpu_models": console.gpu_models(token)})
        return 0
    regions = console.regions(token)
    if a.list == "regions":
        names = {}
        for r in regions:
            names.setdefault(r["name"], []).append(r["sign"])
        out({"regions": [{"name": n, "signs": s} for n, s in names.items()]})
        return 0
    gpu, region = list(a.gpu or []), list(a.region or [])
    if a.like:
        inst = console.instance(token, instance_id(a.like))
        gpu = gpu or [inst["gpu_model"]]
        region = region or [inst["region"]]
    gpus = pick(gpu, [g["name"] for g in console.gpu_models(token)], "GPU model") if gpu else []
    signs = []
    if region:
        hits = pick(region, list(dict.fromkeys([r["name"] for r in regions] + [r["sign"] for r in regions])),
                    "region")
        signs = [r["sign"] for r in regions if r["name"] in hits or r["sign"] in hits]
    need = 0 if a.all else a.gpus
    machines, how = console.market(token, gpus, signs, need, early=not (a.thorough or a.all))
    if how == "early" and not any(fits(m, a, need) for m in machines):      # nothing in the free block: read on
        machines, how = console.market(token, gpus, signs, need)
    rows = [m for m in machines if fits(m, a, need)]
    if a.sort == "free":
        rows.sort(key=lambda m: (-m["gpu_free"], m["price_yuan_h"]))
    else:
        rows.sort(key=lambda m: (m["price_yuan_h"], -m["gpu_free"]))
    res = {"ok": True, "filters": {"gpu_models": gpus or "all", "regions": sorted(set(signs)) or "all",
                                   "free_gpus_at_least": None if a.all else a.gpus, "max_price_yuan_h": a.max_price,
                                   "min_cuda": a.min_cuda, "min_gpu_memory_gb": a.min_memory, "sort": a.sort},
           "scanned": len(machines), "read": how, "found": len(rows), "machines": rows[:a.limit],
           "note": "prices are per GPU per hour, pay as you go; free GPUs change by the minute. Rent one with "
                   "autopilot rent MACHINE --gpus N ... (a plan first, --yes to rent)"}
    if how == "early":
        res["read_note"] = ("stopped after the machines the console lists as free; one freed in the last minutes "
                            "can be missing (--thorough reads the whole market)")
    if how == "capped":
        res["warning"] = "the market has more machines than one search reads; narrow it with --gpu or --region"
    out(res)
    return 0


def cmd_images(a) -> int:
    token, _ = creds.find()
    if a.private:
        out({"private_images": console.private_images(token)})
        return 0
    found = console.images(token)
    if a.framework:
        found = [i for i in found if a.framework.lower() in (i["framework"] or "").lower()]
    out({"images": found})
    return 0


def choose_image(token: str, spec: str, m: dict) -> dict:
    """The image for spec on machine m: a base image ID, a private image ID, or FRAMEWORK[:VERSION] (the newest
    match whose chip and CPU fit the machine and whose CUDA its driver supports)."""
    if spec.startswith("base-image-"):
        hits = [i for i in console.images(token) if i["uuid"] == spec]
        if not hits:
            raise ValueError(f"no base image {spec} (autopilot images lists them)")
        image = hits[0]
    elif spec.startswith("image-"):
        hits = [i for i in console.private_images(token) if i["uuid"] == spec]
        if not hits:
            raise ValueError(f"no private image {spec} in this account (autopilot images --private)")
        return {**hits[0], "private": True}
    else:
        name, _, ver = spec.partition(":")
        cands = [i for i in console.images(token) if (i["framework"] or "").lower() == name.lower()
                 and (not ver or (i["version"] or "").startswith(ver))
                 and i["chip"] == m["chip"] and i["cpu_arch"] == m["cpu_arch"]
                 and (m["chip"] != "nvidia" or version(i["cuda"]) <= version(m["cuda"]))]
        if not cands:
            raise ValueError(f"no image {spec!r} fits machine {m['id']} ({m['chip']}, {m['cpu_arch']}, CUDA up to "
                             f"{m['cuda']}): autopilot images lists them")
        image = max(cands, key=lambda i: (version(i["version"]), version(i["cuda"])))
    if image["chip"] != m["chip"] or image["cpu_arch"] != m["cpu_arch"]:
        raise ValueError(f"image {image['uuid']} is for {image['chip']}/{image['cpu_arch']}, the machine is "
                         f"{m['chip']}/{m['cpu_arch']}")
    if m["chip"] == "nvidia" and version(image["cuda"]) > version(m["cuda"]):
        raise ValueError(f"image {image['uuid']} needs CUDA {image['cuda']}, the machine's driver goes up to {m['cuda']}")
    return {**image, "private": False}


def new_instance_on(token: str, machine_id: str, since: int) -> dict | None:
    """The instance created on machine_id at or after since, if the console lists one."""
    hits = [i for i in console.instances(token)
            if i["machine_id"] == machine_id and (i["created_at"] or 0) >= since - 120]
    return max(hits, key=lambda i: i["created_at"] or 0) if hits else None


def cmd_rent(a) -> int:
    if a.gpus < 1:
        raise ValueError("--gpus: at least 1")
    token, _ = creds.find()
    conf = settings()
    m = console.market_machine(token, a.machine)
    plan = {"machine": a.machine, "region": m["region"], "host_name": m["host_name"], "gpu_model": m["gpu_model"],
            "gpus": a.gpus, "gpu_free_now": m["gpu_free"], "price_yuan_h": round(m["price_yuan_h"] * a.gpus, 3),
            "hours": a.hours, "planned_yuan": round(m["price_yuan_h"] * a.gpus * a.hours, 2),
            "data_disk_gb": m["data_disk_gb"] + a.expand_disk, "cuda": m["cuda"]}
    if not m["rentable"] or m["gpu_free"] < a.gpus:
        out({"ok": False, "sent": False, "refused": f"machine {a.machine} has {m['gpu_free']} free GPUs now"
             + ("" if m["online"] else " and is offline") + f", {a.gpus} needed: search again with autopilot market",
             "plan": plan})
        return EXIT_REFUSED
    if a.expand_disk and a.expand_disk > m["data_disk_expand_gb"]:
        raise ValueError(f"--expand-disk: the machine can add at most {m['data_disk_expand_gb']} GB")
    image = choose_image(token, a.image, m)
    plan["image"] = image
    budget_fen = budget.parse_yuan(a.budget)
    planned_fen = round(m["price_yuan_h"] * a.gpus * a.hours * 100)
    if budget_fen is not None and planned_fen > budget_fen:
        out({"ok": False, "sent": False, "refused": f"the plan ({planned_fen / 100:.2f} yuan) is over the budget "
             f"({budget_fen / 100:.2f} yuan)", "plan": plan})
        return EXIT_BUDGET
    balance = console.balance_fen(token)
    plan["balance_yuan"] = balance / 100
    if balance < planned_fen:
        out({"ok": False, "sent": False, "refused": f"the balance ({balance / 100:.2f} yuan) does not cover the "
             f"planned {planned_fen / 100:.2f} yuan", "plan": plan})
        return EXIT_REFUSED
    fs = storage_unready(token, m["region"], a.without_storage)
    if fs and a.yes:
        out({"ok": False, "sent": False, **storage_refusal(fs), "plan": plan})
        return EXIT_REFUSED
    if not a.yes:
        if fs:
            plan["storage"] = {**fs, "note": storage_refusal(fs)["refused"] + f"; autopilot storage init "
                               f"{fs['sign']} before renting, or rent with --without-storage"}
        out({"ok": True, "sent": False, "plan": plan,
             "tip": "a new instance starts empty: suggest downloading models and data in no-GPU mode first "
                    "(off, then on --mode nogpu at 0.1 yuan/h), then GPU mode to train; while off the GPU is "
                    "not kept, so on a host with few free GPUs it may be gone",
             "next": "show this plan to the user; once they agree, run the same command with --yes and --said "
                     "'<their words>'. Renting starts the instance and its billing at once"})
        return 0
    g = budget.make_grant(a.modes, budget_fen, a.said or "", now())    # refuses an empty --said
    t0 = console.server_time(token)
    try:
        iid = console.rent(token, a.machine, a.gpus, image["uuid"], image["private"], a.expand_disk)
    except console.ApiError as e:
        out({"ok": False, "sent": True, "done": False, "code": e.code, "msg": e.msg, "plan": plan,
             "next": {"MachineGpuNumUpdateFailed": "the GPUs were taken meanwhile: search again",
                      "TORecharge": "the balance is too low: the user recharges in the console",
                      "TORealName": "the account needs real-name verification in the console first"}.get(e.code)})
        return EXIT_REFUSED
    except console.NoAnswer:
        SLEEP(10)
        found = new_instance_on(token, a.machine, t0)
        if not found:
            out({"ok": None, "uncertain": True, "plan": plan, "next": "the rent request got no answer and no new "
                 "instance is listed yet: read autopilot status in a minute before renting again"})
            return EXIT_UNCERTAIN
        iid = found["id"]
    with State() as st:
        st.data["grants"][iid] = g
        st.event(iid, "rent", at=t0, machine=a.machine, gpus=a.gpus, image=image["uuid"],
                 price_fen_h=round(m["price_yuan_h"] * a.gpus * 100), hours=a.hours)
        st.event(iid, "on", at=t0, mode="gpu", price_fen_h=round(m["price_yuan_h"] * a.gpus * 100), hours=a.hours)
    res = {"ok": True, "instance": iid, "rented": True, "mode": "gpu", "price_yuan_h": plan["price_yuan_h"],
           "image": image, "grant": {k: g[k] for k in ("modes", "budget_fen")}}
    # the fallback as soon as the instance exists (set again once it runs, if the console refused it while the
    # instance was being created): until the guard runs, the console shuts it down
    fallback_at = t0 + 60 * (a.fallback or conf["fallback_min"])
    try:
        set_timer_checked(token, iid, fallback_at)
    except (console.ApiError, LookupError):
        pass
    res["fallback_timer"] = console.stamp(fallback_at)
    try:
        return bring_up(token, iid, a, res, fallback_at, idle_for("gpu", a.idle), t0, 900)
    except TimeoutError as e:
        out({**res, "ok": False, "error": str(e), "next": "read autopilot status ID; the image may still be loading"})
        return EXIT_ERR


# ---- the data disk ----

def cmd_disk(a) -> int:
    """The data disk of an instance: show it, or add space (--add) or take extra space away (--remove, --remove-all).
    Without --yes only the plan."""
    iid = instance_id(a.instance)
    token, _ = creds.find()
    inst = console.instance(token, iid)
    shrink = bool(a.remove or a.remove_all)
    if a.add and shrink:
        raise ValueError("--add or --remove, not both")
    q0 = console.disk_quote(token, iid, inst["machine_id"], 0)
    now_disk = {"included_gb": q0["included_gb"], "extra_gb": q0["extra_gb"], "total_gb": q0["included_gb"] + q0["extra_gb"],
                "used_gb": q0["used_gb"], "can_add_gb": q0["can_add_gb"], "can_remove_gb": q0["can_remove_gb"],
                "extra_yuan_day": round(q0["day_fen_after"] / 100, 3)}
    if not a.add and not shrink:
        out({"instance": iid, "state": inst["state"], "disk": now_disk, "mount": "/root/autodl-tmp"})
        return 0
    change = a.add or (q0["extra_gb"] if a.remove_all else a.remove)
    if change < 1:
        raise ValueError("the change is at least 1 GB" + (" (there is no extra space to remove)" if a.remove_all else ""))
    running = inst["state"] not in console.SHUT
    if shrink:
        if running:
            out({"ok": False, "sent": False, "instance": iid, "disk": now_disk, "refused": "extra space can only be "
                 "removed while the instance is shut down (the console's rule, so data stays consistent): back up "
                 "what matters, autopilot off ID, then run this again"})
            return EXIT_REFUSED
        if inst["charge_type"] != "payg":
            out({"ok": False, "sent": False, "instance": iid, "refused": "only pay-as-you-go instances can remove "
                 "space before their term ends"})
            return EXIT_REFUSED
        if change > q0["can_remove_gb"]:
            out({"ok": False, "sent": False, "instance": iid, "disk": now_disk, "refused": f"at most "
                 f"{q0['can_remove_gb']} GB can be removed now (extra space that is free); delete or move files first"})
            return EXIT_REFUSED
    elif change > q0["can_add_gb"]:
        out({"ok": False, "sent": False, "instance": iid, "disk": now_disk, "refused": f"the host can add at most "
             f"{q0['can_add_gb']} GB"})
        return EXIT_REFUSED
    q = console.disk_quote(token, iid, inst["machine_id"], change, shrink)
    plan = {"instance": iid, "state": inst["state"], "change": f"{'-' if shrink else '+'}{change} GB",
            "total_gb_after": q["included_gb"] + q["extra_after_gb"], "extra_gb_after": q["extra_after_gb"],
            "extra_yuan_day_after": round(q["day_fen_after"] / 100, 3),
            "billing": "billed by the day; a day is charged at its largest size, so space removed today is still "
                       "paid for today",
            "takes_effect": ("only after a restart: until then the instance keeps its old size. A restart is "
                             "autopilot off then on, which stops running jobs" + (", and in GPU mode the GPU is "
                             "released and may be taken meanwhile" if inst["mode"] == "gpu" else "")
                             if running else "at the next power-on")}
    if not running:
        plan["reminder"] = BACKUP_NOTE
    if shrink:
        plan["warning"] = ("removing space cannot be undone for the files on it: keep the data disk's used space "
                           f"({q['used_gb']} GB) well inside {q['included_gb'] + q['extra_after_gb']} GB, and back up "
                           "what matters first")
    if not a.yes:
        out({"ok": True, "sent": False, "plan": plan,
             "next": "show this plan to the user, saying when it takes effect; once they agree, run it again with "
                     "--yes and --said '<their words>'"})
        return 0
    if not (a.said or "").strip():
        raise ValueError("--said: the user's own words agreeing to this change")
    try:
        console.disk_change(token, iid, inst["machine_id"], change, shrink)
    except console.ApiError as e:
        out({"ok": False, "sent": True, "done": False, "code": e.code, "msg": e.msg, "plan": plan})
        return EXIT_REFUSED
    except console.NoAnswer:
        pass                                           # read back below what the console now shows
    after = console.disk_quote(token, iid, inst["machine_id"], 0)
    done = after["extra_gb"] == q["extra_after_gb"]
    with State() as st:
        st.event(iid, "disk", change_gb=-change if shrink else change, extra_gb=after["extra_gb"], said=a.said.strip())
    res = {"ok": done, "instance": iid, "extra_gb": after["extra_gb"], "total_gb": after["included_gb"] + after["extra_gb"],
           "extra_yuan_day": round(after["day_fen_after"] / 100, 3), "takes_effect": plan["takes_effect"]}
    if not done:
        res["uncertain"] = True
        res["next"] = "the console does not show the new size yet: read autopilot disk ID before ordering again"
    out(res)
    return 0 if done else EXIT_UNCERTAIN


# ---- file storage ----

def storage_region(token: str, text: str) -> dict:
    """The file storage region for a region name (or part of one), a sign, or an instance ID (its region)."""
    found = storage.regions(token)
    if INSTANCE_RE.match(text or ""):
        text = console.instance(token, text)["region"]
    hits = pick([text], list(dict.fromkeys([r["name"] for r in found] + [r["sign"] for r in found])), "region")
    regions = [r for r in found if r["name"] in hits or r["sign"] in hits]
    if len(regions) != 1:
        raise ValueError(f"{text!r} matches {', '.join(r['name'] for r in regions)}: name one region")
    return regions[0]


def storage_ready(token: str, region: str) -> dict:
    """The file storage of the region an instance runs in, read before a power-on or a rent. An instance mounts
    /root/autodl-fs only when it starts, so storage initialized while it runs is seen only after a restart."""
    try:
        found = [r for r in storage.regions(token) if r["name"] == region]
    except (console.ApiError, console.NoAnswer) as e:
        return {"region": region, "status": "unknown", "note": f"the file storage list could not be read: {e}"}
    if not found:
        return {"region": region, "status": "none", "note": "the region has no file storage"}
    return {"region": region, "sign": found[0]["sign"], "status": found[0]["status"],
            "free_gb": found[0]["free_gb"]}


def storage_unready(token: str, region: str, skip: bool) -> dict | None:
    """The file storage state when it stands in the way of a power-on: not initialized, not skipped with
    --without-storage, and storage_check on."""
    if skip or not settings()["storage_check"]:
        return None
    fs = storage_ready(token, region)
    return fs if fs["status"] == "uninitialized" else None


def storage_refusal(fs: dict) -> dict:
    return {"refused": f"file storage in {fs['region']} is not initialized: an instance mounts /root/autodl-fs only "
                       "when it starts, so initializing it later means powering off and on again",
            "storage": fs,
            "next": f"ask the user: initialize it first (autopilot storage init {fs['sign']} shows what it means, "
                    "--yes initializes), then run this again; or, if they do not need file storage, run this again "
                    "with --without-storage (autopilot config set storage_check false stops asking)"}


def cmd_storage(a) -> int:
    token, _ = creds.find()
    if a.action == "list":
        out({"regions": storage.regions(token)})
        return 0
    if not a.region:
        raise ValueError(f"storage {a.action} needs a region (a name like 内蒙B区, a sign or an instance ID)")
    region = storage_region(token, a.region)
    if a.action == "init":
        if region["status"] == "created":
            out({"ok": True, "region": region["name"], "already": True})
            return 0
        if not a.yes:
            out({"ok": True, "sent": False, "region": region["name"], "free_gb": region["free_gb"],
                 "next": "initializing needs the user's agreement (the account must be bound to WeChat; beyond "
                         f"{region['free_gb']} GB the storage is billed): run again with --yes. Instances of the "
                         "region that are running mount it at /root/autodl-fs after their next power-on"})
            return 0
        storage.init(token, region["sign"])
        console.SLEEP(3)
        now_status = storage_region(token, region["sign"])["status"]
        out({"ok": now_status == "created", "region": region["name"], "status": now_status})
        return 0 if now_status == "created" else EXIT_ERR
    st = storage.Store(token, region)
    args = list(a.paths or [])
    if a.action == "ls":
        path = args[0] if args else "/"
        out({"region": region["name"], "path": storage.clean(path), "entries": st.ls(path), "usage": st.usage()})
    elif a.action == "put":
        if not args:
            raise ValueError("storage put REGION LOCAL [REMOTE_DIR]")
        out({"ok": True, "region": region["name"], **st.put(args[0], args[1] if len(args) > 1 else "/"),
             "usage": st.usage(), "on_instances": "/root/autodl-fs" + storage.clean(args[1] if len(args) > 1 else "/")})
    elif a.action == "get":
        if not args:
            raise ValueError("storage get REGION REMOTE [LOCAL_DIR]")
        out({"ok": True, "region": region["name"], **st.get(args[0], args[1] if len(args) > 1 else ".")})
    elif a.action == "rm":
        if not args:
            raise ValueError("storage rm REGION PATH [--recursive]")
        out({"ok": True, "region": region["name"], **st.rm(args[0], a.recursive), "usage": st.usage()})
    return 0


# ---- power on and off ----

def set_timer_checked(token: str, iid: str, at: int | None) -> bool:
    """Set (or cancel) the timed shutdown and read it back; True when the console shows what was asked."""
    want = None if at is None else -(-at // 60) * 60
    try:
        console.set_timer(token, iid, at)
    except console.NoAnswer:
        pass
    return console.instance(token, iid)["timer"] == want


def idle_for(mode: str | None, flag: int | None = None) -> int:
    """Idle minutes before the guard shuts down: --idle, else idle_min in GPU mode (15) and idle_min_nogpu in
    no-GPU mode (60: downloads take long there, and an idle no-GPU hour costs 0.1 yuan)."""
    if flag:
        return flag
    conf = settings()
    return int(conf["idle_min_nogpu"] if mode == "nogpu" else conf["idle_min"])


def start_guard(inst: dict, idle_min: int) -> dict:
    """Deploy and start the guard, trying SSH for a while: sshd comes up some seconds after the console says
    running."""
    r = remote_for(inst)
    deadline = console.MONO() + SSH_READY_S
    while True:
        try:
            return remote.deploy_and_start(r, {"idle": idle_min * 60, "mode": inst["mode"] or "gpu"})
        except remote.LoginRefused:
            raise
        except remote.Unreachable:
            if console.MONO() >= deadline:
                raise
            SLEEP(10)


def cmd_on(a) -> int:
    iid = instance_id(a.instance)
    token, _ = creds.find()
    conf = settings()
    idle_min = idle_for(a.mode, a.idle)
    fallback_s = 60 * (a.fallback or conf["fallback_min"])
    inst = console.instance(token, iid)
    if inst["state"] != "shutdown":
        out({"ok": False, "instance": iid, "sent": False, "refused": f"the instance is {inst['state']}, not shut "
             "down (a running one is taken over with autopilot arm)", "state": inst["state"]})
        return EXIT_REFUSED
    fs = storage_unready(token, inst["region"], a.without_storage)
    if fs:
        out({"ok": False, "instance": iid, "sent": False, **storage_refusal(fs)})
        return EXIT_REFUSED
    if a.mode == "gpu" and not inst["gpu_free"]:
        out({"ok": False, "instance": iid, "sent": False, "refused": f"no free GPU on its host ({inst['gpu_idle']} "
             f"idle, {inst['gpus']} needed): wait and try again, or use no-GPU mode if the task allows"})
        return EXIT_REFUSED
    verdict = judge(token, inst, a.mode, a.hours)
    if not verdict["ok"]:
        out({"ok": False, "instance": iid, "sent": False, **verdict})
        return EXIT_NO_GRANT if verdict["reason"].startswith("no grant") else EXIT_BUDGET
    balance = console.balance_fen(token)
    if balance < verdict["planned_fen"]:
        out({"ok": False, "instance": iid, "sent": False, "refused": f"the balance ({balance / 100:.2f} yuan) does "
             f"not cover the planned {verdict['planned_fen'] / 100:.2f} yuan"})
        return EXIT_REFUSED
    # the safety net first: until the guard runs, the console itself shuts the instance down
    fallback_at = console.server_time(token) + fallback_s
    if not set_timer_checked(token, iid, fallback_at):
        out({"ok": False, "instance": iid, "sent": False, "refused": "the fallback shutdown timer could not be set, "
             "so nothing was powered on"})
        return EXIT_ERR
    t0 = now()
    try:
        console.power_on(token, iid, a.mode)
    except console.ApiError as e:
        set_timer_checked(token, iid, None)
        out({"ok": False, "instance": iid, "sent": True, "done": False, "code": e.code, "msg": e.msg})
        return EXIT_REFUSED
    except console.NoAnswer:
        try:
            console.wait_state(token, iid, "running", 120)
        except TimeoutError:
            state = console.instance(token, iid)["state"]
            if state == "shutdown":
                set_timer_checked(token, iid, None)
            out({"ok": None, "instance": iid, "uncertain": True, "state": state,
                 "next": "the power-on got no answer; read autopilot status before trying again"})
            return EXIT_UNCERTAIN
    with State() as st:
        st.event(iid, "on", at=t0, mode=a.mode, price_fen_h=verdict["price_fen_h"], hours=a.hours)
    res = {"ok": True, "instance": iid, "mode": a.mode, "price_yuan_h": verdict["price_fen_h"] / 100,
           "fallback_timer": console.stamp(fallback_at)}
    return bring_up(token, iid, a, res, fallback_at, idle_min, t0, 300)


def bring_up(token: str, iid: str, a, res: dict, fallback_at: int, idle_min: int, t0: int, wait_s: int) -> int:
    """After a power-on or a rent: wait until running, start the guard (the key is added on the way if the
    instance refuses it), then cancel the fallback timer or keep the hard limit."""
    inst, seen = console.wait_state(token, iid, "running", wait_s)
    res["states"] = seen
    if inst["timer"] is None and not set_timer_checked(token, iid, max(fallback_at, console.server_time(token) + 600)):
        res["warning"] = "no fallback timer could be set: nothing but the guard shuts it down"
    use_guard, source = guard_setting(iid, a.guard)
    res["guard_setting"] = {"on": use_guard, "from": source}
    if use_guard:
        try:
            res["guard"] = start_guard(inst, idle_min)
        except (remote.Unreachable, remote.Uncertain, RuntimeError) as e:
            out({**res, "ok": False, "error": f"powered on, but the guard did not start: {e}",
                 "note": f"the fallback timer still shuts it down at {console.stamp(fallback_at)}"})
            return EXIT_ERR
    else:
        res["guard"] = None
        res["warning"] = ("the guard is off for this instance: nothing shuts it down when idle; it runs until "
                          "autopilot off" + (" or the hard limit" if a.hard_limit else ""))
    final = None if a.hard_limit is None else (inst["started_at"] or t0) + duration_s(a.hard_limit)
    res["timer"] = console.stamp(final) if final else None
    if not set_timer_checked(token, iid, final):
        res["warning"] = "the console timer could not be changed after the guard started; check autopilot status"
    out(res)
    return 0


def guard_setting(iid: str, flag) -> tuple:
    """(on, where that came from): the flag of this command, the instance's own setting, or guard_default."""
    if flag is not None:
        return flag, "this command"
    with State() as st:
        own = st.data["instances"].get(iid, {}).get("guard")
    if own is not None:
        return own, "instance"
    return bool(settings()["guard_default"]), "guard_default"


def cmd_guard(a) -> int:
    """Turn the guard on or off for one instance (kept on this computer), or back to guard_default; when the
    instance runs, the change takes effect on it at once."""
    iid = instance_id(a.instance)
    if a.action in ("on", "off", "auto"):
        with State() as st:
            entry = st.data["instances"].setdefault(iid, {})
            if a.action == "auto":
                entry.pop("guard", None)
            else:
                entry["guard"] = a.action == "on"
            st.event(iid, f"guard {a.action}")
    on, source = guard_setting(iid, None)
    res = {"ok": True, "instance": iid, "guard": on, "from": source}
    token, _ = creds.find()
    inst = console.instance(token, iid)
    if inst["state"] == "running":
        r = remote_for(inst)
        if a.action == "status":
            res["on_instance"] = remote.status(r)
        elif on:
            res["on_instance"] = start_guard(inst, idle_for(inst["mode"], a.idle))
        else:
            res["on_instance"] = remote.stop_guard(r)
    else:
        res["note"] = f"the instance is {inst['state']}: the setting applies from its next autopilot on"
    out(res)
    return 0


def protect_value(a) -> tuple:
    given = [(k, v) for k, v in (("pid", a.pid), ("job", a.job), ("cmd", a.cmd)) if v is not None]
    if len(given) != 1:
        raise ValueError("give exactly one of --pid, --job, --cmd")
    kind, value = given[0]
    value = str(value)
    if kind == "job" and not JOB_RE.match(value):
        raise ValueError(f"job name {value!r}")
    if kind == "cmd" and (len(value) < 3 or "\t" in value or "\n" in value):
        raise ValueError("--cmd: at least 3 characters of the command line, no tabs or line breaks")
    return kind, value


def cmd_protect(a) -> int:
    iid = instance_id(a.instance)
    if not JOB_RE.match(a.name):
        raise ValueError(f"entry name {a.name!r}: letters, digits and ._- (up to 64)")
    kind, value = protect_value(a)
    seconds = duration_s(a.for_)
    if not 0 < seconds <= 90 * 86400:
        raise ValueError("--for: up to 90 days")
    token, _ = creds.find()
    inst = running(token, iid)
    answer = remote.protect(remote_for(inst), a.name, kind, value, seconds)
    with State() as st:
        st.event(iid, "protect", name=a.name, kind=kind, seconds=seconds)
    out({"instance": iid, **answer})
    return 0


def cmd_unprotect(a) -> int:
    iid = instance_id(a.instance)
    token, _ = creds.find()
    inst = running(token, iid)
    out({"instance": iid, **remote.unprotect(remote_for(inst), a.name)})
    return 0


def cmd_protected(a) -> int:
    iid = instance_id(a.instance)
    token, _ = creds.find()
    inst = running(token, iid)
    out({"instance": iid, **remote.whitelist(remote_for(inst))})
    return 0


def cmd_connect(a) -> int:
    """Make sure the key logs in (adding it once if needed) and show how to open a shell by hand."""
    iid = instance_id(a.instance)
    token, _ = creds.find()
    inst = running(token, iid)
    r = remote_for(inst)
    r.run("true", 60)
    key = settings()["key"]
    res = {"ok": True, "instance": iid, "login": "key", "key_added": r.key_added}
    if key:
        res["ssh"] = f"ssh -i {key} -p {inst['ssh_port']} root@{inst['ssh_host']}"
    out(res)
    return 0


def cmd_arm(a) -> int:
    iid = instance_id(a.instance)
    token, _ = creds.find()
    inst = console.instance(token, iid)
    if inst["state"] != "running":
        out({"ok": False, "instance": iid, "refused": f"the instance is {inst['state']}, not running"})
        return EXIT_REFUSED
    status = start_guard(inst, idle_for(inst["mode"], a.idle))
    with State() as st:
        st.event(iid, "arm")
    out({"ok": True, "instance": iid, "guard": status})
    return 0


def cmd_off(a) -> int:
    iid = instance_id(a.instance)
    token, _ = creds.find()
    inst = console.instance(token, iid)
    if inst["state"] not in ("running", "starting"):
        out({"ok": True, "instance": iid, "state": inst["state"], "note": "already not running"})
        return 0
    if a.console:
        if not a.force:
            out({"ok": False, "instance": iid, "sent": False, "refused": "the console power-off cuts running jobs: "
                 "use it with --force only when SSH cannot reach the instance or the user agreed now"})
            return EXIT_REFUSED
        try:
            console.power_off(token, iid)
        except console.NoAnswer:
            pass
    else:
        rc, answer = remote.shutdown(remote_for(inst), a.force)
        if rc == EXIT_REFUSED:
            out({"ok": False, "instance": iid, "sent": False, **answer,
                 "next": "let the jobs finish (autopilot run --then-off), or --force if the user agrees to cut them"})
            return EXIT_REFUSED
    inst, seen = console.wait_state(token, iid, "shutdown", 180)
    if inst["timer"]:
        set_timer_checked(token, iid, None)
    with State() as st:
        st.event(iid, "off", via="console" if a.console else "ssh")
    out({"ok": True, "instance": iid, "states": seen, "bills": sync_bills(token, budget.month_start(now())),
         "release_at": console.stamp(inst["release_at"]) if inst["release_at"] else None,
         "reminder": BACKUP_NOTE})
    return 0


BACKUP_NOTE = ("an instance shut down for 15 days in a row is released and everything on it is deleted, data disk "
               "included; before leaving it off for long, back up what matters: autopilot pull to this computer, or "
               "copy it to /root/autodl-fs (file storage, kept apart from the instance)")


def cmd_timer(a) -> int:
    iid = instance_id(a.instance)
    token, _ = creds.find()
    at = None if a.cancel else console.server_time(token) + duration_s(a.in_)
    ok = set_timer_checked(token, iid, at)
    out({"ok": ok, "instance": iid, "timer": console.stamp(-(-at // 60) * 60) if at else None})
    return 0 if ok else EXIT_ERR


# ---- jobs and files ----

def running(token: str, iid: str) -> dict:
    inst = console.instance(token, iid)
    if inst["state"] != "running":
        raise ValueError(f"{iid} is {inst['state']}, not running: power it on first")
    return inst


def cmd_run(a) -> int:
    iid = instance_id(a.instance)
    if not JOB_RE.match(a.name):
        raise ValueError(f"job name {a.name!r}: letters, digits and ._- (up to 64)")
    command = " ".join(a.command).strip() if a.command else ""
    if a.command_file:
        command = pathlib.Path(a.command_file).read_text(encoding="utf-8")
    if not command:
        raise ValueError("give the command after --, or --command-file")
    conf = settings()
    workdir = a.workdir or conf["workdir"]
    if "'" in workdir:
        raise ValueError("workdir must not contain quotes")
    token, _ = creds.find()
    inst = running(token, iid)
    r = remote_for(inst)
    answer = remote.start_job(r, a.name, command, workdir, conf["env"], a.then_off)
    if answer.get("ok") and a.protect:
        answer["protect"] = remote.protect(r, a.name, "job", a.name, duration_s(a.protect))["whitelist"]
    out({"instance": iid, **answer})
    return 0 if answer.get("ok") else EXIT_REFUSED


def cmd_jobs(a) -> int:
    token, _ = creds.find()
    inst = running(token, instance_id(a.instance))
    out(remote.status(remote_for(inst)))
    return 0


def cmd_tail(a) -> int:
    token, _ = creds.find()
    inst = running(token, instance_id(a.instance))
    if not JOB_RE.match(a.name):
        raise ValueError(f"job name {a.name!r}")
    sys.stdout.write(remote.tail(remote_for(inst), a.name, a.lines))
    return 0


def cmd_hold(a) -> int:
    token, _ = creds.find()
    inst = running(token, instance_id(a.instance))
    out(remote.hold(remote_for(inst), 0 if a.duration == "0" else duration_s(a.duration)))
    return 0


def cmd_push(a) -> int:
    token, _ = creds.find()
    inst = running(token, instance_id(a.instance))
    remote_for(inst).copy(a.local, a.remote_dir, upload=True)
    out({"ok": True, "instance": inst["id"], "copied": a.local, "to": a.remote_dir})
    return 0


def cmd_pull(a) -> int:
    token, _ = creds.find()
    inst = running(token, instance_id(a.instance))
    remote_for(inst).copy(a.remote_path, a.local_dir, upload=False)
    out({"ok": True, "instance": inst["id"], "copied": a.remote_path, "to": a.local_dir})
    return 0


# ---- the parser ----

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autopilot", description="Let an AI power AutoDL instances on and off.")
    p.add_argument("--version", action="version", version=f"autopilot {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, func, help_text):
        s = sub.add_parser(name, help=help_text)
        s.set_defaults(func=func)
        return s

    s = add("token", cmd_token, "store (from stdin), show or delete the console token")
    s.add_argument("action", choices=["set", "status", "forget"])
    s.add_argument("--source", choices=["auto", "env", "file", "store"], default="auto")
    s.add_argument("--verify", action="store_true", help="status: ask the console whether it accepts the token")
    s = add("config", cmd_config, "show or change the settings (key, add_key, workdir, env, idle_min, idle_min_nogpu, fallback_min, guard_default, storage_check)")
    s.add_argument("action", choices=["show", "set"], nargs="?", default="show")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s = add("keygen", cmd_keygen, "make (or reuse) an SSH key for AutoDL and print its public half")
    s.add_argument("--path", default="~/.ssh/id_ed25519_autodl")
    add("doctor", cmd_doctor, "check Python, ssh, the token, the key and the directories")

    s = add("connect", cmd_connect, "check that the SSH key logs in to a running instance, adding it if refused")
    s.add_argument("instance")

    s = add("market", cmd_market, "search the GPU market: machines with free GPUs, by model, region, price, CUDA")
    s.add_argument("--gpu", action="append", help="GPU model, e.g. 'RTX 4090' or 4090 (repeatable)")
    s.add_argument("--region", action="append", help="region name or sign, e.g. 内蒙B区, 西北 or nm-B1 (repeatable)")
    s.add_argument("--like", metavar="ID", help="the GPU model and region of this instance, unless given")
    s.add_argument("--gpus", type=int, default=1, help="free GPUs needed on the machine (default 1)")
    s.add_argument("--max-price", type=float, help="yuan per GPU per hour")
    s.add_argument("--min-cuda", help="highest CUDA version the driver supports, at least, e.g. 12.4")
    s.add_argument("--min-memory", type=int, help="GPU memory in GB, at least")
    s.add_argument("--sort", choices=["price", "free"], default="price")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--all", action="store_true", help="also machines without free GPUs or offline")
    s.add_argument("--list", choices=["gpus", "regions"], help="list the GPU models or regions instead")
    s.add_argument("--thorough", action="store_true", help="read the whole market, not only the free block")
    s = add("images", cmd_images, "the base images (or --private: the account's own) an instance can be rented with")
    s.add_argument("--framework", help="e.g. PyTorch, Miniconda, TensorFlow")
    s.add_argument("--private", action="store_true")
    s = add("rent", cmd_rent, "rent a pay-as-you-go instance on a market machine: a plan first, --yes to rent")
    s.add_argument("machine", help="the machine ID from autopilot market")
    s.add_argument("--gpus", type=int, default=1)
    s.add_argument("--image", default="PyTorch", help="FRAMEWORK[:VERSION] (default PyTorch, the newest that fits), "
                   "a base-image-... or a private image-... ID")
    s.add_argument("--expand-disk", type=int, default=0, help="extra data disk in GB (billed daily)")
    s.add_argument("--hours", type=float, required=True, help="planned hours, for the budget check")
    s.add_argument("--budget", required=True, help="monthly budget in yuan for the new instance, or none")
    s.add_argument("--modes", choices=["gpu", "nogpu", "both"], default="both", help="what later power-ons may use")
    s.add_argument("--said", help="the user's own words agreeing to this rent (with --yes)")
    s.add_argument("--yes", action="store_true", help="rent; without it only the plan is shown")
    s.add_argument("--without-storage", action="store_true",
                   help="rent although the region's file storage is not initialized")
    s.add_argument("--idle", type=int, help="idle minutes before the guard shuts it down")
    s.add_argument("--fallback", type=int, help="minutes of the fallback timer")
    s.add_argument("--hard-limit", help="keep a console timer this long after the start, e.g. 8h")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--guard", dest="guard", action="store_const", const=True, default=None)
    g.add_argument("--no-guard", dest="guard", action="store_const", const=False)

    s = add("disk", cmd_disk, "the data disk of an instance: show it, add space or remove extra space (a plan first)")
    s.add_argument("instance")
    s.add_argument("--add", type=int, metavar="GB", help="add this much space (billed by the day)")
    s.add_argument("--remove", type=int, metavar="GB", help="remove this much extra space (shut down, pay as you go)")
    s.add_argument("--remove-all", action="store_true", help="remove all extra space")
    s.add_argument("--said", help="the user's own words agreeing to the change (with --yes)")
    s.add_argument("--yes", action="store_true", help="order it; without it only the plan is shown")

    s = add("storage", cmd_storage, "file storage (文件存储) of a region: list, init, ls, put, get, rm")
    s.add_argument("action", choices=["list", "init", "ls", "put", "get", "rm"])
    s.add_argument("region", nargs="?", help="a region name (or part), a sign, or an instance ID")
    s.add_argument("paths", nargs="*", help="ls [PATH]; put LOCAL [REMOTE_DIR]; get REMOTE [LOCAL_DIR]; rm PATH")
    s.add_argument("--recursive", action="store_true", help="rm: a folder with everything in it")
    s.add_argument("--yes", action="store_true", help="init: the user agreed")

    s = add("status", cmd_status, "every instance, or one; --guard adds the guard's state over SSH")
    s.add_argument("instance", nargs="?")
    s.add_argument("--guard", action="store_true")
    add("balance", cmd_balance, "the account's balance")
    s = add("sync", cmd_sync, "fetch the billing detail into the local cache")
    s.add_argument("--since", help="YYYY-MM-DD (default: the start of this month, Beijing time)")
    s = add("spend", cmd_spend, "this month's spending per instance, from the cache")
    s.add_argument("instance", nargs="?")

    s = add("grant", cmd_grant, "record what the user allows on an instance")
    s.add_argument("instance")
    s.add_argument("--modes", required=True, choices=list(budget.MODES))
    s.add_argument("--budget", required=True, help="yuan per month, or none")
    s.add_argument("--said", required=True, help="the user's own words")
    add("grants", cmd_grants, "list the grants")
    s = add("revoke", cmd_revoke, "remove an instance's grant")
    s.add_argument("instance")
    s = add("check", cmd_check, "judge the budget for running an instance HOURS more (sends nothing)")
    s.add_argument("instance")
    s.add_argument("--mode", required=True, choices=["gpu", "nogpu"])
    s.add_argument("--hours", required=True, type=float)

    s = add("on", cmd_on, "budget check, fallback timer, power on, start the guard")
    s.add_argument("instance")
    s.add_argument("--mode", required=True, choices=["gpu", "nogpu"])
    s.add_argument("--hours", required=True, type=float, help="planned hours, for the budget check")
    s.add_argument("--idle", type=int, help="minutes idle before the guard shuts down (default: idle_min in GPU mode, idle_min_nogpu without)")
    s.add_argument("--fallback", type=int, help="minutes of the fallback timer until the guard runs")
    s.add_argument("--hard-limit", help="keep a console timer at start + this (e.g. 8h): a stop that cuts jobs")
    s.add_argument("--without-storage", action="store_true",
                   help="power on although the region's file storage is not initialized")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--guard", dest="guard", action="store_true", default=None,
                   help="start the guard this time, whatever the setting")
    g.add_argument("--no-guard", dest="guard", action="store_false",
                   help="no guard this time: it runs until autopilot off (or --hard-limit)")
    s = add("guard", cmd_guard, "the guard of one instance: on, off, auto (follow guard_default) or status")
    s.add_argument("instance")
    s.add_argument("action", choices=["on", "off", "auto", "status"], nargs="?", default="status")
    s.add_argument("--idle", type=int)
    s = add("protect", cmd_protect, "whitelist a process for a while: kept on even when idle")
    s.add_argument("instance")
    s.add_argument("name", help="a name for the entry")
    s.add_argument("--pid", type=int)
    s.add_argument("--job", help="a job started by autopilot run")
    s.add_argument("--cmd", help="a piece of the process's command line, e.g. jupyter-lab")
    s.add_argument("--for", dest="for_", required=True, help="how long, e.g. 1d or 7d (up to 90d)")
    s = add("unprotect", cmd_unprotect, "remove a whitelist entry")
    s.add_argument("instance")
    s.add_argument("name")
    s = add("protected", cmd_protected, "the whitelist and what each entry protects now")
    s.add_argument("instance")
    s = add("arm", cmd_arm, "start the guard on an instance that is already running")
    s.add_argument("instance")
    s.add_argument("--idle", type=int)
    s = add("off", cmd_off, "shut down over SSH (refused while jobs run); --console --force when SSH is down")
    s.add_argument("instance")
    s.add_argument("--force", action="store_true")
    s.add_argument("--console", action="store_true")
    s = add("timer", cmd_timer, "set (--in DUR) or cancel the console's timed shutdown")
    s.add_argument("instance")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--in", dest="in_")
    g.add_argument("--cancel", action="store_true")

    s = add("run", cmd_run, "start a job: autopilot run ID NAME [--then-off] -- COMMAND")
    s.add_argument("instance")
    s.add_argument("name")
    s.add_argument("--then-off", action="store_true", help="shut down once it ends (after a short grace)")
    s.add_argument("--protect", metavar="DUR", help="also whitelist the job for DUR (e.g. 1d): kept on even when idle")
    s.add_argument("--workdir")
    s.add_argument("--command-file")
    s.set_defaults(command=None)        # everything after -- (split off in main, before argparse sees it)
    s = add("jobs", cmd_jobs, "the guard's state and the jobs")
    s.add_argument("instance")
    s = add("tail", cmd_tail, "the end of a job's log (NAME guard: the guard's log)")
    s.add_argument("instance")
    s.add_argument("name")
    s.add_argument("-n", "--lines", type=int, default=50)
    s = add("hold", cmd_hold, "count the instance as busy for DURATION (0 clears)")
    s.add_argument("instance")
    s.add_argument("duration")
    s = add("push", cmd_push, "copy a local file or directory into a directory on the instance")
    s.add_argument("instance")
    s.add_argument("local")
    s.add_argument("remote_dir")
    s = add("pull", cmd_pull, "copy a file or directory from the instance")
    s.add_argument("instance")
    s.add_argument("remote_path")
    s.add_argument("local_dir")
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = None
    if "--" in argv:        # `run ID NAME [options] -- COMMAND`: the command is never parsed as options
        cut = argv.index("--")
        argv, command = argv[:cut], argv[cut + 1:]
    a = parser().parse_args(argv)
    if a.cmd == "run":
        a.command = command
    elif command:
        out({"ok": False, "error": f"'--' and a command belong to autopilot run, not {a.cmd}"})
        return EXIT_ERR
    try:
        return a.func(a)
    except creds.TokenError as e:
        out({"ok": False, "error": str(e), "token": False})
        return EXIT_TOKEN
    except remote.LoginRefused as e:
        out({"ok": False, "error": str(e), "note": "nothing was done",
             "next": "autopilot connect ID adds the key (needs the key setting and add_key true); or paste the "
                     "public key into the console's 设置SSH免密登录 and power the instance on again"})
        return EXIT_UNREACHABLE
    except (console.NoAnswer, remote.Unreachable) as e:
        out({"ok": False, "error": str(e), "note": "nothing was done"})
        return EXIT_UNREACHABLE
    except remote.WrongHost as e:
        out({"ok": False, "error": str(e)})
        return EXIT_WRONG_HOST
    except remote.Uncertain as e:
        out({"ok": None, "uncertain": True, "error": str(e)})
        return EXIT_UNCERTAIN
    except (ValueError, LookupError, StateError, console.ApiError, storage.StorageError, TimeoutError, RuntimeError,
            OSError) as e:
        out({"ok": False, "error": str(e)})
        return EXIT_ERR
