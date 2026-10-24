"""The instance-side guard (autopilot/guard.sh), run here with bash against stub readings: a fake clock, a fake
cgroup, /proc/net/dev and nvidia-smi, and a shutdown command that only records that it ran."""
import json
import os
import pathlib
import shutil
import subprocess
import time

import pytest

GUARD = pathlib.Path(__file__).resolve().parents[1] / "autopilot" / "guard.sh"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


class Box:
    def __init__(self, root: pathlib.Path):
        self.root = root
        self.ap = root / "ap"
        (root / "cgroup").mkdir(parents=True)
        (root / "proc" / "net").mkdir(parents=True)
        self.clock = root / "now"
        self.t = 1_000_000
        self.cpu = 0
        self.net = 0
        self.gpu = "0"
        self.write()
        nvsmi = root / "nvidia-smi"
        nvsmi.write_text(f'#!/bin/sh\n[ -f "{root}/gpu" ] || exit 9\ncat "{root}/gpu"\n')
        nvsmi.chmod(0o755)
        off = root / "shutdown"
        off.write_text(f'#!/bin/sh\necho called >> "{root}/shutdown.log"\n')
        off.chmod(0o755)
        self.env = {**os.environ, "AUTOPILOT_DIR": str(self.ap), "AUTOPILOT_NOW": str(self.clock),
                    "AUTOPILOT_CGROUP": str(root / "cgroup"), "AUTOPILOT_PROC": str(root / "proc"),
                    "AUTOPILOT_NVSMI": str(nvsmi), "AUTOPILOT_SHUTDOWN": str(off)}

    def write(self):
        self.clock.write_text(str(self.t))
        (self.root / "cgroup" / "cpu.stat").write_text(f"usage_usec {self.cpu}\nuser_usec 0\n")
        (self.root / "proc" / "net" / "dev").write_text(
            "Inter-|   Receive\n face |bytes\n"
            f"    lo: 999999 0 0 0 0 0 0 0 999999 0 0 0 0 0 0 0\n"
            f"  eth0: {self.net} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
        if self.gpu is None:
            (self.root / "gpu").unlink(missing_ok=True)
        else:
            (self.root / "gpu").write_text(self.gpu + "\n")

    def step(self, secs=60, cpu_cores=0.0, net_bps=0, gpu="0"):
        self.t += secs
        self.cpu += int(cpu_cores * secs * 1_000_000)
        self.net += net_bps * secs
        self.gpu = gpu
        self.write()
        return self.sh("tick")

    def sh(self, *args):
        return subprocess.run(["bash", str(GUARD), *args], env=self.env, capture_output=True, text=True, timeout=30)

    def status(self):
        return json.loads((self.ap / "status.json").read_text())

    def shut(self):
        return (self.root / "shutdown.log").exists()

    def configure(self, **opts):
        self.ap.mkdir(parents=True, exist_ok=True)
        (self.ap / "guard.conf").write_text("".join(f"{k}={v}\n" for k, v in opts.items()))
        (self.ap / "idle_since").write_text(str(self.t))


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def test_idle_long_enough_shuts_down(box):
    box.configure(idle=300)
    box.step()                                   # first reading: no previous counters, counts as busy
    assert box.status()["busy"] == ["cpu:unread", "net:unread"]
    for _ in range(4):
        box.step()
    assert not box.shut() and box.status()["idle_for"] == 240
    box.step()
    assert box.shut() and box.status()["verdict"] == "shutdown"
    box.step()
    assert (box.root / "shutdown.log").read_text().count("called") == 1     # once only


@pytest.mark.parametrize("load,reason", [({"cpu_cores": 0.5}, "cpu:500m"), ({"net_bps": 300_000}, "net:300000B/s"),
                                         ({"gpu": "37"}, "gpu:37%"), ({"gpu": None}, "gpu:unread")])
def test_each_signal_keeps_it_on(box, load, reason):
    box.configure(idle=120)
    box.step()
    for _ in range(5):
        box.step(**load)
    assert not box.shut() and reason in box.status()["busy"] and box.status()["idle_for"] == 0


def test_nogpu_mode_ignores_the_missing_gpu(box):
    box.configure(idle=120, mode="nogpu")
    box.step(gpu=None)
    for _ in range(2):
        box.step(gpu=None)
    assert box.shut()


def test_hold_keeps_it_on_until_it_runs_out(box):
    box.configure(idle=120)
    box.step()
    assert json.loads(box.sh("hold", "600").stdout)["hold_until"] == box.t + 600
    for _ in range(9):
        box.step()
    assert not box.shut()
    for _ in range(3):
        box.step()
    assert box.shut()


def test_a_quiet_job_does_not_keep_it_on_by_itself(box):
    """Default logic: a job that runs but uses nothing is idle like anything else."""
    box.configure(idle=120)
    jobs = box.ap / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "wait.sh").write_text("sleep 30\n")
    pid = json.loads(box.sh("job-start", "wait").stdout)["pid"]
    try:
        box.step()
        for _ in range(2):
            box.step()
        assert box.shut() and box.status()["jobs"][0]["running"] is True
    finally:
        os.kill(pid, 9)


