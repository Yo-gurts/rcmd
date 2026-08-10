#!/usr/bin/env python3
"""rcmd - run commands on remote telnet/ssh/serial devices as if they were local.

Architecture: a background daemon holds one persistent shell per device
(via pexpect for ssh/telnet, pyserial for serial). The thin CLI client talks
to it over a socket (Unix socket on Linux/Mac, TCP localhost on Windows).
Command boundaries and exit codes are captured with a random sentinel echoed
after each command, so `rcmd exec` returns the *real* remote exit status.

Transports:
  - ssh     : pexpect spawn ssh (Linux/Mac only — pexpect needs Unix PTY)
  - telnet  : pexpect spawn telnet (Linux/Mac only)
  - serial  : pyserial direct (cross-platform, works on Windows)
  - adb     : `adb shell` subprocess pipe (cross-platform)

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
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import uuid

HOME = os.path.expanduser("~")
RUN_DIR = os.path.join(HOME, ".cache", "rcmd")
LOG_PATH = os.path.join(RUN_DIR, "daemon.log")
CONFIG_PATH = os.environ.get(
    "RCMD_CONFIG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices.yaml")
)
DEFAULT_TIMEOUT = int(os.environ.get("RCMD_TIMEOUT", "30"))

# --- Socket transport: use TCP on Windows, Unix socket elsewhere -----------
IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    SOCK_HOST = "127.0.0.1"
    SOCK_PORT = int(os.environ.get("RCMD_PORT", "47321"))
    SOCK_PATH = None
else:
    SOCK_PATH = os.path.join(RUN_DIR, "rcmd.sock")
    SOCK_HOST = None
    SOCK_PORT = None

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
                if val and val[0] in "\"'":
                    q = val[0]
                    end = val.find(q, 1)
                    val = val[1:end] if end != -1 else val[1:]
                else:
                    val = val.split("#", 1)[0].strip()
                cur[key.strip()] = val
    return devices


# --------------------------------------------------------------------------
# Socket helpers — abstract over Unix socket vs TCP
# --------------------------------------------------------------------------
def make_server_socket():
    """Create and bind the daemon's listening socket."""
    if IS_WINDOWS:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((SOCK_HOST, SOCK_PORT))
    else:
        if os.path.exists(SOCK_PATH):
            os.unlink(SOCK_PATH)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCK_PATH)
    srv.listen(16)
    return srv


def make_client_socket():
    """Create and connect a client socket to the daemon."""
    if IS_WINDOWS:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((SOCK_HOST, SOCK_PORT))
    else:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCK_PATH)
    return sock


def cleanup_socket():
    """Remove the Unix socket file (no-op on Windows)."""
    if not IS_WINDOWS and os.path.exists(SOCK_PATH):
        try:
            os.unlink(SOCK_PATH)
        except Exception:
            pass


