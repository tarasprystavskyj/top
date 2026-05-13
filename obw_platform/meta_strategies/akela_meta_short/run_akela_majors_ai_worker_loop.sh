#!/usr/bin/env bash
set -euo pipefail

ROOT="/var/www/vps2.happyuser.info/top/top_1"
OUT_DIR="$ROOT/_reports/akela_meta_short/v21_majors_rank"
LOG_DIR="$OUT_DIR/ai_worker_logs"
PROMPT_BASE="$ROOT/docs/akela_majors_ai_worker_prompt.md"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
MODEL="${CLAUDE_MODEL:-haiku}"
SLEEP_SEC="${AKELA_MAJORS_AI_SLEEP_SEC:-1800}"
mkdir -p "$LOG_DIR" "$OUT_DIR"
cd "$ROOT"

while true; do
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  prompt="$LOG_DIR/prompt_${stamp}.md"
  log="$LOG_DIR/ai_worker_${stamp}.log"
  {
    cat "$PROMPT_BASE"
    printf '\n\n# Current passive majors summary\n'
    cat "$ROOT/_reports/akela_meta_short/s0_passive_orderbook_majors/summary.md" 2>/dev/null || true
    printf '\n\n# Current V21 majors rank\n'
    cat "$OUT_DIR/v21_majors_rank.md" 2>/dev/null || true
    printf '\n\n# Current UTC\n%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '\nReturn only markdown analysis plus the required output block. Do not use tools. Do not write files. Do not request permissions.\n'
  } >"$prompt"

  if command -v "$CLAUDE_BIN" >/dev/null 2>&1 && "$CLAUDE_BIN" -p "ping" >/tmp/akela_majors_claude_probe.out 2>&1; then
    set +e
    timeout 900 "$CLAUDE_BIN" --model "$MODEL" -p "$(cat "$prompt")" >"$log" 2>&1
    status=$?
    set -e
  elif command -v codex >/dev/null 2>&1; then
    set +e
    timeout 900 codex exec --sandbox read-only -C "$ROOT" -o "$log" "$(cat "$prompt")" >"$log.events" 2>&1
    status=$?
    set -e
    if [[ ! -s "$log" ]]; then
      {
        echo "Codex did not produce an output-last-message file. Event tail:"
        tail -120 "$log.events" 2>/dev/null || true
      } >"$log"
    fi
  else
    status=127
    echo "No usable AI CLI found. Claude probe failed and codex is missing." >"$log"
  fi
  {
    echo "# Akela majors AI worker latest"
    echo
    echo "Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo
    echo "Exit status: $status"
    echo
    cat "$log" 2>/dev/null || true
  } >"$OUT_DIR/ai_worker_latest.md"
  find "$LOG_DIR" -maxdepth 1 -type f -name 'ai_worker_*.log' -printf '%T@ %p\n' \
    | sort -nr | awk 'NR > 48 {print $2}' | xargs -r rm -f
  sleep "$SLEEP_SEC"
done
