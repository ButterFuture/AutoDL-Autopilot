#!/usr/bin/env python3
"""autodl_write_test — one pass over the power and timer endpoints on one instance.

Sequence: set a timed shutdown (server time + 60 min) -> cancel it -> power off
-> power on in NO-GPU mode -> power off. Each step polls the instance list until
the state settles and records every intermediate status seen. GPU mode is not
reachable from this script: the power-on payload is the constant "non_gpu".
If anything fails after the no-GPU power-on, it still tries to power off.

Usage:
  python3 -I autodl_write_test.py <instance_uuid> [--source auto|env|file|store]

Reuses the JWT lookup, request and masking code of autodl_probe.py (same dir).
Prints one masked JSON report at the end; costs one short no-GPU run (~￥0.01).
"""
import argparse
import datetime as dt
import importlib.util
import json
import os
import sys
import time

_spec = importlib.util.spec_from_file_location(
    "autodl_probe", os.path.join(os.path.dirname(os.path.abspath(__file__)), "autodl_probe.py"))
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

NO_GPU = "non_gpu"
POLL_EVERY, POLL_MAX = 3, 300  # seconds
BJT = dt.timezone(dt.timedelta(hours=8))


class Run:
    def __init__(self, uuid, tok):
        self.uuid, self.tok, self.t0 = uuid, tok, time.time()
        self.steps = []

    def req(self, method, path, payload=None):
        status, body = probe.call(method, path, payload, self.tok, None)
        return status, body

    def row(self):
        _, body = self.req("POST", "/api/v1/instance",
                           {"date_from": "", "date_to": "", "page_index": 1, "page_size": 50,
                            "status": [], "charge_type": []})
        for it in ((body or {}).get("data") or {}).get("list") or []:
            if it.get("uuid") == self.uuid:
                return {k: it.get(k) for k in ("status", "sub_status", "start_mode",
                                               "timed_shutdown_at", "gpu_idle_num", "status_at")}
        sys.exit(f"instance {self.uuid} not in list: {probe.mask(body)}")

    def act(self, name, path, payload):
        status, body = self.req("POST", path, payload)
        step = {"step": name, "t": round(time.time() - self.t0, 1), "request": {"path": path, "body": payload},
                "http": status, "response": probe.mask(body)}
        self.steps.append(step)
        ok = isinstance(body, dict) and body.get("code") == "Success"
        if not ok:
            raise RuntimeError(f"{name} failed: {json.dumps(step['response'], ensure_ascii=False)}")
        return step

    def wait(self, step, done):
        seen, start = [], time.time()
        while time.time() - start < POLL_MAX:
            r = self.row()
            key = (r["status"], r["sub_status"], r["start_mode"])
            if not seen or seen[-1]["state"] != list(key):
                seen.append({"after_s": round(time.time() - start, 1), "state": list(key)})
            if done(r):
                step["states"], step["final"] = seen, r
                return r
            time.sleep(POLL_EVERY)
        step["states"] = seen
        raise RuntimeError(f"{step['step']}: no settled state after {POLL_MAX}s, last {seen[-1]}")


def server_now(run):
    _, body = run.req("GET", "/api/v1/time_sync")
    return dt.datetime.fromtimestamp(body["data"], BJT)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("uuid")
    ap.add_argument("--source", choices=["auto", "env", "file", "store"], default="auto")
    args = ap.parse_args()
    tok, _ = probe.load_jwt(args.source)
    run = Run(args.uuid, tok)
    report = {"instance": args.uuid, "before": run.row(), "steps": run.steps}
    powered_on = False
    try:
        # 1. timer set + cancel
        at = (server_now(run) + dt.timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M")
        s = run.act("timer_set", "/api/v1/instance/timed/shutdown",
                    {"instance_uuid": args.uuid, "shutdown_at": at})
        s["final"] = run.row()
        s = run.act("timer_cancel", "/api/v1/instance/timed/shutdown",
                    {"instance_uuid": args.uuid, "shutdown_at": None})
        s["final"] = run.row()

        # 2. power off (only if running)
        if report["before"]["status"] == "running":
            s = run.act("power_off_1", "/api/v1/instance/power_off", {"instance_uuid": args.uuid})
            run.wait(s, lambda r: r["status"] == "shutdown")

        # 3. no-GPU power on
        s = run.act("power_on_nogpu", "/api/v1/instance/power_on",
                    {"instance_uuid": args.uuid, "payload": NO_GPU})
        powered_on = True
        run.wait(s, lambda r: r["status"] == "running")

        # 4. power off
        s = run.act("power_off_2", "/api/v1/instance/power_off", {"instance_uuid": args.uuid})
        run.wait(s, lambda r: r["status"] == "shutdown")
        powered_on = False
    except Exception as e:  # report what happened, then make sure nothing is left on
        report["error"] = str(e)
        if powered_on or run.row()["status"] != "shutdown":
            try:
                s = run.act("cleanup_power_off", "/api/v1/instance/power_off", {"instance_uuid": args.uuid})
                run.wait(s, lambda r: r["status"] == "shutdown")
            except Exception as e2:
                report["cleanup_error"] = str(e2)
    report["after"] = run.row()
    report["elapsed_s"] = round(time.time() - run.t0, 1)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