# ==========================================================================
# SESSION: ssh / telnet  (pexpect-based, Linux/Mac only)
# ==========================================================================
class Session:
    """One persistent remote shell for a single device (ssh/telnet)."""

    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg
        self.child = None
        self.connected = False
        self._raw_log = None

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
        self._raw_log = open(self._raw_log_path(), "a")
        child.logfile_read = self._raw_log

        login_prompt = cfg.get("login_prompt")
        password_prompt = cfg.get("password_prompt", "[Pp]assword:")
        shell_prompt = cfg.get("shell_prompt", "[#$]")

        if transport == "telnet" and login_prompt:
            child.expect(login_prompt, timeout=20)
            child.sendline(cfg.get("username", ""))

        if cfg.get("password"):
            idx = child.expect([password_prompt, shell_prompt], timeout=20)
            if idx == 0:
                child.sendline(cfg["password"])

        self._handshake(child)
        self.child = child
        self.connected = True

    def _handshake(self, child):
        """Install a clean PS1, disable echo, sync on ready tokens."""
        ready = "RCMD_READY_%s" % uuid.uuid4().hex[:8]
        child.sendline(
            "export PS1='' PROMPT_COMMAND='' PAGER=cat GIT_PAGER=cat; "
            "stty -echo 2>/dev/null; echo %s" % ready
        )
        child.expect(ready + r"\r?\n", timeout=20)
        ready2 = "RCMD_SYNC_%s" % uuid.uuid4().hex[:8]
        child.sendline("echo %s" % ready2)
        child.expect(ready2 + r"\r?\n", timeout=20)
        try:
            while True:
                child.read_nonblocking(65536, timeout=0.3)
        except Exception:
            pass

    def exec(self, command, timeout=DEFAULT_TIMEOUT):
        import pexpect

        if not self.connected:
            self.connect()
        marker = "___RCMD_%s___" % uuid.uuid4().hex[:12]
        self.child.sendline(command)
        self.child.sendline("__rc=$?; echo %s:$__rc" % marker)
        try:
            self.child.expect(r"%s:(\d+)" % marker, timeout=timeout)
        except pexpect.TIMEOUT:
            self._interrupt_and_resync()
            raise TimeoutError(
                "command timed out after %ss (interactive program? use `rcmd raw`)"
                % timeout
            )
        code = int(self.child.match.group(1))
        out = self.child.before
        out = self._clean(out, command)
        return out, code

    def _interrupt_and_resync(self):
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
            self.close()

    def _clean(self, out, command):
        out = out.replace("\r", "")
        lines = out.split("\n")
        if lines and command.strip() and lines[0].strip() == command.strip():
            lines = lines[1:]
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

    def _raw_log_path(self):
        return os.path.join(RUN_DIR, "session-%s.log" % self.name)

    def close(self):
        if self.child:
            try:
                self.child.close(force=True)
            except Exception:
                pass
        if self._raw_log:
            try:
                self._raw_log.close()
            except Exception:
                pass
        self.connected = False