def test_then_off_ends_it_after_the_grace(box):
    box.configure(idle=10_000, grace=120)
    jobs = box.ap / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "train.sh").write_text("sleep 30\n")
    started = json.loads(box.sh("job-start", "train", "then_off").stdout)
    assert started["ok"] and started["job"] == "train"
    box.step(cpu_cores=1)
    os.kill(started["pid"], 9)
    (jobs / "train.rc").write_text(f"0 {box.t}\n")     # the end, as the wrapper writes it, on the fake clock
    box.step(secs=60)
    assert not box.shut()
    box.step(secs=60)
    assert box.shut() and box.status()["verdict"] == "shutdown"


def test_job_names_are_checked(box):
    box.configure()
    for bad in ("../x", ".hidden", "a b", ""):
        assert box.sh("job-start", bad).returncode != 0


def test_off_refuses_while_a_job_runs(box):
    box.configure()
    jobs = box.ap / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "long.sh").write_text("sleep 30\n")
    pid = json.loads(box.sh("job-start", "long").stdout)["pid"]
    try:
        r = box.sh("off")
        assert r.returncode == 3 and json.loads(r.stdout)["running"] == "long"
        r = box.sh("off", "--force")
        assert r.returncode == 0 and json.loads(r.stdout)["ok"]
        for _ in range(50):
            if box.shut():
                break
            time.sleep(0.1)
        assert box.shut()
    finally:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def test_status_without_a_daemon(box):
    box.configure()
    got = json.loads(box.sh("status").stdout)
    assert got["daemon_running"] is False and got["last"] is None and got["jobs"] == []


def test_bad_settings_are_refused(box):
    box.ap.mkdir(parents=True)
    r = box.sh("start", "idle=ten")
    assert r.returncode != 0 and not (box.ap / "guard.pid").exists()


def test_daemon_starts_ticks_and_stops(box):
    """start writes the settings and runs the loop in the background; the loop ticks; stop ends it."""
    box.ap.mkdir(parents=True)
    shutil.copy(GUARD, box.ap / "guard.sh")
    started = box.sh("start", "idle=3600", "interval=1", "mode=nogpu")
    assert started.returncode == 0, started.stderr
    assert json.loads(started.stdout.splitlines()[-1])["daemon_running"] is True
    for _ in range(50):
        if (box.ap / "status.json").exists():
            break
        time.sleep(0.1)
    assert box.status()["mode"] == "nogpu" and box.status()["idle"] == 3600
    pid = int((box.ap / "guard.pid").read_text())
    box.sh("stop")
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the guard loop is still running after stop")
    assert not box.shut()


# ---- the whitelist ----

def fake_process(box, pid, cmdline):
    d = box.root / "proc" / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode() + b"\0")
    return d


def protect(box, *args):
    r = box.sh("protect", *args)
    return r.returncode, (json.loads(r.stdout) if r.stdout.startswith("{") else r.stderr)


def entry(box, name):
    return next(e for e in box.status()["whitelist"] if e["name"] == name)


def test_an_unlisted_process_does_not_keep_it_on(box):
    box.configure(idle=120)
    fake_process(box, 4242, "python -m jupyterlab --port 8888")
    for _ in range(4):
        box.step()
    assert box.shut()


