# rcmd — 像本地命令一样远程执行

在远程 **telnet**、**ssh**、**serial**、**serial_bridge**、**prompt**/**prompt_bridge** 或 **adb** 设备上执行命令，就像在本地一样。
每个设备保持一个持久 shell，`cd` / env / 状态跨调用保留，每次 `exec`
都返回**真实的远程退出码**。为 AI 工具（可从 Bash 工具调用）和人类用户
设计。支持 Linux 和 Windows。

## 为什么用 rcmd

`ssh host cmd` 无状态（不支持 `cd`），`tmux send-keys` 无法捕获退出码，
telnet 更是完全没有干净的自动化方案。`rcmd` 一次性解决这三个问题：

- **有状态** — daemon 为每个设备持有长期存活的 shell。
- **准确** — 每条命令后回显一个随机哨兵标记命令边界并携带 `$?`，
  因此 `rcmd` 自身的退出码 == 远程命令的退出码。
- **统一** — telnet、ssh、serial、adb 对调用方完全一致。

## 架构

```
  caller ──> rcmd (thin CLI, new process each call)
                 │  socket: AF_UNIX 或 AF_INET (Windows 上用 TCP localhost)
                 ▼
             rcmd daemon (persistent)
                 ├─ session[board]        → pexpect telnet shell         (有状态)
                 ├─ session[server]       → pexpect ssh shell            (有状态)
                 ├─ session[serial_board] → pyserial UART shell          (有状态)
                 ├─ session[bridge_board] → serial-bridge WebSocket 网关 (有状态)
                 └─ session[adb_board]    → adb shell pipe               (有状态)
```

`serial_bridge` 传输把串口放到**另一台主机**上：那台机器跑
[serial-bridge](https://github.com/Yo-gurts/serial-bridge) 网关独占 UART 并用
WebSocket 暴露，rcmd 连过去，就能像操作本地串口一样操作它。适合 rcmd 跑在
服务器、而设备 UART 插在 Windows/另一台机器上的场景。它复用 `serial` 的整套
哨兵机制，只是底层换成了 WebSocket。

daemon 首次使用时自动启动。命令边界 + 退出码机制：

```
send:   <cmd>; __rc=$?; echo ___RCMD_<rand>___:$__rc
expect: ___RCMD_<rand>___:(\d+)      # \d+ 即退出码；前面的文本是输出
```

## 安装

```bash
pip3 install --user pexpect          # telnet/ssh 需要（Linux）
pip3 install --user pyserial         # serial 传输需要
pip3 install --user websocket-client # serial_bridge / prompt_bridge 传输需要
cp devices.yaml.example devices.yaml # 然后编辑填入真实设备
```

依赖都是**按传输方式可选**的,只装用到的即可;也可以 `pip install -r requirements.txt` 一次装全。

**推荐用虚拟环境**(避免和系统里其它工具的依赖打架,如 yoctools 钉死的 ruamel.yaml):

```bash
./setup_venv.sh                      # 建 .venv 并装好依赖
```

装好后无需额外操作——`rcmd.py` 启动时会**自动切到该 `.venv`** 运行(daemon 也随之使用),照常 `./rcmd.py ...` 或用 PATH 里的软链 `rcmd ...` 即可。

**adb** 传输需要 `adb` 在你的 `PATH`（Android platform-tools），无需 Python 依赖。

**Windows** 上 `pexpect` 不可用，因此只支持 `serial` 和 `adb` 传输；daemon 会自动
使用 TCP localhost socket 替代 Unix socket。

编辑 `devices.yaml` 描述你的设备（telnet 需要 login/password 提示符；
ssh 需要用户 + 密码或密钥认证；serial 需要 port + baud；adb 需要 serial）。
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
./rcmd exec <device> "<cmd>" -t 60 # 指定单命令超时（秒，默认 30）
./rcmd push <device> <local> <remote>   # 推文件到设备（ssh/scp；密码认证自动走 sshpass）
./rcmd pull <device> <remote> <local>   # 从设备拉文件
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
./rcmd exec bridge_board "df -h"  # 经 serial-bridge 网关的串口，用法也完全相同
./rcmd exec adb_board "uname -a"  # adb 设备用法完全相同
./rcmd push board ./fw.bin /mnt/data/fw.bin   # 传文件不再需要手敲 sshpass+scp
```

**断连自愈**：ssh 会话带 `ServerAliveInterval=15` keepalive；exec 遇到
连接被断（闲置断开、隧道抖动、设备重启）会自动重连并**重试当前命令**，
调用方一般无需手动 `reset`。

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

| key            | telnet | ssh | serial | serial_bridge | adb | 含义                                      |
|----------------|:------:|:---:|:------:|:-------------:|:---:|-------------------------------------------|
| `transport`    |   ✓    |  ✓  |   ✓    |       ✓       |  ✓  | `telnet`、`ssh`、`serial`、`serial_bridge` 或 `adb` |
| `host` / `port`|   ✓    |  ✓  |        |               |     | 网络地址                                  |
| `username`     |   ✓    |  ✓  |        |               |     | 登录用户                                  |
| `password`     |   ✓    |  ○  |        |               |     | telnet 必需；ssh 用它或走密钥             |
| `login_prompt` |   ✓    |     |        |               |     | 发送用户名前等待的正则提示符              |
| `password_prompt`|  ○   |  ○  |        |               |     | 发送密码前等待的正则提示符                |
| `shell_prompt` |   ○    |  ○  |        |               |     | 交互 shell 就绪的正则提示符               |
| `port`         |        |     |   ✓    |       ✓       |     | serial：本地串口路径（COM3 / /dev/ttyUSB0）；serial_bridge：**网关那台机器上的** COM 口 |
| `baud`         |        |     |   ○    |       ○       |     | 波特率，默认 115200                       |
| `url`          |        |     |        |       ✓       |     | serial-bridge 网关 WebSocket 地址（`ws://host:port/ws`） |
| `token`        |        |     |        |       ○       |     | 网关 `--token`；网关免密时可省略          |
| `serial` (adb) |        |     |        |               |  ○  | adb 设备序列号（`adb devices -l`）；可省略取唯一设备 |

环境变量：`RCMD_CONFIG`（配置路径）、`RCMD_TIMEOUT`（每条命令超时秒数）。

> **serial_bridge 前提**：另一台主机需运行 [serial-bridge](https://github.com/Yo-gurts/serial-bridge)
> 网关（`python server.py --host 0.0.0.0 --token <token>`），rcmd 侧
> `pip install websocket-client`。串口是独占的——网关侧若已有别的客户端
> （如浏览器 UI）开着同一个口，rcmd 打开时会因端口占用失败。

### prompt / prompt_bridge —— 无 `$?` 的提示符型 shell（RT-Thread msh、U-Boot…）

`serial`/`serial_bridge`/`ssh` 都靠 bash 哨兵机制（`__rc=$?; echo MARKER:$__rc`）
拿退出码；但 **RT-Thread msh / FinSH、U-Boot** 这类 shell 没有 `$?`、没有分号
语法，套哨兵必然超时。`prompt`（本地串口）和 `prompt_bridge`（经 serial-bridge
网关）为它们而生：

- **命令边界 = 提示符重新出现**：正则匹配，**每设备可配** `prompt:`。默认匹配
  RT-Thread msh（含 `cd` 后变化的路径，如 `msh /mnt>`）；U-Boot 设 `prompt: "=> "`。
- **退出码 = 启发式**（这类 shell 无真实返回码）：输出命中 `error_pattern:`
  （默认识别 `command not found`）→ `127`，否则 `0`。想把更多失败判为非零就改
  `error_pattern`。
- 输出会自动剥离 ANSI 颜色码、回显的命令行与尾部提示符；`cd` 等状态照常跨调用保留。

```bash
./rcmd exec msh_board "version"     # → RT-Thread banner，退出码 0
./rcmd exec msh_board "foobar"      # → "foobar: command not found."，退出码 127
./rcmd exec msh_board "cd /mnt"; ./rcmd exec msh_board "pwd"   # → /mnt（有状态）
```

---

[English README](README.en.md)
