"""The SSH layer: the host check, telling 'did not run' from 'unknown', what is sent, and adding the key with
the root password when the instance refuses it."""
import os
import subprocess

import pytest

from autopilot import remote
from conftest import ID


def fake_ssh(monkeypatch, rc, stdout="", stderr=""):
    """Every ssh call answers rc, stdout, stderr; a list of such triples answers the calls in turn."""
    sent = []
    answers = rc if isinstance(rc, list) else None

    def run(argv, input=None, capture_output=None, timeout=None, **kw):
        sent.append((argv, input, kw))
        code, o, e = answers.pop(0) if answers is not None else (rc, stdout, stderr)
        return subprocess.CompletedProcess(argv, code, o.encode(), e.encode())
    monkeypatch.setattr(remote, "RUN", run)
    monkeypatch.setattr(remote, "ssh_binary", lambda: "ssh")
    return sent


def r():
    return remote.Remote(ID, "connect.example.seetacloud.com", 41234, "~/.ssh/id_ed25519_autodl")


def test_script_starts_with_the_host_check(monkeypatch):
    sent = fake_ssh(monkeypatch, 0, "ok\n", remote.RAN + "\n")
    rc, out, err = r().run("echo ok")
    argv, data, kw = sent[0]
    script = data.decode()
    assert script.splitlines()[0] == f'[ "$(hostname)" = "autodl-container-{ID}" ] || ' \
                                     f'{{ echo {remote.WRONG} "$(hostname)" >&2; exit 113; }}'
    assert "\r" not in script and isinstance(data, bytes)
    assert argv[:3] == ["ssh", "-p", "41234"] and argv[-1] == "bash -s"
    assert "StrictHostKeyChecking=accept-new" in argv and "BatchMode=yes" in argv and "IdentitiesOnly=yes" in argv
    assert rc == 0 and out == "ok\n" and remote.RAN not in err


def test_wrong_host(monkeypatch):
    fake_ssh(monkeypatch, 113, "", f"{remote.WRONG} autodl-container-other\n")
    with pytest.raises(remote.WrongHost, match="autodl-container-other"):
        r().run("true")


def test_failed_before_start_is_unreachable(monkeypatch):
    fake_ssh(monkeypatch, 255, "", "Connection closed by 1.2.3.4 port 41234\n")
    with pytest.raises(remote.Unreachable):
        r().run("true")


def test_failed_after_start_is_uncertain(monkeypatch):
    fake_ssh(monkeypatch, 255, "", f"{remote.RAN}\nclient_loop: send disconnect: Broken pipe\n")
    with pytest.raises(remote.Uncertain):
        r().run("true")


def test_no_address_is_unreachable():
    with pytest.raises(remote.Unreachable, match="not running"):
        remote.Remote(ID, None, None, None)


def test_heredoc_delimiter_never_in_content():
    snippet = remote.heredoc("/x", "line\nAUTOPILOT_\n")
    mark = snippet.splitlines()[0].split("<<'", 1)[1].rstrip("'")
    assert snippet.count(mark) == 2 and snippet.endswith(mark + "\n")


def test_deploy_sends_the_guard_and_starts_it(monkeypatch):
    sent = fake_ssh(monkeypatch, 0, '{"daemon_running":true}\n', remote.RAN)
    status = remote.deploy_and_start(r(), {"idle": 900, "mode": "nogpu"})
    script = sent[0][1].decode()
    assert status == {"daemon_running": True}
    assert remote.GUARD.read_text() in script
    assert script.rstrip().endswith(f"bash {remote.AP}/guard.sh start idle=900 mode=nogpu")


def test_job_script_runs_in_the_workdir_with_the_env(monkeypatch):
    sent = fake_ssh(monkeypatch, 0, '{"ok":true,"job":"train","pid":42}\n', remote.RAN)
    answer = remote.start_job(r(), "train", "python train.py --epochs 3", "/root/autodl-tmp/proj",
                              "source /root/miniconda3/etc/profile.d/conda.sh", then_off=True)
    script = sent[0][1].decode()
    assert answer["pid"] == 42
    assert "cd '/root/autodl-tmp/proj' || exit 1\nsource /root/miniconda3/etc/profile.d/conda.sh\n" \
           "python train.py --epochs 3\n" in script
    assert script.rstrip().endswith("guard.sh job-start train then_off")


DENIED = "root@connect.example.seetacloud.com: Permission denied (publickey,password).\n"
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAID2arFm6VtjoMCjvMIsBUdj+DNOSiNsDyBW9LILAI/Ea autodl-autopilot"


def test_refused_key_is_login_refused_and_nothing_ran(monkeypatch):
    fake_ssh(monkeypatch, 255, "", DENIED)
    with pytest.raises(remote.LoginRefused) as e:
        r().run("true")
    assert isinstance(e.value, remote.Unreachable)


def test_refused_key_calls_login_once_then_runs(monkeypatch):
    sent = fake_ssh(monkeypatch, [(255, "", DENIED), (0, "ok\n", remote.RAN)])
    rem, fixes = r(), []
    rem.login = fixes.append
    assert rem.run("echo ok")[1] == "ok\n"
    assert fixes == [rem] and rem.key_added and rem.login is None and len(sent) == 2


def test_still_refused_after_adding_says_so(monkeypatch):
    fake_ssh(monkeypatch, [(255, "", DENIED), (255, "", DENIED)])
    rem = r()
    rem.login = lambda _: None
    with pytest.raises(remote.LoginRefused, match="still refused"):
        rem.run("true")


def test_add_key_logs_in_with_the_password_out_of_sight(monkeypatch):
    sent = fake_ssh(monkeypatch, 0, "", remote.RAN)
    remote.add_key(r(), "pw-123", PUB + "\n")
    argv, data, kw = sent[0]
    script = data.decode()
    assert "pw-123" not in " ".join(argv) and "pw-123" not in script
    assert kw["env"]["AUTOPILOT_SSH_PASSWORD"] == "pw-123" and kw["env"]["SSH_ASKPASS_REQUIRE"] == "force"
    assert "PubkeyAuthentication=no" in argv and "BatchMode=no" in argv and "-i" not in argv
    assert script.startswith(f'[ "$(hostname)" = "autodl-container-{ID}" ]')    # the host check still comes first
    assert f"grep -qxF '{PUB}' /root/.ssh/authorized_keys || printf '%s\\n' '{PUB}' >> " in script
    assert "chmod 600 /root/.ssh/authorized_keys" in script


def test_add_key_refuses_a_bad_key_or_no_password(monkeypatch):
    sent = fake_ssh(monkeypatch, 0, "", remote.RAN)
    for bad in ("not a key", PUB + "' ; rm -rf / #", "-----BEGIN OPENSSH PRIVATE KEY-----"):
        with pytest.raises(ValueError):
            remote.add_key(r(), "pw", bad)
    with pytest.raises(remote.LoginRefused, match="no root password"):
        remote.add_key(r(), "", PUB)
    assert sent == []


@pytest.mark.skipif(os.name == "nt", reason="the POSIX askpass script")
def test_askpass_answers_only_a_password_prompt():
    prog = remote.askpass_program()
    env = {**os.environ, "AUTOPILOT_SSH_PASSWORD": "pw 'x\" $y"}
    got = subprocess.run([prog, "root@host's password: "], env=env, capture_output=True)
    assert got.returncode == 0 and got.stdout == b"pw 'x\" $y\n"
    got = subprocess.run([prog, "Are you sure you want to continue connecting (yes/no)?"], env=env,
                         capture_output=True)
    assert got.returncode == 1 and got.stdout == b""
    assert "pw" not in open(prog).read()
