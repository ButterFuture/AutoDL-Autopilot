"""Shared by the tests: every test gets its own config and state directories and no token from the environment,
and nothing reaches the network or an instance (the console and SSH are replaced where a test needs them)."""
import io
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from autopilot import cli, console, remote  # noqa: E402

ID = "abcd123456-1234abcd"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjE3OTc4ODY4MDAsImlhdCI6MTc5Mjc1ODk5MCwidWlkIjoxfQ.c2lnbmF0dXJl"
T0 = 1_792_760_440            # 2026-10-23 21:00:40 Beijing time


@pytest.fixture(autouse=True)
def _private_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTODL_AUTOPILOT_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("AUTODL_AUTOPILOT_STATE", str(tmp_path / "state"))
    monkeypatch.delenv("AUTODL_WEB_JWT", raising=False)


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def instance_record(**over):
    it = {"uuid": ID, "status": "shutdown", "start_mode": "non_gpu", "req_gpu_amount": 1, "gpu_idle_num": 2,
          "gpu_all_num": 8, "payg_price": 1140, "charge_type": "payg",
          "timed_shutdown_at": {"Time": "0001-01-01T00:00:00Z", "Valid": False},
          "started_at": {"Time": "2026-10-23T20:00:00+08:00", "Valid": True},
          "stopped_at": {"Time": "2026-10-23T20:30:00+08:00", "Valid": True},
          "region_name": "西北B区", "machine_alias": "123机", "snapshot_gpu_alias_name": "RTX 3080 Ti",
          "proxy_host": "connect.example.seetacloud.com", "ssh_port": 41234,
          "root_password": "secret-pw", "jupyter_token": "secret-jt", "ssh_command": "ssh -p 41234 root@x"}
    it.update(over)
    return it


def market_record(mid, gpu="RTX 4090", region=("内蒙B区", "nm-B1"), idle=1, total=8, li=2290, on_line=2,
                  cuda="13.2", slots=10, memory_gib=24):
    return {"machine_id": mid, "region_name": region[0], "region_sign": region[1], "machine_alias": f"{mid}机",
            "gpu_name": gpu, "gpu_memory": memory_gib << 30, "gpu_idle_num": idle, "gpu_number": total,
            "payg_price": li, "highest_cuda_version": cuda, "cpu_per_gpu": 16, "mem_per_gpu": 120 << 30,
            "max_instance_disk_size": 50 << 30, "max_data_disk_expand_size": 113 << 30, "on_line": on_line,
            "max_instance_num": 150, "binding_instance_num": 150 - slots,
            "rent_deadline": "2027-05-01T00:00:00+08:00", "chip_corp": "nvidia", "cpu_arch": "x86",
            "machine_base_info": {"ip_address": "10.0.0.1", "cpu_name": "Xeon"}}


IMAGES = [{"uuid": "base-image-old", "framework_name": "PyTorch", "framework_version": "1.11.0",
           "python_version": "3.8(ubuntu20.04)", "cuda_version": "11.3", "chip_corp": "nvidia", "cpu_arch": "x86"},
          {"uuid": "base-image-new", "framework_name": "PyTorch", "framework_version": "2.7.0",
           "python_version": "3.12(ubuntu22.04)", "cuda_version": "12.8", "chip_corp": "nvidia", "cpu_arch": "x86"},
          {"uuid": "base-image-mid", "framework_name": "PyTorch", "framework_version": "2.5.1",
           "python_version": "3.12(ubuntu22.04)", "cuda_version": "12.4", "chip_corp": "nvidia", "cpu_arch": "x86"},
          {"uuid": "base-image-arm", "framework_name": "PyTorch", "framework_version": "2.9.0",
           "python_version": "3.10", "cuda_version": "8.0.0", "chip_corp": "huawei", "cpu_arch": "arm"},
          {"uuid": "base-image-gone", "framework_name": "PyTorch", "framework_version": "3.0",
           "cuda_version": "11.0", "chip_corp": "nvidia", "cpu_arch": "x86", "is_off": True},
          {"uuid": "base-image-conda", "framework_name": "Miniconda", "framework_version": "conda3",
           "python_version": "3.10(ubuntu22.04)", "cuda_version": "11.8", "chip_corp": "nvidia", "cpu_arch": "x86"}]


