#!/usr/bin/env bash
set -u

if [ "${FUSION_REAL:-}" != "1" ]; then
  echo "real smoke is opt-in because it consumes provider quota: FUSION_REAL=1 make dogfood-real" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
WORKSPACE="$TMP/workspace"
mkdir -p "$WORKSPACE"
printf '%s\n' '# real Fusion smoke fixture' > "$WORKSPACE/README.md"

overall=0
for agent in claude codex; do
  stderr_path="$TMP/$agent.stderr"
  set +e
  output="$(PYTHONDONTWRITEBYTECODE=1 "$ROOT/fusion" --workspace "$WORKSPACE" --json delegate --agent "$agent" --read-only --fresh --role real-smoke --success 'return a structured handoff' 'Read README.md and return the required handoff without editing files.' 2>"$stderr_path")"
  rc=$?
  set -e
  printf '%s\n' "=== $agent ==="
  printf '%s\n' "$output" | jq '{status, agent, model, duration_ms, usage, summary, blockers}' 2>/dev/null || printf '%s\n' "$output"
  if [ "$rc" -eq 0 ] && [ "$(printf '%s\n' "$output" | jq -r '.status // "unknown"' 2>/dev/null)" = "success" ]; then
    printf '%s\n' "$agent smoke passed"
    continue
  fi
  blockers="$(printf '%s\n' "$output" | jq -r '(.blockers // []) | join(" ")' 2>/dev/null || true)"
  case "$blockers" in
    *usage*|*limit*|*session*|*authentication*|*login*|*credential*)
      printf '%s\n' "$agent reached the CLI but the provider gate blocked the turn"
      [ "$overall" -eq 0 ] && overall=2 ;;
    *)
      printf '%s\n' "$agent smoke failed; see $stderr_path" >&2
      overall=1 ;;
  esac
done

printf '%s\n' "trace:"
PYTHONDONTWRITEBYTECODE=1 "$ROOT/fusion" --workspace "$WORKSPACE" trace --limit 10
printf '%s\n' "usage:"
PYTHONDONTWRITEBYTECODE=1 "$ROOT/fusion" --workspace "$WORKSPACE" usage --limit 10
exit "$overall"
