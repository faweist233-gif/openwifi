#!/bin/bash
# 只重下损坏的连续字节区间，然后拼出一个新镜像并校验
set -u

URL="https://users.ugent.be/~xjiao/openwifi-1.5.0-shahecheng.img.xz"
SRC="$HOME/Downloads/openwifi-1.5.0-shahecheng.img.xz"
NEW="$HOME/Downloads/openwifi-1.5.0-fixed.img.xz"
WORK="$HOME/Downloads/.ow_fix"
PARTS="$WORK/parts"
RANGE="$WORK/range.bin"

START=1339116640
LEN=646857548
N=16
ROUNDS=6
MAXTIME=1800

mkdir -p "$PARTS"

echo "=== 目标区间 ==="
echo "起点 ${START}  长度 ${LEN}  终点 $((START+LEN-1))"
echo

# ---------- 1. 分段并发下载 ----------
seg_bounds() {  # $1=段号 -> "abs_start abs_end want"
  local i=$1
  local a=$(( START + LEN * i / N ))
  local b=$(( START + LEN * (i + 1) / N - 1 ))
  echo "$a $b $(( b - a + 1 ))"
}

get_part() {
  local i=$1 a=$2 b=$3 want=$4
  local p="$PARTS/p$i" cur aa r
  for r in $(seq 1 $ROUNDS); do
    cur=$(stat -f %z "$p" 2>/dev/null || echo 0)
    [ "$cur" -ge "$want" ] && return 0
    aa=$(( a + cur ))
    curl -sS --fail --max-time $MAXTIME -r "$aa-$b" "$URL" >> "$p" || true
  done
  cur=$(stat -f %z "$p" 2>/dev/null || echo 0)
  if [ "$cur" -ge "$want" ]; then return 0; fi
  echo "SEG_FAIL $i: $cur / $want"
  return 1
}

echo "--- 1/3 分段下载 (${N} 路) ---"
pids=""
for i in $(seq 0 $((N - 1))); do
  set -- $(seg_bounds "$i")
  get_part "$i" "$1" "$2" "$3" &
  pids="$pids $!"
done

fail=0
for pid in $pids; do wait "$pid" || fail=1; done
if [ "$fail" != "0" ]; then
  echo "有段未完成，重跑本脚本即可续传"
  exit 1
fi

# ---------- 2. 拼装区间文件 ----------
rm -f "$RANGE"
for i in $(seq 0 $((N - 1))); do
  cat "$PARTS/p$i" >> "$RANGE"
done
got=$(stat -f %z "$RANGE")
echo "区间文件大小: ${got} / ${LEN}"
if [ "$got" != "$LEN" ]; then
  echo "长度不符，中止"
  exit 1
fi

# ---------- 3. 拼出新镜像 ----------
echo
echo "--- 3/3 拼装新镜像（原文件保留不动）---"
rm -f "$NEW"
head -c "$START" "$SRC" > "$NEW" || exit 1
cat "$RANGE" >> "$NEW" || exit 1
tail -c +$(( START + LEN + 1 )) "$SRC" >> "$NEW" || exit 1

s=$(stat -f %z "$NEW")
echo "新镜像大小: ${s}  (期望 3318391904)"
if [ "$s" != "3318391904" ]; then
  echo "大小不符，中止"
  exit 1
fi

echo
echo "=== 完成，待校验: $NEW ==="
