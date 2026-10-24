"""SSH to an instance, with the host and port the console gives for it.

Every remote script starts by comparing the container's host name with autodl-container-<instance ID>: on any
other machine it stops before doing anything (exit 113, WrongHost). Scripts go to `bash -s` on stdin as bytes, so
no quoting crosses the shell of this computer and line ends stay LF on Windows. The host keys are kept in
Autopilot's own known_hosts: accepted the first time, refused if they change.

A marker on stderr tells whether the script started: an ssh failure (exit 255) before the marker means nothing
ran (Unreachable); after it, the outcome is unknown and the caller reads the state before repeating anything.

When the instance refuses the key (LoginRefused), a Remote with a `login` callback calls it once and tries again:
the callback adds the public key to the instance's authorized_keys over a single password login (add_key). The
password reaches ssh through SSH_ASKPASS and an environment variable of the ssh process only; it is never on a
command line, in a file or in the output.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys

from . import paths

AP = "/root/autodl-tmp/.autopilot"     # on the instance's data disk
GUARD = pathlib.Path(__file__).with_name("guard.sh")
RAN = "__AUTOPILOT_RAN__"
WRONG = "__AUTOPILOT_WRONG_HOST__"
PUBLIC_KEY_RE = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)) [A-Za-z0-9+/]+={0,3}( [^'\n\r]*)?\Z")
RUN = subprocess.run   # replaced in tests


class Unreachable(Exception):
    """ssh failed before the script started: nothing was done."""


class WrongHost(Exception):
    """The host and port lead to another machine than the instance: nothing was done."""


class LoginRefused(Unreachable):
    """sshd answered but did not accept the login: nothing ran."""


class Uncertain(Exception):
    """The script started and ssh failed before it ended: it may or may not have done its work."""


class Remote:
    def __init__(self, instance: str, host: str, port: int, key: str | None, timeout_s: int = 15):
        if not host or not port:
            raise Unreachable(f"{instance} has no SSH address in the console (it is not running)")
        self.instance, self.host, self.port, self.key = instance, host, int(port), key
        self.timeout_s = timeout_s
        self.login = None          # called once with this Remote when the key is refused, to add it
        self.key_added = False

    def options(self, password: bool = False) -> list:
        kh = paths.private_dir(paths.state_dir()) / "known_hosts"
        opts = ["-o", f"ConnectTimeout={self.timeout_s}", "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=4", "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={kh}", "-o", "LogLevel=ERROR"]
        if password:
            return opts + ["-o", "BatchMode=no", "-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1",
                           "-o", "PreferredAuthentications=password,keyboard-interactive"]
        opts += ["-o", "BatchMode=yes"]
        if self.key:
            opts += ["-i", str(pathlib.Path(self.key).expanduser()), "-o", "IdentitiesOnly=yes"]
        return opts

    def run(self, script: str, timeout_s: int = 120) -> tuple:
        """(exit code, stdout, stderr) of script on the instance."""
        try:
            return self._run(script, timeout_s)
        except LoginRefused:
            if self.login is None:
                raise
            fix, self.login = self.login, None
            fix(self)
            self.key_added = True
            try:
                return self._run(script, timeout_s)
            except LoginRefused as e:
                raise LoginRefused(f"the key was added to {self.instance}, yet it is still refused: {e}") from None

    def _run(self, script: str, timeout_s: int, password: str | None = None) -> tuple:
        head = (f'[ "$(hostname)" = "autodl-container-{self.instance}" ] || '
                f'{{ echo {WRONG} "$(hostname)" >&2; exit 113; }}\necho {RAN} >&2\n')
        argv = [ssh_binary(), "-p", str(self.port), *self.options(password is not None), f"root@{self.host}",
                "bash -s"]
        extra = {}
        if password is not None:
            extra = {"env": {**os.environ, "SSH_ASKPASS": askpass_program(), "SSH_ASKPASS_REQUIRE": "force",
                             "DISPLAY": os.environ.get("DISPLAY") or ":0", "AUTOPILOT_SSH_PASSWORD": password}}
            if os.name != "nt":
                extra["start_new_session"] = True    # no terminal: older ssh then asks SSH_ASKPASS too
        try:
            r = RUN(argv, input=(head + script).encode(), capture_output=True, timeout=timeout_s, **extra)
        except subprocess.TimeoutExpired:
            raise Uncertain(f"ssh to {self.instance} took longer than {timeout_s}s") from None
        out, err = r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
        ran = RAN in err
        err = "\n".join(line for line in err.splitlines() if line.strip() != RAN)
        if r.returncode == 113 and WRONG in err:
            raise WrongHost(f"{self.host}:{self.port} leads to {err.split(WRONG, 1)[1].strip()}, not to "
                            f"{self.instance}: nothing was done there")
        if r.returncode == 255 and not ran and "Permission denied" in err:
            raise LoginRefused(f"{self.host}:{self.port} refused the login "
                               f"({'password' if password is not None else self.key or 'default keys'})")
        if r.returncode == 255 and not ran:
            raise Unreachable(f"ssh to {self.host}:{self.port} failed: {err.strip()[-300:]}")
        if r.returncode == 255:
            raise Uncertain(f"ssh to {self.instance} broke off while the command ran: {err.strip()[-300:]}")
        return r.returncode, out, err

    def json(self, script: str, timeout_s: int = 120):
        rc, out, err = self.run(script, timeout_s)
        lines = [line for line in out.splitlines() if line.startswith("{")]
        if not lines:
            raise RuntimeError(f"no answer from the instance (exit {rc}): {err.strip()[-300:]}")
        return rc, json.loads(lines[-1])

    def copy(self, src: str, dst: str, upload: bool, timeout_s: int = 3600) -> None:
        """scp -r between this computer and the instance, after checking that the address leads to it."""
        self.run("true", 60)
        remote = f"root@{self.host}:"
        pair = [src, remote + dst] if upload else [remote + src, dst]
        argv = [scp_binary(), "-r", "-P", str(self.port), *self.options(), *pair]
        r = RUN(argv, capture_output=True, timeout=timeout_s)
        if r.returncode != 0:
            raise RuntimeError(f"scp failed (exit {r.returncode}): {r.stderr.decode('utf-8', 'replace')[-300:]}")


def askpass_program() -> str:
    """A small program ssh runs to read the password: it answers a password prompt from AUTOPILOT_SSH_PASSWORD
    and nothing else. The file holds no secret."""
    folder = paths.private_dir(paths.state_dir())
    if os.name == "nt":
        path = folder / "askpass.cmd"
        code = ("import os,sys; a=' '.join(sys.argv[1:]).lower(); "
                "sys.exit(1) if 'password' not in a else print(os.environ['AUTOPILOT_SSH_PASSWORD'])")
        text = f'@"{sys.executable}" -c "{code}" %*\r\n'
    else:
        path = folder / "askpass.sh"
        text = ('#!/bin/sh\ncase "$*" in\n  *[Pp]assword*) printf \'%s\\n\' "$AUTOPILOT_SSH_PASSWORD" ;;\n'
                '  *) exit 1 ;;\nesac\n')
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        paths.write_private(path, text)
    if os.name != "nt":
        path.chmod(0o700)
    return str(path)


def add_key(r: Remote, password: str, public_key: str) -> None:
    """Log in once with the instance's root password and add public_key to /root/.ssh/authorized_keys (kept
    when the instance is powered off and on: it is on the system disk)."""
    public_key = public_key.strip()
    if not PUBLIC_KEY_RE.match(public_key):
        raise ValueError("the public key file does not hold one OpenSSH public key line")
    if not password:
        raise LoginRefused(f"the console shows no root password for {r.instance}, so the key cannot be added")
    q, f = shlex.quote(public_key), "/root/.ssh/authorized_keys"
    script = (f"umask 077\nmkdir -p /root/.ssh\ntouch {f}\n"
              f'[ -s {f} ] && [ -n "$(tail -c 1 {f})" ] && echo >> {f}\n'
              f"grep -qxF {q} {f} || printf '%s\\n' {q} >> {f}\n"
              f"chmod 700 /root/.ssh && chmod 600 {f}\n")
    rc, out, err = r._run(script, 60, password=password)
    if rc != 0:
        raise RuntimeError(f"adding the key on {r.instance} failed (exit {rc}): {err.strip()[-300:]}")


def ssh_binary() -> str:
    found = shutil.which("ssh")
    if not found:
        raise Unreachable("no ssh client on this computer (Windows: Settings > Optional features > OpenSSH Client)")
    return found


def scp_binary() -> str:
    found = shutil.which("scp")
    if not found:
        raise Unreachable("no scp on this computer (it comes with the OpenSSH client)")
    return found


def heredoc(path: str, content: str) -> str:
    """A shell snippet writing content to path, with a delimiter that does not occur in it."""
    while True:
        mark = f"AUTOPILOT_{secrets.token_hex(8)}"
        if mark not in content:
            break
    if not content.endswith("\n"):
        content += "\n"
    return f"cat > '{path}' <<'{mark}'\n{content}{mark}\n"


def guard(args: str) -> str:
    return f"bash {AP}/guard.sh {args}\n"


def deploy_and_start(r: Remote, settings: dict) -> dict:
    """Write the guard to the instance, start it with settings, and return its status."""
    script = (f"mkdir -p {AP}/jobs\n" + heredoc(f"{AP}/guard.sh", GUARD.read_text(encoding="utf-8"))
              + guard("start " + " ".join(f"{k}={v}" for k, v in settings.items())))
    rc, status = r.json(script)
    if rc != 0 or not status.get("daemon_running"):
        raise RuntimeError(f"the guard did not start on {r.instance}: {status}")
    return status


def status(r: Remote) -> dict:
    rc, out, err = r.run(f'if [ -f {AP}/guard.sh ]; then bash {AP}/guard.sh status; else echo "{{}}"; fi\n')
    lines = [line for line in out.splitlines() if line.startswith("{")]
    return json.loads(lines[-1]) if lines else {}


def start_job(r: Remote, name: str, command: str, workdir: str, env: str, then_off: bool) -> dict:
    body = f"cd '{workdir}' || exit 1\n" + (f"{env}\n" if env else "") + command + "\n"
    script = (f"mkdir -p {AP}/jobs\n" + heredoc(f"{AP}/jobs/{name}.sh", body)
              + guard(f"job-start {name}" + (" then_off" if then_off else "")))
    rc, answer = r.json(script)
    return answer


def hold(r: Remote, seconds: int) -> dict:
    return r.json(guard(f"hold {int(seconds)}"))[1]


def tail(r: Remote, name: str, lines: int) -> str:
    rc, out, err = r.run(guard(f"tail {name} {int(lines)}"))
    if rc != 0:
        raise RuntimeError(err.strip() or f"tail exited {rc}")
    return out


def shutdown(r: Remote, force: bool) -> tuple:
    """(exit code, answer): 0 when the shutdown was started, 3 when refused because jobs run."""
    script = (f'if [ -f {AP}/guard.sh ]; then bash {AP}/guard.sh off{" --force" if force else ""}; '
              f'else ( sleep 2; sync; /usr/bin/shutdown ) >/dev/null 2>&1 </dev/null & echo \'{{"ok":true}}\'; fi\n')
    return r.json(script)


def protect(r: Remote, name: str, kind: str, value: str, seconds: int) -> dict:
    """Whitelist a process on the instance for seconds: kind pid, job or cmd (a piece of its command line)."""
    script = f"set -- {shlex.quote(name)} {kind} {shlex.quote(value)} {int(seconds)}\n" + guard('protect "$@"')
    rc, answer = r.json(script)
    if rc != 0:
        raise ValueError(f"the guard refused the entry (exit {rc})")
    return answer


def unprotect(r: Remote, name: str) -> dict:
    return r.json(f"set -- {shlex.quote(name)}\n" + guard('unprotect "$1"'))[1]


def whitelist(r: Remote) -> dict:
    return r.json(f'if [ -f {AP}/guard.sh ]; then {guard("protected").strip()}; else echo \'{{"whitelist":[]}}\'; fi\n')[1]


def stop_guard(r: Remote) -> dict:
    """Stop the guard's loop (the whitelist and the jobs stay); returns its status, or {} without a guard."""
    rc, out, err = r.run(f'if [ -f {AP}/guard.sh ]; then bash {AP}/guard.sh stop; bash {AP}/guard.sh status; '
                         f'else echo "{{}}"; fi\n')
    lines = [line for line in out.splitlines() if line.startswith("{")]
    return json.loads(lines[-1]) if lines else {}

