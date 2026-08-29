#!/usr/bin/env bash
set -euo pipefail

MODE=${1:-}
if [[ "$MODE" != "weekday" && "$MODE" != "weekend" ]]; then
  echo "usage: $0 weekday|weekend" >&2
  exit 2
fi

REPOSITORY=/mnt/g/share/projects/arxiv-papers-daily
NOTES_ROOT=/mnt/g/share/papers
VENV_PATH=/home/zyf/.cache/arxiv-papers-daily/venv
VLLM_BASE_URL=http://127.0.0.1:8000/v1
LOCK_PATH=/run/user/$(id -u)/arxiv-paper-summary.lock
RESULT_DIR=/run/user/$(id -u)/arxiv-paper-summary
STARTED_MODEL=0

mkdir -p "$RESULT_DIR"
exec 9>"$LOCK_PATH"
if ! flock -n 9; then
  echo "Another local paper-summary process already holds $LOCK_PATH" >&2
  exit 75
fi

cd "$REPOSITORY"

stop_model() {
  if [[ "$STARTED_MODEL" == "1" ]]; then
    sudo -n /usr/bin/systemctl stop vllm-paper.service || true
  fi
}
trap stop_model EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ensure_clean_pull() {
  if [[ $(git branch --show-current) != "main" ]]; then
    echo "Expected the shared checkout to be on main" >&2
    exit 1
  fi
  if ! git diff --quiet || ! git diff --cached --quiet || \
     [[ -n $(git ls-files --others --exclude-standard) ]]; then
    echo "Shared checkout is not clean; refusing to publish over local work" >&2
    git status --short
    exit 1
  fi
  git pull --ff-only origin main
}

start_model() {
  if ! /usr/bin/systemctl is-active --quiet vllm-paper.service; then
    sudo -n /usr/bin/systemctl start vllm-paper.service
    STARTED_MODEL=1
  fi
  for _ in $(seq 1 60); do
    if curl --fail --silent --show-error --max-time 10 \
      "$VLLM_BASE_URL/models" >/dev/null; then
      return
    fi
    sleep 5
  done
  echo "vLLM did not become ready within five minutes" >&2
  exit 1
}

prepare_python() {
  if [[ ! -x "$VENV_PATH/bin/python" ]]; then
    /home/zyf/softwares/miniforge3/envs/vllm/bin/python -m venv "$VENV_PATH"
    "$VENV_PATH/bin/python" -m pip install -r requirements.txt
  fi
  "$VENV_PATH/bin/python" -c \
    "import arxiv, bleach, markdown, pymupdf, requests, yaml"
}

publish_checkpoint() {
  ensure_clean_pull
  PAPER_NOTES_ROOT="$NOTES_ROOT" SUMMARY_ENABLED=1 \
    "$VENV_PATH/bin/python" daily_arxiv.py --publish-only
  git config user.name zyf515730395
  git config user.email zhangyufan.aiesec@gmail.com
  git add docs/index.html
  git add -A docs/notes
  if git diff --cached --quiet; then
    echo "No summary page changes at this checkpoint"
    return
  fi
  git diff --cached --check
  git commit -m "Local Summary Update Arxiv Papers"
  git push origin main
}

prepare_python
ensure_clean_pull
start_model

if [[ "$MODE" == "weekday" ]]; then
  RESULT_PATH="$RESULT_DIR/weekday-result.json"
  rm -f "$RESULT_PATH"
  PAPER_NOTES_ROOT="$NOTES_ROOT" SUMMARY_ENABLED=1 \
    VLLM_BASE_URL="$VLLM_BASE_URL" \
    "$VENV_PATH/bin/python" daily_arxiv.py \
      --summaries-only --new-only --no-publish --result-json "$RESULT_PATH"
  publish_checkpoint
  exit 0
fi

CUTOFF_EPOCH=$(TZ=Asia/Shanghai "$VENV_PATH/bin/python" - <<'PY'
import datetime as dt
from zoneinfo import ZoneInfo

zone = ZoneInfo("Asia/Shanghai")
now = dt.datetime.now(zone)
days_until_sunday = (6 - now.weekday()) % 7
cutoff_date = now.date() + dt.timedelta(days=days_until_sunday)
cutoff = dt.datetime.combine(cutoff_date, dt.time(23, 30), zone)
print(int(cutoff.timestamp()))
PY
)

while true; do
  NOW_EPOCH=$(date +%s)
  REMAINING_SECONDS=$((CUTOFF_EPOCH - NOW_EPOCH))
  if (( REMAINING_SECONDS <= 600 )); then
    echo "Weekend cutoff is too close to start another paper"
    break
  fi
  BUDGET_MINUTES=$(( (REMAINING_SECONDS - 300) / 60 ))
  if (( BUDGET_MINUTES > 150 )); then
    BUDGET_MINUTES=150
  fi
  if (( BUDGET_MINUTES < 1 )); then
    break
  fi

  ensure_clean_pull
  RESULT_PATH="$RESULT_DIR/weekend-result.json"
  rm -f "$RESULT_PATH"
  PAPER_NOTES_ROOT="$NOTES_ROOT" SUMMARY_ENABLED=1 \
    SUMMARY_BACKFILL_LIMIT=10 \
    SUMMARY_BACKFILL_TIME_BUDGET_MINUTES="$BUDGET_MINUTES" \
    SUMMARY_HISTORY_METADATA_LOOKUP=0 \
    VLLM_BASE_URL="$VLLM_BASE_URL" \
    "$VENV_PATH/bin/python" daily_arxiv.py \
      --summaries-only --backfill-history --no-publish \
      --result-json "$RESULT_PATH"
  publish_checkpoint

  read -r BACKFILL_COMPLETE STOP_REASON < <(
    "$VENV_PATH/bin/python" - "$RESULT_PATH" <<'PY'
import json
import sys

result = json.load(open(sys.argv[1], encoding="utf-8"))
print(str(bool(result.get("backfill_complete"))).lower(), result.get("stop_reason", ""))
PY
  )
  if [[ "$BACKFILL_COMPLETE" == "true" ]]; then
    echo "Historical summary backfill is complete"
    break
  fi
  if [[ "$STOP_REASON" == "model-unavailable" || "$STOP_REASON" == "deferred-failures" ]]; then
    echo "Backfill paused because stop_reason=$STOP_REASON" >&2
    exit 1
  fi
done

publish_checkpoint
