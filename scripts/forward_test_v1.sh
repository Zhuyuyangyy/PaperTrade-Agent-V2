#!/usr/bin/env bash
# Forward Test V1 启动脚本（在项目根目录执行）
#
#   bash scripts/forward_test_v1.sh
#
# 默认：73 轮 × 1h ≈ 72 小时真实时间。可用环境变量覆盖：
#   ROOT / ROUNDS / HOURS / LOGS
set -euo pipefail

ROOT="${ROOT:-data/openalpha_forward_v1}"
LOGS="${LOGS:-logs}"
ROUNDS="${ROUNDS:-73}"
HOURS="${HOURS:-72}"

echo "== pull =="
git pull --ff-only

echo "== tests =="
python -m pytest tests -q

echo "== smoke (1 round) =="
mkdir -p "${LOGS}" "${ROOT}"
python -m research.runtime live --rounds 1 --root "${ROOT}_smoke"

echo "== launch forward test =="
nohup python -u -m research.runtime live \
  --interval-ms 3600000 \
  --rounds "${ROUNDS}" \
  --root "${ROOT}" \
  > "${LOGS}/openalpha_forward_v1.log" 2>&1 &
PID=$!
echo "${PID}" > "${ROOT}/live.pid"
echo "started pid=${PID} root=${ROOT} rounds=${ROUNDS} (~${HOURS}h)"
echo "tail:  tail -f ${LOGS}/openalpha_forward_v1.log"
echo "check: python scripts/verify_forward_run.py --root ${ROOT} --rounds ${ROUNDS} --hours ${HOURS}"