def test_a_listed_process_keeps_it_on_until_the_entry_expires(box):
    box.configure(idle=120)
    fake_process(box, 4242, "python -m jupyterlab --port 8888")
    rc, got = protect(box, "jupyter", "cmd", "jupyterlab", str(86400))      # one day
    assert rc == 0 and got["whitelist"][0]["state"] == "protecting" and got["whitelist"][0]["pids"] == "4242"
    box.step()
    for _ in range(20):
        box.step(secs=3600)                       # 20 idle hours
    assert not box.shut() and "protect:jupyter" in box.status()["busy"]
    for _ in range(5):
        box.step(secs=3600)                       # the day runs out
    assert entry(box, "jupyter")["state"] == "expired" and box.shut()


def test_an_entry_without_its_process_protects_nothing(box):
    box.configure(idle=120)
    d = fake_process(box, 777, "python serve.py")
    assert protect(box, "server", "pid", "777", str(7 * 86400))[0] == 0
    box.step()
    box.step()
    assert entry(box, "server")["state"] == "protecting"
    for f in d.iterdir():
        f.unlink()
    d.rmdir()                                     # the process is gone
    box.step()
    box.step()
    assert entry(box, "server")["state"] == "no_process" and box.shut()


def test_a_listed_job(box):
    box.configure(idle=120)
    jobs = box.ap / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "wait.sh").write_text("sleep 30\n")
    pid = json.loads(box.sh("job-start", "wait").stdout)["pid"]
    try:
        assert protect(box, "wait", "job", "wait", "3600")[0] == 0
        for _ in range(5):
            box.step()
        assert not box.shut() and entry(box, "wait")["pids"] == str(pid)
    finally:
        os.kill(pid, 9)
    box.step()
    box.step()
    box.step()
    assert box.shut()


def test_the_guard_itself_never_matches(box):
    box.configure(idle=120)
    fake_process(box, 999, "bash /root/autodl-tmp/.autopilot/guard.sh loop jupyterlab")
    protect(box, "jupyter", "cmd", "jupyterlab", "3600")
    assert entry_now(box, "jupyter") == "no_process"


def entry_now(box, name):
    return next(e["state"] for e in json.loads(box.sh("protected").stdout)["whitelist"] if e["name"] == name)


def test_unprotect_and_replace(box):
    box.configure(idle=120)
    fake_process(box, 4242, "python -m jupyterlab")
    protect(box, "jupyter", "cmd", "jupyterlab", "60")
    rc, got = protect(box, "jupyter", "cmd", "jupyterlab", "7200")       # the same name replaces the entry
    assert len(got["whitelist"]) == 1 and got["whitelist"][0]["expires_in"] == 7200
    got = json.loads(box.sh("unprotect", "jupyter").stdout)
    assert got["removed"] is True and got["whitelist"] == []
    assert json.loads(box.sh("unprotect", "jupyter").stdout)["removed"] is False


@pytest.mark.parametrize("args", [("x", "port", "1", "60"), ("x", "pid", "abc", "60"), ("x", "cmd", "py", "60"),
                                  ("x", "job", "../a", "60"), ("x", "cmd", "jupyter", "0"),
                                  ("x", "cmd", "jupyter", str(91 * 86400)), ("../x", "cmd", "jupyter", "60")])
def test_bad_entries_are_refused(box, args):
    box.configure()
    rc, _ = protect(box, *args)
    assert rc != 0 and not (box.ap / "whitelist").exists()


def test_quotes_in_a_command_stay_valid_json(box):
    box.configure()
    rc, got = protect(box, "q", "cmd", 'run "a\\b"', "60")
    assert rc == 0 and got["whitelist"][0]["value"] == 'run "a\\b"'


def test_the_whitelist_outlives_a_restart(box):
    box.ap.mkdir(parents=True)
    shutil.copy(GUARD, box.ap / "guard.sh")
    protect(box, "jupyter", "cmd", "jupyterlab", "3600")
    box.sh("start", "idle=60", "interval=3600")
    box.sh("stop")
    assert json.loads(box.sh("protected").stdout)["whitelist"][0]["name"] == "jupyter"