# ==========================================================================
# SESSION: serial  (pyserial-based, cross-platform including Windows)
# ==========================================================================
class SerialSession:
    """One persistent remote shell over a serial port (pyserial).

    Uses the same sentinel mechanism as Session — the transport is the only
    difference. Works on Windows, Linux, and Mac.
    """

    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg
        self.ser = None
        self.connected = False
        self._raw_log = None

    def connect(self):
        import serial

        cfg = self.cfg
        port = cfg["port"]
        baud = int(cfg.get("baud", "115200"))

        self.ser = serial.Serial(port, baud, timeout=0.05)
        self._raw_log = open(self._raw_log_path(), "a")
        time.sleep(0.5)

        # Flush any pending data in the receive buffer (hardware-level clear)
        self.ser.reset_input_buffer()

        # Send a newline to provoke a prompt
        self._write("\r\n")
        time.sleep(1)
        self.ser.reset_input_buffer()

        # Same handshake as ssh/telnet: install clean PS1, disable echo, sync
        self._handshake()
        self.connected = True

    def _write(self, text):
        """Write text to the serial port and log it."""
        data = text.encode()
        self.ser.write(data)
        if self._raw_log:
            self._raw_log.write(data.decode(errors="replace"))
            self._raw_log.flush()

    def _read_until(self, pattern, timeout=20):
        """Read from serial until regex pattern is found. Returns (before, match).

        Uses blocking read(1024) with short timeout instead of polling
        in_waiting — more reliable on Windows and avoids buffer overflow.
        """
        rx = re.compile(pattern)
        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self.ser.read(1024)  # blocks up to timeout (50ms)
            if chunk:
                text = chunk.decode(errors="replace")
                buf += text
                if self._raw_log:
                    self._raw_log.write(text)
                    self._raw_log.flush()
                m = rx.search(buf)
                if m:
                    before = buf[:m.start()]
                    return before, m
        raise TimeoutError("serial: timed out waiting for pattern %r after %ss" % (pattern, timeout))

    def _drain(self, duration=0.5):
        """Read and discard all pending data for a short duration."""
        buf = ""
        deadline = time.time() + duration
        while time.time() < deadline:
            chunk = self.ser.read(1024)
            if chunk:
                text = chunk.decode(errors="replace")
                buf += text
                if self._raw_log:
                    self._raw_log.write(text)
                    self._raw_log.flush()
            else:
                time.sleep(0.02)
        return buf

    def _handshake(self):
        """Install a clean PS1, disable echo, sync on ready tokens.

        Note: serial reattaches to the same shell (unlike ssh which spawns a
        new one), so we explicitly cd ~ to give reset() a clean state.
        """
        ready = "RCMD_READY_%s" % uuid.uuid4().hex[:8]
        self._write(
            "cd ~; export PS1='' PROMPT_COMMAND='' PAGER=cat GIT_PAGER=cat; "
            "stty -echo 2>/dev/null; echo %s\r" % ready
        )
        self._read_until(ready + r"\r?\n", timeout=20)

        ready2 = "RCMD_SYNC_%s" % uuid.uuid4().hex[:8]
        self._write("echo %s\r" % ready2)
        self._read_until(ready2 + r"\r?\n", timeout=20)

        # Clear any residual output (banners, echoed handshake lines)
        self.ser.reset_input_buffer()

    def exec(self, command, timeout=DEFAULT_TIMEOUT):
        """Run one command, return (output, exit_code). Raises on timeout."""
        if not self.connected:
            self.connect()
        # Clear hardware input buffer (instant, avoids read-timing race on Windows)
        self.ser.reset_input_buffer()
        marker = "___RCMD_%s___" % uuid.uuid4().hex[:12]
        sentinel_cmd = "__rc=$?; echo %s:$__rc" % marker
        # Send command + sentinel as a SINGLE write to avoid timing issues
        self._write(command + "\r" + sentinel_cmd + "\r")
        try:
            before, m = self._read_until(r"%s:(\d+)" % marker, timeout=timeout)
        except TimeoutError:
            self._interrupt_and_resync()
            raise TimeoutError(
                "command timed out after %ss (interactive program? use `rcmd raw`)"
                % timeout
            )
        code = int(m.group(1))
        out = self._clean(before, command)
        return out, code

    def _interrupt_and_resync(self):
        """Recover after a timeout: send Ctrl-C, resync, clear buffer."""
        try:
            self._write("\x03")  # Ctrl-C
            time.sleep(0.3)
            self.ser.reset_input_buffer()
            sync = "RCMD_RESYNC_%s" % uuid.uuid4().hex[:8]
            self._write("echo %s\r" % sync)
            self._read_until(sync + r"\r?\n", timeout=5)
            self.ser.reset_input_buffer()
        except Exception:
            self.close()

    def _clean(self, out, command):
        # Serial consoles often use \r or \r\n for line breaks, and stty -echo
        # may not work, so the command and sentinel command are echoed back.
        # Normalize line endings, then strip echoed command lines.
        out = out.replace("\r\n", "\n").replace("\r", "\n")
        lines = out.split("\n")
        cleaned = []
        for line in lines:
            stripped = line.strip()
            # Skip the echoed command line
            if stripped == command.strip():
                continue
            # Skip the echoed sentinel command line
            if stripped.startswith("__rc=$?; echo ___RCMD_"):
                continue
            cleaned.append(line)
        # Remove leading blank lines
        while cleaned and cleaned[0] == "":
            cleaned.pop(0)
        return "\n".join(cleaned).rstrip("\n")

    def raw(self, keys):
        if not self.connected:
            self.connect()
        self._write(keys)
        time.sleep(0.5)
        return self._drain(1.0)

    def _raw_log_path(self):
        return os.path.join(RUN_DIR, "session-%s.log" % self.name)

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        if self._raw_log:
            try:
                self._raw_log.close()
            except Exception:
                pass
        self.connected = False


