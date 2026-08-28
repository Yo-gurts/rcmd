---
name: rcmd
description: Run commands on remote telnet, ssh, serial or adb devices (embedded boards, dev servers) with stateful sessions, accurate exit codes, and file push/pull, via the `rcmd` CLI. Use whenever a task involves executing shell commands on a remote device over telnet/ssh/serial/adb — inspecting logs, checking status, deploying or replacing binaries on a board, running scripts on a board or server. Prefer this over raw `ssh host cmd` (no state), `sshpass`, or `tmux send-keys` (no exit code). Devices are pre-configured by name in devices.yaml; the skill ships alongside the rcmd tool.
---

# rcmd — 远程 telnet/ssh/serial/adb 命令 + 文件传输

`rcmd` 让你在远程 telnet/ssh/serial/adb 设备上跑命令、传文件，像本地一样：**有状态**（`cd`/env 跨调用保留）、**退出码准确透传**、四种传输方式**统一接口**。已在 `PATH` 中，直接调用 `rcmd`。

**约定（cvitek_agent 项目）：在目标设备上执行任何命令一律走 `rcmd`，绝不用裸 `ssh`/`sshpass` 跑命令。** 这是被纠正过的强约束。

## 何时用

任务涉及「在某台远程设备/板子/服务器上执行命令或传文件」时用它，而不是自己拼 `ssh`/`sshpass`/`tmux send-keys`/裸 `scp`。设备已按名字配好（如 `board` = telnet 嵌入式板，`server` = ssh 开发机）。

## 核心命令

```bash
rcmd ls                                            # 先看有哪些设备、是否已连接
rcmd exec <device> "<cmd>"                         # 主力：跑命令，rcmd 退出码 == 远程退出码
rcmd exec <device> "<cmd>" -t 60                   # 单命令超时（秒，默认 30，可用 RCMD_TIMEOUT 覆盖）
rcmd push <device> <local> <remote>                # 推文件到设备（scp，密码自动走 sshpass）
rcmd pull <device> <remote> <local>                # 从设备拉文件
rcmd reset <device>                                # 会话乱了/要干净环境时重连
rcmd raw <device> "<keys>"                         # 仅交互式程序用，发原始按键（无退出码）
rcmd logs <device> [n]                             # 调试：看该会话最近原始 I/O
rcmd stop                                          # 停止 daemon
rcmd --help                                        # 完整用法
```

判断成败就看退出码，和本地命令一样：

```bash
rcmd exec board "test -f /etc/foo && echo yes"; echo $?
```

**注意**：若 `rcmd ls` 报「配置目录 not a directory」，每次调用都得带 `RCMD_CONFIG` 前缀，见「上手流程」。

## 必须记住的规则（否则会卡住）

1. **`exec` 只用于会返回的命令**。交互式全屏程序（`top`、`vi`、`tail -f`、等待输入的 `sudo`）不会返回，`exec` 会**超时**（默认 30s）。
   - 要 `top` 数据 → 用 **batch 模式**：procps 用 `top -b -n1`，busybox（多数嵌入式板）用 `top -b -n1` 或带线程 `top -bH -n1`。
   - 要 `tail -f` → 改成 `tail -n 100`。
   - 真需要实时交互操控才用 `rcmd raw`（发原始字节，自己带 `\n`，读取窗口约 1s）。

2. **会话有状态**：一次 `cd`/`export` 影响后续调用。需要干净环境时先 `rcmd reset <device>`。

3. **超时/断连会自愈**：命令超时后 rcmd 自动发 Ctrl-C 并重新同步；`exec` 遇到连接被断（闲置断开、隧道抖动、设备重启）会**自动重连并重试当前命令**，一般不必手动 `reset`。命令里可安全包含 `#` 注释、管道、引号。

## 上手流程

**首次使用先确认 `rcmd` 在 `PATH` 中**（`which rcmd`）。若 `command not found`，用软链装到 `~/.local/bin/`（该目录通常已在 `PATH`；不在则往 `~/.bashrc` 加 `export PATH=$HOME/.local/bin:$PATH`）：

