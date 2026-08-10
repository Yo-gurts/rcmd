---
name: rcmd
description: Run commands on remote telnet, ssh, serial or adb devices (embedded boards, dev servers) with stateful sessions and accurate exit codes, via the `rcmd` CLI. Use whenever a task involves executing shell commands on a remote device over telnet/ssh/serial/adb — inspecting logs, checking status, running scripts on a board or server. Prefer this over raw `ssh host cmd` (no state), `sshpass`, or `tmux send-keys` (no exit code). Devices are pre-configured by name in ~/rcmd/devices.yaml.
---

# rcmd — 远程 telnet/ssh/serial/adb 命令

`rcmd` 让你在远程 telnet/ssh/serial/adb 设备上跑命令，像本地一样：**有状态**（`cd`/env 跨调用保留）、**退出码准确透传**、telnet/ssh/serial/adb **统一接口**。已在 `PATH` 中，直接调用 `rcmd`。

## 何时用

任务涉及「在某台远程设备/板子/服务器上执行命令」时用它，而不是自己拼 `ssh`/`sshpass`/`tmux send-keys`。设备已在 `~/rcmd/devices.yaml` 里按名字配好（如 `board` = telnet 嵌入式板，`server` = ssh 开发机）。

## 核心命令

```bash
rcmd ls                          # 先看有哪些设备、是否已连接
rcmd exec <device> "<cmd>"       # 主力：跑命令，rcmd 退出码 == 远程退出码
rcmd reset <device>              # 会话乱了/要干净环境时重连
rcmd raw <device> "<keys>"       # 仅交互式程序用，发原始按键（无退出码）
rcmd logs <device> [n]           # 调试：看该会话最近原始 I/O
rcmd --help                      # 完整用法
```

判断成败就看退出码，和本地命令一样：

```bash
rcmd exec board "test -f /etc/foo && echo yes"; echo $?
```

## 必须记住的三条规则（否则会卡住）

1. **`exec` 只用于会返回的命令**。交互式全屏程序（`top`、`vi`、`tail -f`、等待输入的 `sudo`）不会返回，`exec` 会**超时**（默认 30s）。
   - 要 `top` 数据 → 用 **batch 模式**：procps 用 `top -b -n1`，busybox（多数嵌入式板）用 `top -b -n1` 或带线程 `top -bH -n1`。
   - 要 `tail -f` → 改成 `tail -n 100`。
   - 真需要实时交互操控才用 `rcmd raw`（发原始字节，自己带 `\n`，读取窗口约 1s）。

2. **会话有状态**：一次 `cd`/`export` 影响后续调用。需要干净环境时先 `rcmd reset <device>`。

3. **超时会自动恢复**：命令超时后 rcmd 自动发 Ctrl-C 并重新同步，下一条命令照常工作，不必手动 reset。命令里可安全包含 `#` 注释、管道、引号。

## 上手流程

先 `rcmd ls` 确认设备名和连通性，再用 `rcmd exec <device> "..."`。不确定设备上是 busybox 还是 procps 工具时，先 `rcmd exec <device> "<tool> --help 2>&1 | head"` 探一下参数。

## 配置

设备定义在 `~/rcmd/devices.yaml`（含密码，已 gitignore；模板见 `devices.yaml.example`）。加新设备就在该文件加一个命名块（transport/host/port/username/password，telnet 另需 login_prompt/password_prompt；serial 需 port/baud；adb 需 serial）。环境变量：`RCMD_TIMEOUT`（每条命令超时秒数）、`RCMD_CONFIG`（配置路径）。

仓库：`git@github.com:Yo-gurts/rcmd.git`。