REGIONS = [{"region_sign": "nm-B1", "region_name": "内蒙B区"}, {"region_sign": "nm-B2", "region_name": "内蒙B区"},
           {"region_sign": "west-B", "region_name": "西北B区"}, {"region_sign": "bj-B1", "region_name": "北京B区"}]
GPU_MODELS = [{"gpu_name": "RTX 3080 Ti", "gpu_rank": 100, "gpu_memory": 12 << 30, "gpu_series_type": "Ampere"},
              {"gpu_name": "RTX 4090", "gpu_rank": 300, "gpu_memory": 24 << 30, "gpu_series_type": "Ada"},
              {"gpu_name": "RTX 4090D", "gpu_rank": 290, "gpu_memory": 24 << 30, "gpu_series_type": "Ada"},
              {"gpu_name": "A800-80GB", "gpu_rank": 500, "gpu_memory": 80 << 30, "gpu_series_type": "Ampere"}]


def bill(serial, created, li, sub="charge_settle", iid=ID, to=None, charge_type="payg_no_gpu"):
    return {"uuid": serial, "created_at": created, "product_uuid": iid, "bill_type": "charge",
            "bill_sub_type": sub, "asset": li, "charge_type": charge_type, "balance_statement_type": "expenditure",
            "details": {"charge_to": to} if to else {}}


