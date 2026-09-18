#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
WORKSPACE="$TMP/workspace"
BIN="$TMP/bin"
mkdir -p "$WORKSPACE" "$BIN"
printf '%s\n' '# dogfood fixture' > "$WORKSPACE/README.md"

cat > "$BIN/claude" <<'PY'
#!/usr/bin/env python3
import json
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": "dogfood-claude",
    "result": "STATUS: success\nSUMMARY: Claude stage completed\nCHANGED: none\nTESTS: none\nBLOCKERS: none",
}))
PY

cat > "$BIN/codex" <<'PY'
#!/usr/bin/env python3
import json
print(json.dumps({"type": "thread.started", "thread_id": "dogfood-codex"}))
print(json.dumps({
    "type": "item.completed",
    "item": {
        "type": "agent_message",
        "text": "STATUS: success\nSUMMARY: Codex stage completed\nCHANGED: src/app.py\nTESTS: python -m unittest\nBLOCKERS: none",
    },
}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 8}}))
PY
chmod +x "$BIN/claude" "$BIN/codex"

cat > "$WORKSPACE/.fusion.json" <<JSON
{
  "claude": {"command": "$BIN/claude"},
  "codex": {"command": "$BIN/codex"},
  "routes": {
    "fixture-claude": {"agent": "claude", "command": "$BIN/claude"}
  },
  "ultra": {
    "max_stages": 3,
    "stages": {
      "explore": {"agent": "claude", "route": "fixture-claude", "write": false},
      "implement": {"agent": "codex", "write": true},
      "review": {"agent": "claude", "route": "fixture-claude", "write": false}
    }
  }
}
JSON

RESULT="$(PYTHONDONTWRITEBYTECODE=1 "$ROOT/fusion" --workspace "$WORKSPACE" --json ultra 'dogfood the bounded pipeline')"
printf '%s\n' "$RESULT" | jq -e '
  .schema == "fusion.ultra.v1"
  and .status == "success"
  and (.stages | length) == 3
  and .stages[0].result.agent == "claude"
  and .stages[1].result.agent == "codex"
  and (.stages[1].result.changed | index("src/app.py")) != null
  and (.artifacts.manifest | type) == "string"
' >/dev/null

STATUS="$(PYTHONDONTWRITEBYTECODE=1 "$ROOT/fusion" --workspace "$WORKSPACE" --json status --limit 10)"
printf '%s\n' "$STATUS" | jq -e 'length == 3 and all(.[]; .result.status == "success")' >/dev/null

TRACE="$(PYTHONDONTWRITEBYTECODE=1 "$ROOT/fusion" --workspace "$WORKSPACE" trace --limit 10)"
printf '%s\n' "$TRACE" | jq -e 'length == 3 and all(.[]; .schema == "fusion.trace.v1")' >/dev/null

USAGE="$(PYTHONDONTWRITEBYTECODE=1 "$ROOT/fusion" --workspace "$WORKSPACE" usage --limit 10)"
printf '%s\n' "$USAGE" | jq -e '.spans == 3 and .total.input_tokens == 12 and .total.output_tokens == 8' >/dev/null

printf 'fusion dogfood passed: subprocesses, handoffs, traces, usage, and ledger\n'