# ==========================================================================
# SESSION: adb  (subprocess pipe to `adb shell`, cross-platform)
# ==========================================================================
class AdbSession:
    """One persistent remote shell over `adb shell` (subprocess pipe).

    Unlike a serial/TTY transport, `adb shell` without -t is a *pipe*: no
    PTY, so commands must end with \\n (not \\r) and stty -echo has no effect.
    We keep a persistent `adb -s <serial> shell` process and drive it with a
    reader thread + queue. The same sentinel mechanism gives us accurate
    exit codes and stateful sessions (cd persists across calls).

    Requires: adb in PATH (Android platform-tools).
    """

    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg
        self.proc = None
        self.q = None
        self.connected = False
        self._raw_log = None

    # -- helpers ----------------------------------------------------------

    def _adb_base(self):
        """['adb', '-s', serial] or ['adb'] if no serial configured."""
        serial = self.cfg.get("serial") or self.cfg.get("device")
        if serial:
            return ["adb", "-s", serial]
        return ["adb"]

    def _write(self, text):
        """Write text to the adb shell stdin and log it."""
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("adb shell not connected")
        self.proc.stdin.write(text.encode())
        self.proc.stdin.flush()
        if self._raw_log:
            self._raw_log.write(text)
            self._raw_log.flush()

    def _read_until(self, pattern, timeout=20):
        """Read from the queue until regex pattern is found.

        Returns (before, match). Chunks may arrive split arbitrarily, so we
        accumulate into a buffer and search the whole buffer each time.
        """
        rx = re.compile(pattern)
        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if chunk is None:  # EOF — adb shell exited
                raise EOFError("adb shell closed unexpectedly")
            # Null bytes from device-tree/files can glue output to the echoed
            # sentinel line; drop them so line parsing stays clean.
            chunk = chunk.replace("\x00", "")
            buf += chunk
            if self._raw_log:
                self._raw_log.write(chunk)
                self._raw_log.flush()
            m = rx.search(buf)
            if m:
                before = buf[:m.start()]
                return before, m
        raise TimeoutError(
            "adb: timed out waiting for pattern %r after %ss" % (pattern, timeout)
        )

    def _drain(self, duration=0.5):
        """Read and discard all pending output for a short duration."""
        buf = ""
        deadline = time.time() + duration
        while time.time() < deadline:
            try:
                chunk = self.q.get(timeout=0.05)
            except queue.Empty:
                continue
            if chunk is None:
                break
            buf += chunk
            if self._raw_log:
                self._raw_log.write(chunk)
                self._raw_log.flush()
        return buf

    # -- lifecycle --------------------------------------------------------

    def connect(self):
        if self.connected:
            return
        cmd = self._adb_base() + ["shell"]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        self._raw_log = open(self._raw_log_path(), "a")
        self.q = queue.Queue()

        def reader():
            while True:
                chunk = self.proc.stdout.read(1024)
                if not chunk:
                    self.q.put(None)
                    break
                self.q.put(chunk.decode(errors="replace"))

        threading.Thread(target=reader, daemon=True).start()

        # Drain boot banner / initial prompt, then handshake
        time.sleep(0.5)
        self._drain(0.5)
        self._handshake()
        self.connected = True

    def _handshake(self):
        """Install clean PS1, sync on ready tokens (pipe: \\n line endings)."""
        ready = "RCMD_READY_%s" % uuid.uuid4().hex[:8]
        self._write(
            "export PS1='' PROMPT_COMMAND='' PAGER=cat GIT_PAGER=cat; "
            "cd ~; echo %s\n" % ready
        )
        self._read_until(ready + r"\r*\n", timeout=20)

        ready2 = "RCMD_SYNC_%s" % uuid.uuid4().hex[:8]
        self._write("echo %s\n" % ready2)
        self._read_until(ready2 + r"\r*\n", timeout=20)

        self._drain(0.5)

    def exec(self, command, timeout=DEFAULT_TIMEOUT):
        """Run one command, return (output, exit_code). Raises on timeout."""
        if not self.connected:
            self.connect()
        # Drain residual output so we don't match a stale sentinel
        self._drain(0.3)
        marker = "___RCMD_%s___" % uuid.uuid4().hex[:12]
        sentinel_cmd = "__rc=$?; echo %s:$__rc" % marker
        # Single write: command + sentinel, \n terminated (adb shell is a pipe)
        self._write(command + "\n" + sentinel_cmd + "\n")
        try:
            before, m = self._read_until(r"%s:(\d+)" % marker, timeout=timeout)
        except (TimeoutError, EOFError):
            self._interrupt_and_resync()
            raise TimeoutError(
                "command timed out after %ss (interactive program? use `rcmd raw`)"
                % timeout
            )
        code = int(m.group(1))
        out = self._clean(before, command)
        return out, code

    def _interrupt_and_resync(self):
        """Recover after a timeout: Ctrl-C, resync, drain."""
        try:
            self._write("\x03")
            time.sleep(0.3)
            self._drain(0.5)
            sync = "RCMD_RESYNC_%s" % uuid.uuid4().hex[:8]
            self._write("echo %s\n" % sync)
            self._read_until(sync + r"\r*\n", timeout=5)
            self._drain(0.3)
        except Exception:
            self.close()

    def _clean(self, out, command):
        # adb shell pipe echoes the command line(s); strip them like serial.
        # adb emits \r\r\n line endings — normalize to \n, collapse blank runs.
        # Output may be glued to the echoed sentinel line by \r or \x00 — cut
        # everything from the echoed "__rc=$?; echo ___RCMD_..." marker onward.
        out = out.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
        idx = out.find("__rc=$?; echo ___RCMD_")
        if idx != -1:
            out = out[:idx]
        lines = out.split("\n")
        cleaned = []
        for line in lines:
            stripped = line.strip()
            if stripped == command.strip():
                continue
            # Drop any line carrying sentinel remnants (echoed command, partial
            # markers glued by \r, or ":$__rc" tails)
            if "___RCMD_" in stripped or stripped.endswith(":$__rc") or re.match(r"^__rc=\$?; echo", stripped):
                continue
            # Skip prompt remnants like "/ #" or "root@host:/#"
            if re.match(r"^/?[^ ]* ?[#$>] $", stripped) or stripped in ("/ #", "#", "$"):
                continue
            cleaned.append(stripped)
        # Drop leading/trailing/consecutive blank lines
        while cleaned and cleaned[0] == "":
            cleaned.pop(0)
        while cleaned and cleaned[-1] == "":
            cleaned.pop()
        result = []
        for line in cleaned:
            if line == "" and result and result[-1] == "":
                continue
            result.append(line)
        return "\n".join(result)

    def raw(self, keys):
        if not self.connected:
            self.connect()
        self._write(keys)
        time.sleep(0.5)
        return self._drain(1.0)

    def _raw_log_path(self):
        return os.path.join(RUN_DIR, "session-%s.log" % self.name)

    def close(self):
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
            except Exception:
                pass
        if self._raw_log:
            try:
                self._raw_log.close()
            except Exception:
                pass
        self.connected = False