class FakeConsole:
    """One instance, a balance, bills and a clock. Power and timer requests change the state as the console
    does; `fail` makes a path give no answer ("noanswer") or a refusal code."""

    def __init__(self):
        self.inst = instance_record()
        self.bills = []
        self.server = T0
        self.balance_li = 41270
        self.calls = []
        self.fail = {}
        self.token_ok = True
        self.on_power_on = "running"
        self.machines = []
        self.rented = []
        self.extra_gb = 0                 # data disk space beyond the included 50 GB
        self.removable_gb = 0             # of that extra space, how much is free to take away
        self.rent_id = "newinst001-0a1b2c3d"
        self.storage = {"西北B区": ("west-B", "created"), "内蒙B区": ("nm-B1", "created")}    # name: (sign, status)

    def __call__(self, req, timeout=None):
        path = req.full_url.split("www.autodl.com", 1)[1].split("?", 1)[0]
        body = json.loads(req.data) if req.data else None
        self.calls.append((path, body))
        assert path in {p for _, p, _ in console.ENDPOINTS.values()}, path
        assert req.get_header("Authorization") == TOKEN
        f = self.fail.get(path)
        if f == "noanswer":
            self.fail.pop(path)
            if path == "/api/v1/instance/power_on":
                self.inst.update(status=self.on_power_on, start_mode=body["payload"])
            raise console.urllib.error.URLError("timed out")
        if not self.token_ok:
            return self.answer("AuthorizeFailed", None, "登录超时")
        if f:
            return self.answer(f[0], None, f[1])
        if path == "/api/v1/time_sync":
            return self.answer("Success", self.server)
        if path == "/api/v1/wallet/balance":
            return self.answer("Success", {"assets": self.balance_li, "voucher_balance": 0})
        if path == "/api/v1/instance":
            return self.answer("Success", {"list": [self.inst], "max_page": 1})
        if path == "/api/v1/bill/balance_statement/list":
            return self.answer("Success", {"list": self.bills, "max_page": 1})
        if path == "/api/v1/region/list":
            return self.answer("Success", REGIONS)
        if path == "/api/v1/file_storage/list":
            return self.answer("Success", [{"sign": sign, "name": name, "status": status, "fs_type": "autofs",
                                            "export_addr_1": "region.example", "port": 7007, "free_size": 20 << 30,
                                            "initialized_at": None} for name, (sign, status) in self.storage.items()])
        if path == "/api/v1/file_storage/init":
            name = next(n for n, (sg, _) in self.storage.items() if sg == body["sign"])
            self.storage[name] = (body["sign"], "created")
            return self.answer("Success", None)
        if path == "/api/v1/machine/gpu_type":
            return self.answer("Success", GPU_MODELS)
        if path == "/api/v1/user/machine/list":     # like the console: filters by model, region and machine,
            rows = [m for m in self.machines if (not body["gpu_type_name"] or m["gpu_name"] in body["gpu_type_name"])
                    and (not body["region_sign_list"] or m["region_sign"] in body["region_sign_list"])
                    and m["machine_id"] == body.get("machine_id", m["machine_id"])]
            if body["gpu_idle_num"]:                 # lists the free ones first (as it counted them), then pages
                rows.sort(key=lambda m: m.get("listed_idle", m["gpu_idle_num"]) < body["gpu_idle_num"])
            size, page = body["page_size"], body["page_index"]
            return self.answer("Success", {"list": rows[(page - 1) * size:page * size], "result_total": len(rows),
                                           "max_page": max(1, -(-len(rows) // size))})
        if path == "/api/v1/instance/power_on":
            self.inst.update(status=self.on_power_on, start_mode=body["payload"],
                             started_at={"Time": console.stamp(self.server).replace(" ", "T") + "+08:00",
                                         "Valid": True})
        elif path == "/api/v1/image/get":
            return self.answer("Success", IMAGES)
        elif path == "/api/v1/image/private/get":
            return self.answer("Success", [{"image_uuid": "image-abc123", "name": "my-env"}])
        elif path == "/api/v1/order/instance/create/payg":
            self.rented.append(body)
            self.inst = instance_record(uuid=self.rent_id, status="starting", start_mode="gpu",
                                        machine_id=body["instance_info"]["machine_id"],
                                        created_at=console.stamp(self.server).replace(" ", "T") + "+08:00",
                                        started_at={"Time": console.stamp(self.server).replace(" ", "T") + "+08:00",
                                                    "Valid": True})
            self.inst["status"] = "running"
            return self.answer("Success", self.rent_id)
        elif path == "/api/v1/instance/snapshot":
            return self.answer("Success", {"expand_data_disk_size": self.extra_gb << 30,
                                           "machine_info_snapshot": {"max_instance_disk_size": 50 << 30}})
        elif path == "/api/v1/order/data_disk/change/preview":
            return self.answer("Success", {"data_disk_used": 3 << 30, "max_expand_size": 900 << 30,
                                           "max_reduce_size": self.removable_gb << 30, "prepay_expand_data_disk": False,
                                           "data_disk_deal_daily_price": 7 * (body["data_disk_expand_size"] >> 30)})
        elif path == "/api/v1/order/data_disk/change":
            gb = body["change_size"] >> 30
            if body["opt_type"] == "data_disk_reduce":
                self.extra_gb -= gb
                self.removable_gb -= gb
            else:
                self.extra_gb += gb
                self.removable_gb += gb
        elif path == "/api/v1/instance/power_off":
            self.inst.update(status="shutdown")
        elif path == "/api/v1/instance/timed/shutdown":
            at = body["shutdown_at"]
            self.inst["timed_shutdown_at"] = ({"Time": at.replace(" ", "T") + ":00+08:00", "Valid": True} if at
                                              else {"Time": "0001-01-01T00:00:00Z", "Valid": False})
        return self.answer("Success", None)

    @staticmethod
    def answer(code, data, msg=""):
        return Resp(json.dumps({"code": code, "data": data, "msg": msg}).encode())

    def writes(self):
        changing = {p for _, p, w in console.ENDPOINTS.values() if w}
        return [c for c in self.calls if c[0] in changing]


@pytest.fixture
def fake(monkeypatch):
    c = FakeConsole()
    monkeypatch.setattr(console, "URLOPEN", c)
    clock = [0.0]                      # a monotonic clock that only the sleeps move on

    def sleep(s):
        clock[0] += s
    monkeypatch.setattr(console, "MONO", lambda: clock[0])
    monkeypatch.setattr(console, "SLEEP", sleep)
    monkeypatch.setattr(cli, "SLEEP", sleep)
    monkeypatch.setattr(cli, "NOW", lambda: c.server)
    monkeypatch.setenv("AUTODL_WEB_JWT", TOKEN)
    return c


def run_cli(capsys, *argv):
    rc = cli.main([str(x) for x in argv])
    text = capsys.readouterr().out.strip()
    return rc, (json.loads(text) if text.startswith("{") else text)
