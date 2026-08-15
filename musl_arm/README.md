# musl_arm — 板端内网穿透二进制

面向 armv7l + musl（ld-musl-armhf，硬浮点）的嵌入式设备，用于反向隧道（内网穿透），
让内网/NAT 后的板子主动连到公网服务器，你从别处通过 `公网IP:端口` 反向访问板子的
ssh(22)/telnet(23) 等任意 TCP 服务。

两个二进制都是**静态链接**（不依赖板上 libc，musl/glibc 通吃），已实测在 CV184x
(armv7l, ld-musl-armhf, kernel 5.10) 板端正常运行。

## 文件

| 文件 | 体积 | 说明 |
|---|---|---|
| `frpc` | 14M | frp 客户端 (Go, v0.71.0)。服务端配套 frps，自带 Web dashboard 可列出在线设备。 |
| `rathole` | 2.4M | rathole 客户端/服务端 (Rust, v0.5.0)。同一个二进制既可作 client 也可作 server；体积小 5 倍，无 dashboard。 |

> 两者都是「转发任意 TCP 字节流」，不解析应用协议，所以 ssh / telnet / http / 私有 TCP 口都能转发，
> 客户端访问方式和直连一样，只是换服务器 IP + 端口。

## 选型

- **要现成的「列出在线设备」界面** → frpc（服务端 frps 有 Web dashboard / API）。
- **在意体积、内存、长期驻留** → rathole（2.4M，Rust 无 GC）；「列出在线设备」需在服务端用
  `ss -tnp | grep <映射端口>` 之类脚本自行判断。

## 配置模板（开箱即用）

`conf-templates/` 下有可直接改用的模板，把 `CHANGE_ME_*` 替换成你的实际值即可：

```
conf-templates/
├── rathole/
│   ├── server.toml         # 服务端，一份管所有设备
│   └── client-devA.toml    # 板端，每台设备一份（复制改名）
└── frp/
    ├── frps.toml           # 服务端（含 dashboard，列在线设备）
    └── frpc-devA.toml      # 板端，每台设备一份（复制改名）
```

下面是要点摘录，完整注释见模板文件本身。

## rathole 用法（ssh + telnet 双端口示例）

服务端（公网服务器）`server.toml`：
```toml
[server]
bind_addr = "0.0.0.0:2333"        # 设备连进来的控制端口
default_token = "改成复杂密钥"

[server.services.devA_ssh]
bind_addr = "0.0.0.0:10022"        # 公网:10022 -> 设备A 的 ssh

[server.services.devA_telnet]
bind_addr = "0.0.0.0:10123"        # 公网:10123 -> 设备A 的 telnet
```
板端（每台设备一份，改 service 名与 remote_addr）`client.toml`：
```toml
[client]
remote_addr = "公网IP:2333"
default_token = "改成复杂密钥"

[client.services.devA_ssh]
local_addr = "127.0.0.1:22"

[client.services.devA_telnet]
local_addr = "127.0.0.1:23"
```
启动：
- 服务端 `rathole server.toml`（或 `rathole -s server.toml`）
- 板端   `rathole client.toml`（或 `rathole -c client.toml`），断线自动指数退避重连
访问：`ssh -p 10022 root@公网IP` / `telnet 公网IP 10123`

## frpc 用法（配套服务端 frps）

服务端 `frps.toml`：
```toml
bindPort = 7000
webServer.addr = "0.0.0.0"
webServer.port = 7500              # 浏览器开 http://公网IP:7500 看在线设备
webServer.user = "admin"
webServer.password = "改密码"
auth.token = "改密钥"
```
板端 `frpc.toml`：
```toml
serverAddr = "公网IP"
serverPort = 7000
auth.token = "改密钥"

[[proxies]]
name = "devA-ssh"                  # 每台设备 name 唯一
type = "tcp"
localIP = "127.0.0.1"
localPort = 22
remotePort = 10022
```
启动：服务端 `frps -c frps.toml`，板端 `frpc -c frpc.toml`。

