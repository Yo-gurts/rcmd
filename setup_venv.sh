#!/usr/bin/env bash
# 为 rcmd 创建独立虚拟环境并安装依赖。
#
# 为什么要 venv:rcmd 的依赖(pyserial / websocket-client / pexpect)装进系统
# Python 容易和别的工具打架(例如 yoctools 钉死 ruamel.yaml==0.16.13)。装进
# 同目录的 .venv 就彼此隔离。
#
# 装好后无需额外操作:rcmd.py 启动时会自动切到这个 .venv 运行(daemon 也随之
# 使用它),所以照常 `./rcmd.py exec ...` 或用软链在 PATH 里的 `rcmd` 即可。
#
# 用法:
#   ./setup_venv.sh              # 用 python3 创建
#   PYTHON=python3.11 ./setup_venv.sh   # 指定解释器
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"
PY="${PYTHON:-python3}"

echo ">> 使用解释器: $("$PY" --version 2>&1)"
echo ">> 创建虚拟环境: $VENV"
"$PY" -m venv "$VENV"

echo ">> 升级 pip"
"$VENV/bin/pip" install --upgrade pip >/dev/null

echo ">> 安装依赖 (requirements.txt)"
"$VENV/bin/pip" install -r "$DIR/requirements.txt"

echo
echo ">> 完成。rcmd 会自动使用该虚拟环境。直接运行即可:"
echo "     $DIR/rcmd.py ls"
echo "   若已把 rcmd 软链进 PATH,照常 \`rcmd ls\` 也会走这个 venv。"
