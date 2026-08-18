#!/bin/sh
# J36 小核音频长挂测：录音 + 3A + 循环播放（case 10）
# 每轮跑 ROUND_SEC 秒，退出即重启下一轮；异常/过快退出会在日志里标红记录。
# 总时长约 2 天后自动停止。
#
# 断点续挂：若日志里已有 SOAK START 行，沿用其起始时间续算剩余时长（重启后由
#   本脚本重启时不断点）。录音目录每轮重新探测：SD 在则落 SD（vfat 58G，覆盖写），
#   SD 不在则降级 /tmp（tmpfs，掉电即丢，挂测不依赖录音内容）。

BIN=/mnt/data/sample_audio
WAV=/usr/share/digit_broadcast.wav
LOG=/mnt/data/soak_audio.log
SD_DIR=/mnt/sd
FALLBACK_DIR=/tmp
ROUND_SEC=3600            # 单轮时长（秒）
TOTAL_SEC=172800         # 总挂测时长（秒）= 2 天

# 断点续挂：从日志恢复原始起始 epoch
start_epoch=$(grep '^==== SOAK START' "$LOG" 2>/dev/null | head -1 | sed 's/.*start_epoch=\([0-9]*\).*/\1/')
# 旧格式日志无 start_epoch 字段：按其记录的起始时间重新解析
if [ -z "$start_epoch" ]; then
    _old=$(grep '^==== SOAK START' "$LOG" 2>/dev/null | head -1 | sed 's/^==== SOAK START //; s/ total=.*//; s/ start_epoch=.*//')
    [ -n "$_old" ] && start_epoch=$(date -d "$_old" +%s 2>/dev/null)
fi
# 轮次编号续接旧日志里最大的 round 号
round=$(grep -o 'ROUND [0-9]* START' "$LOG" 2>/dev/null | tail -1 | awk '{print $2}')
[ -z "$round" ] && round=0
if [ -n "$start_epoch" ]; then
    echo "==== SOAK RESUME $(date) resume_from_epoch=$start_epoch round_base=$round ====" >> "$LOG"
else
    start_epoch=$(date +%s)
    round=0
    echo "==== SOAK START $(date) start_epoch=$start_epoch total=${TOTAL_SEC}s round=${ROUND_SEC}s ====" >> "$LOG"
fi

while :; do
    now=$(date +%s)
    elapsed=$((now - start_epoch))
    [ "$elapsed" -ge "$TOTAL_SEC" ] && break

    round=$((round + 1))
    echo "---- ROUND $round START $(date) elapsed=${elapsed}s ----" >> "$LOG"
    rstart=$(date +%s)

    # 录音目录每轮探测：源码 sample_record.raw 写死相对路径，cwd 决定落点
    if [ -b /dev/mmcblk0p1 ] && mount | grep -q "on $SD_DIR "; then
        RECDIR=$SD_DIR
    elif mount | grep -q "on $SD_DIR "; then
        RECDIR=$SD_DIR
    else
        RECDIR=$FALLBACK_DIR
        # /mnt/sd 若被误当 rootfs 空目录写入（无卡时），清理避免脏数据
        :
    fi
    echo "[recdir] $RECDIR $(date)" >> "$LOG"

    ( cd "$RECDIR" && "$BIN" 10 --list -r 8000 -R 8000 -c 2 -p 320 -C 0 -V 1 -F "$WAV" -T "$ROUND_SEC" ) >> "$LOG" 2>&1
    rc=$?

    rend=$(date +%s)
    dur=$((rend - rstart))
    echo "---- ROUND $round END $(date) rc=$rc dur=${dur}s ----" >> "$LOG"

    # 过快退出（远小于预期）或非 0 退出 => 疑似复现问题，突出记录
    if [ "$rc" -ne 0 ] || [ "$dur" -lt $((ROUND_SEC - 120)) ]; then
        echo "!!!! ABNORMAL round=$round rc=$rc dur=${dur}s (expected ~${ROUND_SEC}s) $(date) !!!!" >> "$LOG"
    fi
    sleep 2
done

echo "==== SOAK DONE $(date) rounds=$round ====" >> "$LOG"