## 服务端安装 (公网服务器)

frp / rathole 都没进 apt/yum 官方源，标准做法是下 GitHub 对应架构的预编译二进制，下载即用。
下面按服务器架构给命令 (服务器多为 x86_64，先 `uname -m` 确认；arm64 服务器把 amd64/x86_64 换成 arm64/aarch64)。

> 注意: 本目录的 `frpc`/`rathole` 是 **arm-musl 板端版**，服务器 (x86_64) 用不了，服务端要下服务器架构对应的包。

### 方式一: rathole 作服务端 (同一个二进制，体积小)
```bash
# 版本号换成最新 release
wget https://github.com/rapiz1/rathole/releases/download/v0.5.0/rathole-x86_64-unknown-linux-gnu.zip
unzip rathole-x86_64-unknown-linux-gnu.zip && sudo mv rathole /usr/local/bin/
rathole server.toml            # 用 conf-templates/rathole/server.toml
```

### 方式二: frps 作服务端 (自带 dashboard 列在线设备)
```bash
wget https://github.com/fatedier/frp/releases/download/v0.71.0/frp_0.71.0_linux_amd64.tar.gz
tar xf frp_0.71.0_linux_amd64.tar.gz
sudo cp frp_0.71.0_linux_amd64/frps /usr/local/bin/     # 服务端只用 frps
frps -c frps.toml              # 用 conf-templates/frp/frps.toml
```
frp 也有 Docker 镜像 `snowdreamtech/frps` 可选。

### 开机自启 (systemd，二选一)
`/etc/systemd/system/tunnel.service`:
```ini
[Unit]
Description=reverse tunnel server
After=network.target

[Service]
# rathole:  ExecStart=/usr/local/bin/rathole /etc/tunnel/server.toml
# frps:     ExecStart=/usr/local/bin/frps -c /etc/tunnel/frps.toml
ExecStart=/usr/local/bin/rathole /etc/tunnel/server.toml
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now tunnel
```

### 防火墙 / 云安全组
放行: 控制端口 (rathole 2333 / frp 7000)、各映射端口 (10022,10122,...)、frp dashboard(7500)。
```bash
# ufw 示例
sudo ufw allow 2333/tcp
sudo ufw allow 10022:10123/tcp
```
云服务器还要在**控制台安全组**里放行这些端口。

## 安全提醒

- 映射端口暴露在公网，务必设强 token/密码。
- telnet 明文，尽量只暴露 ssh；非要 telnet，考虑服务端 `bind_addr` 绑 127.0.0.1 再本地跳板。

## 重新编译

frpc（Go，无需交叉工具链，CGO_ENABLED=0 静态）：
```bash
git clone --depth 1 https://github.com/fatedier/frp && cd frp
GOOS=linux GOARCH=arm GOARM=7 CGO_ENABLED=0 \
  go build -tags noweb -ldflags "-s -w" -o frpc ./cmd/frpc
# -tags noweb 跳过内嵌前端 dist（否则报 "pattern dist: no matching files found"）
```

rathole（Rust，用 armv7 musl target + host-tools 的 musl gcc 作链接器）：
```bash
rustup target add armv7-unknown-linux-musleabihf
git clone --depth 1 https://github.com/rapiz1/rathole && cd rathole
mv rust-toolchain rust-toolchain.bak    # 仓库锁定 1.71.0，用当前 stable 覆盖
export PATH=<host-tools>/arm-none-linux-musleabihf/bin:$PATH
export CARGO_TARGET_ARMV7_UNKNOWN_LINUX_MUSLEABIHF_LINKER=arm-none-linux-musleabihf-gcc
cargo build --profile minimal --target armv7-unknown-linux-musleabihf \
  --no-default-features --features "server,client,noise,hot-reload"
# 去掉 native-tls/websocket-tls 以避免 OpenSSL 交叉编译；noise 已提供隧道加密
# 产物: target/armv7-unknown-linux-musleabihf/minimal/rathole
```
