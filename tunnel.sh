#!/bin/bash
# sc51213 — SSH 端口转发隧道管理
# 用法: ./rcmd_tunnel.sh {start|stop}
#
# 隧道配置：在下面 TUNNELS 数组中按需增删
# 格式: "<local_port>:<remote_ip>:<remote_port>"

SSH_USER_HOST="song.yu@10.80.38.25"

# ========== 隧道配置（在此添加你的设备）==========
TUNNELS=(
    # sc51213
    "12302:192.168.1.78:23"
    # a90s
    "12303:192.168.10.12:23"
    #"12304:192.168.2.100:23"
)
# ===============================================

stop_one() {
    local port="$1"
    # -R 后监听在 Server，本地查端口找不到，按进程命令行匹配
    local pid
    pid=$(pgrep -f "ssh -f -N -R ${port}:" | tr '\n' ' ')
    if [ -n "$pid" ]; then
        echo "  Stopping tunnel on port $port (PID: $pid)"
        kill $pid 2>/dev/null
        for i in 1 2 3; do
            if kill -0 $pid 2>/dev/null; then
                sleep 0.5
            else
                break
            fi
        done
        kill -9 $pid 2>/dev/null || true
        echo "  Stopped"
    else
        echo "  No tunnel running on port $port"
    fi
}

start_one() {
    local port="$1"
    local remote_ip="$2"
    local remote_port="$3"

    echo "  Starting tunnel: <server>:localhost:$port -> $remote_ip:$remote_port"
    # -R: 在【Server】监听 port，流量经隧道回 PC，由 PC 连设备
    ssh -f -N -R "${port}:${remote_ip}:${remote_port}" \
        -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
        "$SSH_USER_HOST"
    if [ $? -eq 0 ]; then
        echo "  Tunnel started: <server>:localhost:$port -> $remote_ip:$remote_port"
    else
        echo "  Failed to start tunnel on port $port (exit code: $?)" >&2
        return 1
    fi
}

stop_all() {
    echo "Stopping all tunnels..."
    for entry in "${TUNNELS[@]}"; do
        local_port="${entry%%:*}"
        stop_one "$local_port"
    done
}

start_all() {
    echo "Starting all tunnels..."
    local has_error=0
    for entry in "${TUNNELS[@]}"; do
        IFS=':' read -r port ip rport <<< "$entry"
        stop_one "$port"
        start_one "$port" "$ip" "$rport" || has_error=1
    done
    if [ "$has_error" -ne 0 ]; then
        echo "One or more tunnels failed to start" >&2
        exit 1
    fi
}

usage() {
    echo "Usage: $0 {start|stop}"
    echo ""
    echo "Tunnels configured:"
    for entry in "${TUNNELS[@]}"; do
        IFS=':' read -r port ip rport <<< "$entry"
        if pgrep -f "ssh -f -N -R ${port}:" &>/dev/null; then
            status="RUNNING"
        else
            status="STOPPED"
        fi
        echo "  [$status] localhost:$port -> $ip:$rport"
    done
} >&2

case "${1:-}" in
    start)
        start_all
        ;;
    stop)
        stop_all
        ;;
    *)
        usage
        ;;
esac
