# rcmd — 像本地命令一样远程执行

在远程 **telnet**、**ssh** 或 **serial** 设备上执行命令，就像在本地一样。
每个设备保持一个持久 shell，`cd` / env / 状态跨调用保留，每次 `exec`
都返回**真实的远程退出码**。为 AI 工具（可从 Bash 工具调用）和人类用户
设计。支持 Linux 和 Windows。

## 为什么用 rcmd

`ssh host cmd` 无状态（不支持 `cd`），`tmux send-keys` 无法捕获退出码，
telnet 更是完全没有干净的自动化方案。`rcmd` 一次性解决这三个问题：

- **有状态** — daemon 为每个设备持有长期存活的 shell。
- **准确** — 每条命令后回显一个随机哨兵标记命令边界并携带 `$?`，
  因此 `rcmd` 自身的退出码 == 远程命令的退出码。
- **统一** — telnet、ssh、serial 对调用方完全一致。

## 架构

```
  caller ──> rcmd (thin CLI, new process each call)
                 │  socket: AF_UNIX 或 AF_INET (Windows 上用 TCP localhost)
                 ▼
             rcmd daemon (persistent)
                 ├─ session[board]       → pexpect telnet shell   (有状态)
                 ├─ session[server]      → pexpect ssh shell       (有状态)
                 └─ session[serial_board]→ pyserial UART shell     (有状态)
```

daemon 首次使用时自动启动。命令边界 + 退出码机制：

```
send:   <cmd>; __rc=$?; echo ___RCMD_<rand>___:$__rc
expect: ___RCMD_<rand>___:(\d+)      # \d+ 即退出码；前面的文本是输出
```

## 安装

```bash
pip3 install --user pexpect          # telnet/ssh 需要（Linux）
pip3 install --user pyserial         # serial 传输需要
cp devices.yaml.example devices.yaml # 然后编辑填入真实设备
```

**Windows** 上 `pexpect` 不可用，因此只支持 `serial` 传输；daemon 会自动
使用 TCP localhost socket 替代 Unix socket。

编辑 `devices.yaml` 描述你的设备（telnet 需要 login/password 提示符；
ssh 需要用户 + 密码或密钥认证；serial 需要 port + baud）。
`devices.yaml` 已被 gitignore，凭据永远不会被提交。

可选：把 `~/rcmd` 加入 `PATH`，即可在任何地方调用 `rcmd`：

```bash
echo 'export PATH="$HOME/rcmd:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

## Claude Code / AI skill

`skills/rcmd/SKILL.md` 教 AI 助手何时以及如何使用 `rcmd`
（exec 与 raw 的区别、`top` 的 batch 模式、有状态会话）。安装后任何会话
都会自动识别：

```bash
mkdir -p ~/.claude/skills
ln -s "$PWD/skills/rcmd" ~/.claude/skills/rcmd   # 或 cp -r
```

## 用法

```bash
./rcmd exec <device> "<command>"   # 执行命令；退出码透传
./rcmd ls                          # 列出设备 + 连接状态
./rcmd reset <device>              # 断开并重连（清除 cd/env）
./rcmd raw <device> "<keys>"       # 发送原始按键（无退出码）
./rcmd logs <device> [n]           # 最近 n 行原始会话 I/O（调试用）
./rcmd stop                        # 停止 daemon
```

示例：

```bash
./rcmd exec server "uname -a"
./rcmd exec board  "cd /tmp"      # 有状态...
./rcmd exec board  "pwd"          # ...保留 → /tmp
./rcmd exec board  "false"; echo $?   # → 1，真实的远程退出码
./rcmd exec serial_board "df -h"  # 串口控制台用法完全相同
```

## AI 调用方注意事项

- `rcmd exec` 用于**会返回**（有退出码）的命令。其退出码镜像远程命令的
  退出码——像检查本地命令一样检查它。
- **交互式程序**（`top`、`vi`、`sudo` 密码提示、`tail -f`）不会发出哨兵，
  会**超时**（默认 30s，可用 `RCMD_TIMEOUT` 调整）。这类程序用
  `rcmd raw`，或尽量避免。
- 会话是**有状态的**：一次调用中的 `cd` 会影响下一次调用。需要干净环境
  时用 `rcmd reset <device>`。
- 超时会在 stderr 输出明确错误并返回非零退出码；会话仍然存活，但残留的
  运行中命令可能还在输出——如果输出看起来错位，执行 `reset`。

## 配置参考（`devices.yaml`）

| key             | telnet | ssh | serial | 含义                                      |
|-----------------|:------:|:---:|:------:|-------------------------------------------|
| `transport`     |   ✓    |  ✓  |   ✓    | `telnet`、`ssh` 或 `serial`               |
| `host` / `port` |   ✓    |  ✓  |        | 网络地址                                  |
| `username`      |   ✓    |  ✓  |        | 登录用户                                  |
| `password`      |   ✓    |  ○  |        | telnet 必需；ssh 用它或走密钥             |
| `login_prompt`  |   ✓    |     |        | 发送用户名前等待的正则提示符              |
| `password_prompt`|  ○    |  ○  |        | 发送密码前等待的正则提示符                |
| `shell_prompt`  |   ○    |  ○  |        | 交互 shell 就绪的正则提示符               |
| `port` (serial) |        |     |   ✓    | 串口设备路径（COM3 / /dev/ttyUSB0）       |
| `baud` (serial) |        |     |   ○    | 波特率，默认 115200                       |

环境变量：`RCMD_CONFIG`（配置路径）、`RCMD_TIMEOUT`（每条命令超时秒数）。

---

[English README](README.en.md)