```bash
ln -sf /data/song.yu/cvitek_agent/tools/rcmd/rcmd.py ~/.local/bin/rcmd
grep -q '.local/bin' ~/.bashrc || echo 'export PATH=$HOME/.local/bin:$PATH' >> ~/.bashrc
```

软链指向 `rcmd.py` 本体时，`rcmd` 按脚本 realpath 解析回 `tools/rcmd/`，能自动找到同目录的 `devices.yaml`，**无需带 `RCMD_CONFIG` 前缀**。

`rcmd` 默认在「`rcmd` 可执行所在目录 / 其链接目标的同目录」找 `devices.yaml`。**再 `rcmd ls` 试一次**：

- **正常出设备清单** → 直接用。
- **报配置目录错误**（如 `无法查找 rcmd 配置文件夹：... not a directory`）→ `rcmd` 解析到一个不在配置目录的链接（典型情况：经 `PATH` 链到 `/usr/bin/rcmd`）。这时**每次调用前都显式带 `RCMD_CONFIG=<devices.yaml 的路径>`**（如 cvitek_agent 仓库里的 `tools/rcmd/devices.yaml`）；别指望某次 `export` 能被后续 Bash 调用记住——shell 环境不持久，把前缀绑在每条命令上。

```bash
RCMD_CONFIG=<devices.yaml 的路径> rcmd ls                    # 确认设备名和连通性
RCMD_CONFIG=<devices.yaml 的路径> rcmd exec <device> "命令"
```

不确定设备上是 busybox 还是 procps 工具时，先 `rcmd exec <device> "<tool> --help 2>&1 | head"` 探一下参数。

## 踩坑速记（来自本项目 memory）

- **部署/替换运行中二进制**：不要 `rcmd push` 直接覆盖运行中的可执行文件，会 `Text file busy`（ETXTBSY）。正确流程：推到临时名（`rcmd push dev ./app /tmp/app.new`）→ `rcmd exec dev "md5sum /tmp/app.new"` 校验 → `rcmd exec dev "mv -f /tmp/app.new /app/bin/app"` 原子替换（rename 不碰运行中 inode）→ 按需 reboot。小容量 ROOTFS 还要先 `df -h` 确认剩余空间。
- **设备无 `timeout` 命令**：嵌入式板 busybox 常没有 `timeout`，写 `timeout N <cmd>` 会 127 静默失败、命令根本没发出。限时改用 `rcmd exec -t N`（外层超时）或命令自身的限时参数。
- **别信一次性回显判断命令是否生效**：某些设备 CLI（如大核 `alios_cli` 转发小核命令）显示的"回显"是滞后的日志环形缓冲，会读到陈旧的 `cmd not found`/help。判断生效看**客观副作用**（如 `cpuusage` 看 idle%），别只看一次回显。
- **挂测/巡检慎用会建常驻任务的命令**：如 `alios_cli` 的 `cpuusage` 无参数会永久运行；采样要带界（`cpuusage -d 500 -t 1500`）。本仓库 memory 里有更详细的 [[alios-cli-log-mmap-is-stale-ring]]、[[device-ops-must-use-rcmd]] 可查。

## 配置

设备定义在 `devices.yaml`（含密码，已 gitignore；模板见 `devices.yaml.example`）。**`rcmd` 默认在「其链接目标的同目录」找 `devices.yaml`**——也就是 `rcmd -> /some/dir/rcmd.py` 时，找的是 `/some/dir/devices.yaml`。若 `rcmd` 经 `PATH` 链接到 `/usr/bin/rcmd` 而那里没有配置，会报 `无法查找 rcmd 配置文件夹`；此时用环境变量 **`RCMD_CONFIG=<devices.yaml 的绝对路径>`** 显式指定，或 `RCMD_TIMEOUT` 调单命令超时（默认 30s）。

加新设备就在 `devices.yaml` 加一个命名块（transport/host/port/username/password，telnet 另需 login_prompt/password_prompt；serial 需 port/baud；adb 需 serial）。环境变量：`RCMD_CONFIG`（配置路径）、`RCMD_TIMEOUT`（每条命令超时秒数）。

权威文档见 rcmd 仓库根目录的 `README.md`（含架构、配置参考表）。仓库：`git@github.com:Yo-gurts/rcmd.git`。
