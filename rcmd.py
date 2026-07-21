#!/usr/bin/env python3
"""rcmd - run commands on remote telnet/ssh devices as if they were local.

Architecture: a background daemon holds one persistent shell per device
(via pexpect). The thin CLI client talks to it over a Unix socket. Command
boundaries and exit codes are captured with a random sentinel echoed after
each command, so `rcmd exec` returns the *real* remote exit status.

Usage:
    rcmd exec <device> "<command>"   run a command in the device's shell
    rcmd ls                          list devices and session state
    rcmd reset <device>              drop and reconnect the session
    rcmd raw <device> "<keys>"       send raw keystrokes (no exit code)
    rcmd logs <device> [n]           dump recent raw I/O for debugging
    rcmd daemon                      run the daemon in the foreground
    rcmd stop                        stop the daemon

The client auto-spawns the daemon on first use.
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid

HOME = os.path.expanduser("~")
RUN_DIR = os.path.join(HOME, ".cache", "rcmd")
SOCK_PATH = os.path.join(RUN_DIR, "rcmd.sock")
LOG_PATH = os.path.join(RUN_DIR, "daemon.log")
CONFIG_PATH = os.environ.get(
    "RCMD_CONFIG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices.yaml")
)
DEFAULT_TIMEOUT = int(os.environ.get("RCMD_TIMEOUT", "30"))

os.makedirs(RUN_DIR, exist_ok=True)


# --------------------------------------------------------------------------
# Minimal YAML loader (avoids a PyYAML dependency for this flat config).
# Supports: top-level "name:" blocks, two-space-indented "key: value" pairs.
# --------------------------------------------------------------------------
def load_config(path):
    devices = {}
    cur = None
    with open(path) as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line.startswith(" "):  # top-level device name
                name = line.split(":", 1)[0].strip()
                cur = {}
                devices[name] = cur
            else:
                key, _, val = line.strip().partition(":")
                val = val.strip()
                # strip surrounding quotes and inline comments
                if val and val[0] in "\"'":
                    q = val[0]
                    end = val.find(q, 1)
                    val = val[1:end] if end != -1 else val[1:]
                else:
                    val = val.split("#", 1)[0].strip()
                cur[key.strip()] = val
    return devices


# ==========================================================================
# DAEMON SIDE
# ==========================================================================
class Session:
    """One persistent remote shell for a single device."""

    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg
        self.child = None
        self.connected = False

    def connect(self):
        import pexpect

        cfg = self.cfg
        transport = cfg.get("transport", "ssh")
        if transport == "telnet":
            cmd = "telnet %s %s" % (cfg["host"], cfg.get("port", "23"))
        elif transport == "ssh":
            cmd = (
                "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                "-o LogLevel=ERROR -p %s %s@%s"
                % (cfg.get("port", "22"), cfg["username"], cfg["host"])
            )
        else:
            raise ValueError("unknown transport: %s" % transport)

        child = pexpect.spawn(cmd, encoding="utf-8", timeout=DEFAULT_TIMEOUT, echo=False)
        child.logfile_read = open(self._raw_log_path(), "a")

        login_prompt = cfg.get("login_prompt")
        password_prompt = cfg.get("password_prompt", "[Pp]assword:")
        shell_prompt = cfg.get("shell_prompt", "[#$]")

        # --- authentication phase -------------------------------------------
        # We can't assume password vs key auth, so wait for whichever comes
        # first: a login/password prompt, or the shell itself.
        if transport == "telnet" and login_prompt:
            child.expect(login_prompt, timeout=20)
            child.sendline(cfg.get("username", ""))

        if cfg.get("password"):
            # Race the password prompt against the shell prompt. If the shell
            # shows up first (ssh key auth), skip sending a password.
            idx = child.expect([password_prompt, shell_prompt], timeout=20)
            if idx == 0:
                child.sendline(cfg["password"])

        # --- handshake phase ------------------------------------------------
        # Don't trust the device's native prompt for command boundaries.
        # Install a fixed PS1 and confirm with a one-shot ready token so we
        # know exactly when the shell is ours, regardless of banners/colors.
        ready = "RCMD_READY_%s" % uuid.uuid4().hex[:8]
        child.sendline(
            "export PS1='' PROMPT_COMMAND='' PAGER=cat GIT_PAGER=cat; "
            "stty -echo 2>/dev/null; echo %s" % ready
        )
        child.expect(ready + r"\r?\n", timeout=20)
        # A second round-trip: echo is now off, so this token arrives with no
        # command echo ahead of it. Sync on it to leave the buffer clean for
        # the first real command.
        ready2 = "RCMD_SYNC_%s" % uuid.uuid4().hex[:8]
        child.sendline("echo %s" % ready2)
        child.expect(ready2 + r"\r?\n", timeout=20)
        # Drain anything still buffered (banners, echoed handshake lines on
        # shells that ignore stty -echo) so the first real command is clean.
        try:
            while True:
                child.read_nonblocking(65536, timeout=0.3)
        except Exception:
            pass
        self.child = child
        self.connected = True

    def _raw_log_path(self):
        return os.path.join(RUN_DIR, "session-%s.log" % self.name)

    def exec(self, command, timeout=DEFAULT_TIMEOUT):
        """Run one command, return (output, exit_code). Raises on timeout."""
        import pexpect

        if not self.connected:
            self.connect()
        marker = "___RCMD_%s___" % uuid.uuid4().hex[:12]
        # Send the command, then the sentinel on a SEPARATE line. Appending
        # after `;` on the same line breaks if the command ends in a `#`
        # comment or an unterminated quote — a newline forces a clean boundary.
        self.child.sendline(command)
        self.child.sendline("__rc=$?; echo %s:$__rc" % marker)
        try:
            self.child.expect(r"%s:(\d+)" % marker, timeout=timeout)
        except pexpect.TIMEOUT:
            # The command is still running and will keep polluting the shell.
            # Send Ctrl-C to interrupt it, then resync so the *next* exec sees
            # a clean prompt instead of leftover output.
            self._interrupt_and_resync()
            raise TimeoutError(
                "command timed out after %ss (interactive program? use `rcmd raw`)"
                % timeout
            )
        code = int(self.child.match.group(1))
        out = self.child.before
        # Strip the echoed command line (first line) that the shell reflects.
        out = self._clean(out, command)
        return out, code

    def _interrupt_and_resync(self):
        """Recover the shell after a timeout: interrupt, drain, confirm alive."""
        import pexpect

        try:
            self.child.sendcontrol("c")
            sync = "RCMD_RESYNC_%s" % uuid.uuid4().hex[:8]
            self.child.sendline("echo %s" % sync)
            self.child.expect(sync + r"\r?\n", timeout=5)
            try:
                while True:
                    self.child.read_nonblocking(65536, timeout=0.3)
            except Exception:
                pass
        except (pexpect.TIMEOUT, pexpect.EOF, OSError):
            # Shell is wedged; drop it so the next call reconnects fresh.
            self.close()

    def _clean(self, out, command):
        out = out.replace("\r", "")
        lines = out.split("\n")
        # With stty -echo the command isn't echoed, but be defensive: drop a
        # leading line that merely repeats the command we sent.
        if lines and command.strip() and lines[0].strip() == command.strip():
            lines = lines[1:]
        # Drop leading blank lines introduced by the sendline newline.
        while lines and lines[0] == "":
            lines.pop(0)
        return "\n".join(lines).rstrip("\n")

    def raw(self, keys):
        if not self.connected:
            self.connect()
        self.child.send(keys)
        time.sleep(0.5)
        try:
            return self.child.read_nonblocking(65536, timeout=1).replace("\r", "")
        except Exception:
            return ""

    def close(self):
        if self.child:
            try:
                self.child.close(force=True)
            except Exception:
                pass
        self.connected = False


class Daemon:
    def __init__(self):
        self.devices = load_config(CONFIG_PATH)
        self.sessions = {}

    def get_session(self, name):
        if name not in self.devices:
            raise KeyError("unknown device: %s" % name)
        if name not in self.sessions:
            self.sessions[name] = Session(name, self.devices[name])
        return self.sessions[name]

    def handle(self, req):
        action = req.get("action")
        try:
            if action == "exec":
                sess = self.get_session(req["device"])
                out, code = sess.exec(req["command"], req.get("timeout", DEFAULT_TIMEOUT))
                return {"ok": True, "output": out, "exit_code": code}
            if action == "raw":
                sess = self.get_session(req["device"])
                return {"ok": True, "output": sess.raw(req["command"])}
            if action == "ls":
                items = []
                for n, c in self.devices.items():
                    s = self.sessions.get(n)
                    items.append(
                        {
                            "device": n,
                            "transport": c.get("transport"),
                            "host": c.get("host"),
                            "connected": bool(s and s.connected),
                        }
                    )
                return {"ok": True, "devices": items}
            if action == "reset":
                if req["device"] in self.sessions:
                    self.sessions[req["device"]].close()
                    del self.sessions[req["device"]]
                return {"ok": True, "output": "session reset"}
            if action == "logs":
                sess = self.get_session(req["device"])
                path = sess._raw_log_path()
                data = ""
                if os.path.exists(path):
                    with open(path) as f:
                        data = f.read()
                n = int(req.get("lines", 200))
                return {"ok": True, "output": "\n".join(data.splitlines()[-n:])}
            if action == "ping":
                return {"ok": True, "output": "pong"}
            if action == "stop":
                return {"ok": True, "output": "stopping", "_stop": True}
            return {"ok": False, "error": "unknown action: %s" % action}
        except Exception as e:
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    def serve(self):
        if os.path.exists(SOCK_PATH):
            os.unlink(SOCK_PATH)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCK_PATH)
        srv.listen(16)
        self._log("daemon listening on %s" % SOCK_PATH)
        try:
            while True:
                conn, _ = srv.accept()
                try:
                    data = _recv_msg(conn)
                    req = json.loads(data)
                    resp = self.handle(req)
                    stop = resp.pop("_stop", False)
                    _send_msg(conn, json.dumps(resp))
                    if stop:
                        break
                except Exception as e:
                    try:
                        _send_msg(conn, json.dumps({"ok": False, "error": str(e)}))
                    except Exception:
                        pass
                finally:
                    conn.close()
        finally:
            for s in self.sessions.values():
                s.close()
            if os.path.exists(SOCK_PATH):
                os.unlink(SOCK_PATH)

    def _log(self, msg):
        with open(LOG_PATH, "a") as f:
            f.write(msg + "\n")


# --------------------------------------------------------------------------
# length-prefixed socket framing
# --------------------------------------------------------------------------
def _send_msg(conn, text):
    data = text.encode()
    conn.sendall(len(data).to_bytes(4, "big") + data)


def _recv_msg(conn):
    hdr = _recv_n(conn, 4)
    n = int.from_bytes(hdr, "big")
    return _recv_n(conn, n).decode()


def _recv_n(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


# ==========================================================================
# CLIENT SIDE
# ==========================================================================
def ensure_daemon():
    if _try_ping():
        return
    # spawn daemon detached
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "daemon"],
        stdout=open(LOG_PATH, "a"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(50):
        if _try_ping():
            return
        time.sleep(0.1)
    sys.stderr.write("rcmd: daemon failed to start (see %s)\n" % LOG_PATH)
    sys.exit(2)


def _try_ping():
    try:
        return request({"action": "ping"}, retries=0).get("ok")
    except Exception:
        return False


def request(req, retries=1):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(SOCK_PATH)
    # Give the daemon its command timeout plus headroom to reply, so a slow
    # remote command surfaces as a clean client-side error instead of hanging.
    if req.get("action") in ("exec", "raw"):
        sock.settimeout(req.get("timeout", DEFAULT_TIMEOUT) + 10)
    try:
        _send_msg(sock, json.dumps(req))
        return json.loads(_recv_msg(sock))
    except socket.timeout:
        return {"ok": False, "error": "no response from daemon (command still running? try `rcmd reset`)"}
    finally:
        sock.close()


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = argv[0]

    if cmd == "daemon":
        Daemon().serve()
        return 0

    if cmd == "stop":
        try:
            print(request({"action": "stop"}).get("output", ""))
        except Exception:
            print("daemon not running")
        return 0

    ensure_daemon()

    if cmd == "exec":
        if len(argv) < 3:
            sys.stderr.write("usage: rcmd exec <device> <command>\n")
            return 2
        device, command = argv[1], argv[2]
        resp = request(
            {
                "action": "exec",
                "device": device,
                "command": command,
                "timeout": DEFAULT_TIMEOUT,
            }
        )
        if not resp.get("ok"):
            sys.stderr.write("rcmd: %s\n" % resp.get("error"))
            return 3
        sys.stdout.write(resp["output"])
        if resp["output"] and not resp["output"].endswith("\n"):
            sys.stdout.write("\n")
        return resp["exit_code"]  # propagate remote exit code

    if cmd == "raw":
        device, keys = argv[1], argv[2]
        resp = request({"action": "raw", "device": device, "command": keys})
        sys.stdout.write(resp.get("output", ""))
        return 0 if resp.get("ok") else 3

    if cmd == "ls":
        resp = request({"action": "ls"})
        for d in resp.get("devices", []):
            state = "connected" if d["connected"] else "idle"
            print("%-10s %-7s %-16s %s" % (d["device"], d["transport"], d["host"], state))
        return 0

    if cmd == "reset":
        resp = request({"action": "reset", "device": argv[1]})
        print(resp.get("output") or resp.get("error"))
        return 0 if resp.get("ok") else 3

    if cmd == "logs":
        lines = argv[2] if len(argv) > 2 else "200"
        resp = request({"action": "logs", "device": argv[1], "lines": lines})
        print(resp.get("output") or resp.get("error"))
        return 0 if resp.get("ok") else 3

    sys.stderr.write("rcmd: unknown command %r (try `rcmd --help`)\n" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
