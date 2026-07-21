# rcmd — run remote commands like local

Run commands on remote **telnet** or **ssh** devices as if they were local.
One persistent shell per device keeps `cd` / env / state across calls, and
every `exec` returns the **real remote exit code**. Built for AI tools (call
it from the Bash tool) and humans alike.

## Why

`ssh host cmd` is stateless (no `cd`), `tmux send-keys` can't capture exit
codes, and telnet has no clean automation at all. `rcmd` solves all three:

- **Stateful** — a daemon holds one long-lived shell per device.
- **Accurate** — a random sentinel echoed after each command marks the exact
  command boundary and carries `$?`, so `rcmd`'s own exit code == the remote
  command's exit code.
- **Uniform** — telnet and ssh look identical to the caller.

## Architecture

```
  caller ──> rcmd (thin CLI, new process each call)
                 │  Unix socket (length-prefixed JSON)
                 ▼
             rcmd daemon (persistent)
                 ├─ session[board]  → pexpect telnet shell   (stateful)
                 └─ session[server] → pexpect ssh shell       (stateful)
```

The daemon auto-starts on first use. Command boundary + exit code:

```
send:   <cmd>; __rc=$?; echo ___RCMD_<rand>___:$__rc
expect: ___RCMD_<rand>___:(\d+)      # \d+ is the exit code; text before = output
```

## Setup

```bash
pip3 install --user pexpect          # only dependency
cp devices.yaml.example devices.yaml # then edit with your real hosts
```

Edit `devices.yaml` to describe your devices (telnet needs login/password
prompts; ssh needs user + password or key auth). `devices.yaml` is gitignored
so your credentials never get committed.

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

| key             | telnet | ssh | meaning                                   |
|-----------------|:------:|:---:|-------------------------------------------|
| `transport`     |   ✓    |  ✓  | `telnet` or `ssh`                         |
| `host` / `port` |   ✓    |  ✓  | address                                   |
| `username`      |   ✓    |  ✓  | login user                                |
| `password`      |   ✓    |  ○  | required for telnet; ssh uses it or a key |
| `login_prompt`  |   ✓    |     | regex awaited before sending username     |
| `password_prompt`|  ○    |  ○  | regex awaited before sending password     |
| `shell_prompt`  |   ○    |  ○  | regex hint for the interactive shell      |

Env: `RCMD_CONFIG` (config path), `RCMD_TIMEOUT` (per-command seconds).