# ==========================================================================
# Session factory — route to the right transport
# ==========================================================================
def create_session(name, cfg):
    transport = cfg.get("transport", "ssh")
    if transport == "serial":
        return SerialSession(name, cfg)
    if transport == "adb":
        return AdbSession(name, cfg)
    return Session(name, cfg)


# ==========================================================================
# DAEMON
# ==========================================================================
class Daemon:
    def __init__(self):
        self.devices = load_config(CONFIG_PATH)
        self.sessions = {}

    def get_session(self, name):
        if name not in self.devices:
            raise KeyError("unknown device: %s" % name)
        if name not in self.sessions:
            self.sessions[name] = create_session(name, self.devices[name])
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
                            "host": c.get("host") or c.get("port"),
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
        srv = make_server_socket()
        self._log("daemon listening on %s" % ("TCP %s:%d" % (SOCK_HOST, SOCK_PORT) if IS_WINDOWS else SOCK_PATH))
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
            srv.close()
            cleanup_socket()

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
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "daemon"],
        stdout=open(LOG_PATH, "a"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        # On Windows, CREATE_NEW_PROCESS_GROUP + DETACHED_PROCESS equivalent
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0,
        start_new_session=not IS_WINDOWS,
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
    sock = make_client_socket()
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
        return resp["exit_code"]

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
