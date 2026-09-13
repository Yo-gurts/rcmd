# rcmd — run remote commands like local

Run commands on remote **telnet**, **ssh**, **serial**, **serial_bridge**,
**prompt**/**prompt_bridge** or **adb** devices as if
they were local. One persistent shell per device keeps `cd` / env / state
across calls, and every `exec` returns the **real remote exit code**. Built
for AI tools (call it from the Bash tool) and humans alike. Works on Linux
and Windows.

## Why

`ssh host cmd` is stateless (no `cd`), `tmux send-keys` can't capture exit
codes, and telnet has no clean automation at all. `rcmd` solves all three:

- **Stateful** — a daemon holds one long-lived shell per device.
- **Accurate** — a random sentinel echoed after each command marks the exact
  command boundary and carries `$?`, so `rcmd`'s own exit code == the remote
  command's exit code.
- **Uniform** — telnet, ssh, serial, serial_bridge and adb all look identical
  to the caller.

## Architecture

```
  caller ──> rcmd (thin CLI, new process each call)
                 │  socket: AF_UNIX or AF_INET (TCP localhost on Windows)
                 ▼
             rcmd daemon (persistent)
                 ├─ session[board]        → pexpect telnet shell        (stateful)
                 ├─ session[server]       → pexpect ssh shell           (stateful)
                 ├─ session[serial_board] → pyserial UART shell         (stateful)
                 ├─ session[bridge_board] → serial-bridge WebSocket gw  (stateful)
                 └─ session[adb_board]    → adb shell pipe              (stateful)
```

The `serial_bridge` transport puts the serial port on **another host**: that
machine runs the [serial-bridge](https://github.com/Yo-gurts/serial-bridge)
gateway (owns the UART, exposes it over WebSocket) and rcmd connects to it —
handy when rcmd runs on a server but the device's UART is attached to a
Windows / other machine. It reuses the whole `serial` sentinel machinery, only
the underlying byte pipe is a WebSocket.

The daemon auto-starts on first use. Command boundary + exit code:

```
send:   <cmd>; __rc=$?; echo ___RCMD_<rand>___:$__rc
expect: ___RCMD_<rand>___:(\d+)      # \d+ is the exit code; text before = output
```

## Setup

```bash
pip3 install --user pexpect          # required for telnet/ssh (Linux)
pip3 install --user pyserial         # required for serial transport
pip3 install --user websocket-client # required for serial_bridge / prompt_bridge
cp devices.yaml.example devices.yaml # then edit with your real hosts
```

Dependencies are **optional per transport** — install only what you use, or
`pip install -r requirements.txt` to grab them all.

**A venv is recommended** (keeps rcmd's deps from clashing with system tools,
e.g. yoctools pinning an older ruamel.yaml):

```bash
./setup_venv.sh                      # create .venv and install deps
```

Nothing else to do afterward — `rcmd.py` **auto-switches to that `.venv`** on
startup (the daemon follows), so `./rcmd.py ...` or the PATH-symlinked `rcmd`
just work.

The **adb** transport needs `adb` in your `PATH` (Android platform-tools);
no Python dependency required.

On **Windows**, `pexpect` is unavailable so only `serial`, `serial_bridge` and
`adb` transports work; the daemon automatically uses a TCP localhost socket
instead of a Unix socket.

Edit `devices.yaml` to describe your devices (telnet needs login/password
prompts; ssh needs user + password or key auth; serial needs port + baud;
adb needs the device serial). `devices.yaml` is gitignored so your
credentials never get committed.

Optionally add `~/rcmd` to your `PATH` so you can call `rcmd` from anywhere:

```bash
echo 'export PATH="$HOME/rcmd:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

## Claude Code / AI skill

`skills/rcmd/SKILL.md` teaches an AI assistant when and how to use `rcmd`
(exec vs raw, batch mode for `top`, stateful sessions). Install it so any
session picks it up automatically:

```bash
mkdir -p ~/.claude/skills
ln -s "$PWD/skills/rcmd" ~/.claude/skills/rcmd   # or cp -r
```

## Usage

```bash
./rcmd exec <device> "<command>"   # run a command; exit code propagates
./rcmd exec <device> "<cmd>" -t 60 # set per-command timeout (seconds, default 30)
./rcmd push <device> <local> <remote>   # push a file to the device (ssh/scp; password auth uses sshpass)
./rcmd pull <device> <remote> <local>   # pull a file from the device
./rcmd ls                          # list devices + connection state
./rcmd reset <device>              # drop & reconnect (clears cd/env)
./rcmd raw <device> "<keys>"       # send raw keystrokes (no exit code)
./rcmd logs <device> [n]           # last n lines of raw session I/O (debug)
./rcmd stop                        # stop the daemon
```

Examples:

```bash
./rcmd exec server "uname -a"
./rcmd exec board  "cd /tmp"      # state...
./rcmd exec board  "pwd"          # ...persists → /tmp
./rcmd exec board  "false"; echo $?   # → 1, real remote exit code
./rcmd exec serial_board "df -h"  # serial console works the same way
./rcmd exec bridge_board "df -h"  # serial over a serial-bridge gateway, same way
./rcmd exec adb_board "uname -a"  # adb device works the same way
./rcmd push board ./fw.bin /mnt/data/fw.bin   # no more hand-typing sshpass+scp
```

## Notes for AI callers

- `rcmd exec` is for commands that **return** (have an exit code). Its exit
  code mirrors the remote command — check it the same way you'd check a local
  command.
- **Interactive programs** (`top`, `vi`, `sudo` password prompt, `tail -f`)
  never emit the sentinel and will **time out** (default 30s, set
  `RCMD_TIMEOUT`). Use `rcmd raw` for those, or avoid them.
- Sessions are **stateful**: a `cd` in one call affects the next. Use
  `rcmd reset <device>` to get a clean shell.
- A timeout returns a clear error on stderr and a non-zero exit; the session
  stays alive, so a stray running command may still be draining — `reset` if
  output looks misaligned.

## Config reference (`devices.yaml`)

| key            | telnet | ssh | serial | serial_bridge | adb | meaning                                   |
|----------------|:------:|:---:|:------:|:-------------:|:---:|-------------------------------------------|
| `transport`    |   ✓    |  ✓  |   ✓    |       ✓       |  ✓  | `telnet`, `ssh`, `serial`, `serial_bridge` or `adb` |
| `host` / `port`|   ✓    |  ✓  |        |               |     | network address                           |
| `username`     |   ✓    |  ✓  |        |               |     | login user                                |
| `password`     |   ✓    |  ○  |        |               |     | required for telnet; ssh uses it or a key |
| `login_prompt` |   ✓    |     |        |               |     | regex awaited before sending username     |
| `password_prompt`|  ○   |  ○  |        |               |     | regex awaited before sending password     |
| `shell_prompt` |   ○    |  ○  |        |               |     | regex hint for the interactive shell      |
| `port`         |        |     |   ✓    |       ✓       |     | serial: local device path (COM3 / /dev/ttyUSB0); serial_bridge: the COM port **on the gateway host** |
| `baud`         |        |     |   ○    |       ○       |     | baud rate, default 115200                 |
| `url`          |        |     |        |       ✓       |     | serial-bridge gateway WebSocket (`ws://host:port/ws`) |
| `token`        |        |     |        |       ○       |     | gateway `--token`; omit if the gateway is token-free  |
| `serial` (adb) |        |     |        |               |  ○  | adb device serial (`adb devices -l`); omit to use the single device |

> **serial_bridge prerequisite**: another host runs the
> [serial-bridge](https://github.com/Yo-gurts/serial-bridge) gateway
> (`python server.py --host 0.0.0.0 --token <token>`), and rcmd side has
> `pip install websocket-client`. Serial ports are exclusive — if the gateway
> already has another client (e.g. the browser UI) holding the same port,
> rcmd's open will fail as busy.

### prompt / prompt_bridge — prompt-only shells with no `$?` (RT-Thread msh, U-Boot…)

`serial`/`serial_bridge`/`ssh` get exit codes via a bash sentinel
(`__rc=$?; echo MARKER:$__rc`). Shells like **RT-Thread msh / FinSH** and
**U-Boot** have no `$?` and no `;` sequencing, so the sentinel just times out.
`prompt` (local serial) and `prompt_bridge` (over a serial-bridge gateway)
handle them:

- **Command boundary = the prompt reappearing** — a regex, configurable per
  device via `prompt:`. Default matches RT-Thread msh (including the path after
  `cd`, e.g. `msh /mnt>`); for U-Boot set `prompt: "=> "`.
- **Exit code = heuristic** (these shells expose no real code): output matching
  `error_pattern:` (default detects `command not found`) → `127`, else `0`.
  Widen `error_pattern` to flag more failures as non-zero.
- Output is auto-stripped of ANSI colors, the echoed command line, and the
  trailing prompt; `cd`/state still persists across calls.

```bash
./rcmd exec msh_board "version"     # → RT-Thread banner, exit 0
./rcmd exec msh_board "foobar"      # → "foobar: command not found.", exit 127
./rcmd exec msh_board "cd /mnt"; ./rcmd exec msh_board "pwd"   # → /mnt (stateful)
```

Env: `RCMD_CONFIG` (config path), `RCMD_TIMEOUT` (per-command seconds).
