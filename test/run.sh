#!/usr/bin/env bash
# shellcheck disable=SC2016
set -uo pipefail

cd "$(dirname "$0")" || exit 1
ROOT="$(cd .. && pwd)"
FIX="$PWD/fixtures"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0

strip_ansi() { sed $'s/\x1b\\[[0-9;]*m//g'; }

t() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    PASS=$((PASS + 1))
    printf '  ok  %s\n' "$name"
  else
    FAIL=$((FAIL + 1))
    printf 'FAIL  %s\n  expected: %s\n  actual:   %s\n' "$name" "$expected" "$actual"
  fi
}

t_contains() {
  local name="$1" needle="$2" haystack="$3"
  case "$haystack" in
    *"$needle"*)
      PASS=$((PASS + 1))
      printf '  ok  %s\n' "$name" ;;
    *)
      FAIL=$((FAIL + 1))
      printf 'FAIL  %s\n  missing:  %s\n  in:       %s\n' "$name" "$needle" "$haystack" ;;
  esac
}

echo "== hud =="
export MODELS_CACHE="$FIX/models.json"
export ORC_SESSION_CACHE="$TMP/hud-cache"
export ORC_LAST_LAUNCH="$TMP/hud-nolaunch"
rm -rf "$ORC_SESSION_CACHE"
mkdir -p "$ORC_SESSION_CACHE"

hud() { "$ROOT/hud.sh" | strip_ansi; }
payload() { jq -n --arg tp "$FIX/$1" "$2"; }

t "paid transcript, ctx estimated from transcript" \
  'paid│↑ 20.0k ↓ 300│cache 45%│ctx ░░░░░░░░░░░░ 7%│$0.0149' \
  "$(payload paid.jsonl '{transcript_path:$tp,model:{id:"test/paid",display_name:"paid"},context_window:null}' | hud)"

t "used_percentage wins over estimates" \
  'paid│↑ 20.0k ↓ 300│cache 45%│ctx ▓▓▓▓▓░░░░░░░ 42%│$0.0149' \
  "$(payload paid.jsonl '{transcript_path:$tp,model:{id:"test/paid",display_name:"paid"},context_window:{used_percentage:42.7,context_window_size:128000}}' | hud)"

t "current_usage object computes ctx pct" \
  'paid│↑ 20.0k ↓ 300│cache 45%│ctx ▓▓▓▓▓▓░░░░░░ 50%│$0.0149' \
  "$(payload paid.jsonl '{transcript_path:$tp,model:{id:"test/paid",display_name:"paid"},context_window:{context_window_size:128000,current_usage:{input_tokens:50000,cache_read_input_tokens:14000,cache_creation:{ephemeral_5m_input_tokens:0,ephemeral_1h_input_tokens:0}}}}' | hud)"

t "free model shows FREE" \
  'free│↑ 2.0k ↓ 100│cache 0%│ctx ░░░░░░░░░░░░ 3%│FREE' \
  "$(payload free.jsonl '{transcript_path:$tp,model:{id:"test/free",display_name:"free"},context_window:null}' | hud)"

t "unknown model: name from transcript, cost unknown, synthetic filtered" \
  'test/mystery│↑ 1.0k ↓ 10│cache 0%│$—' \
  "$(payload unknown.jsonl '{transcript_path:$tp,context_window:null}' | hud)"

t "empty stdin renders placeholder" "—" "$(printf '' | hud)"

t "missing transcript renders zeros without failing" \
  '?│↑ 0 ↓ 0│$0.0000' \
  "$(jq -n '{transcript_path:"/nonexistent/x.jsonl",context_window:null}' | hud)"

t "subagent spend is a sibling segment, parent tokens unchanged" \
  'paid│↑ 20.0k ↓ 300│cache 45%│ctx ░░░░░░░░░░░░ 7%│$0.0149│+$0.0011 agents' \
  "$(payload with-agents.jsonl '{transcript_path:$tp,model:{id:"test/paid",display_name:"paid"},context_window:null}' | hud)"

RESUME_DIR="$TMP/hud-resume"
mkdir -p "$RESUME_DIR" "$TMP/hud-resume-cache"
cp "$FIX/paid.jsonl" "$RESUME_DIR/s.jsonl"
printf '1\n' > "$TMP/hud-launch"
hud_resume() {
  ORC_LAST_LAUNCH="$TMP/hud-launch" ORC_SESSION_CACHE="$TMP/hud-resume-cache" hud
}
payload_resume() {
  jq -n --arg tp "$RESUME_DIR/s.jsonl" '{transcript_path:$tp,model:{id:"test/paid",display_name:"paid"},context_window:null}'
}
FIRST="$(payload_resume | hud_resume)"
t "resume baseline on first join matches file cost" \
  'paid│↑ 20.0k ↓ 300│cache 45%│ctx ░░░░░░░░░░░░ 7%│$0.0149' \
  "$FIRST"
printf '%s\n' '{"type":"assistant","timestamp":"2026-08-20T12:00:00.000Z","message":{"id":"msg_new","model":"test/paid","usage":{"input_tokens":1000,"output_tokens":100,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}' >> "$RESUME_DIR/s.jsonl"
SECOND="$(payload_resume | hud_resume)"
t "resume split shows this-join vs file" \
  'paid│↑ 21.0k ↓ 400│cache 43%│ctx ░░░░░░░░░░░░ 0%│$0.0012 this $0.0161 file' \
  "$SECOND"

unset MODELS_CACHE
unset ORC_SESSION_CACHE
unset ORC_LAST_LAUNCH

echo "== stats =="
export ORC_HOME="$TMP/stats-home"
mkdir -p "$ORC_HOME/claude-state/projects/-proj-a" "$ORC_HOME/claude-state/projects/-proj-b"
cp "$FIX/models.json" "$ORC_HOME/models.json"
cp "$FIX/paid.jsonl" "$ORC_HOME/claude-state/projects/-proj-a/s1.jsonl"
cp "$FIX/free.jsonl" "$ORC_HOME/claude-state/projects/-proj-b/s2.jsonl"
cp "$FIX/unknown.jsonl" "$ORC_HOME/claude-state/projects/-proj-b/s3.jsonl"

S="$("$ROOT/orc" stats --json 2>/dev/null)"
t "stats: message count" "4" "$(printf '%s' "$S" | jq -r .messages)"
t "stats: total input tokens" "4500" "$(printf '%s' "$S" | jq -r .total.i)"
t "stats: total output tokens" "410" "$(printf '%s' "$S" | jq -r .total.o)"
t "stats: total cache reads" "9000" "$(printf '%s' "$S" | jq -r .total.r)"
t "stats: total cache writes" "9500" "$(printf '%s' "$S" | jq -r .total.w)"
t "stats: total cost" "0.014875" "$(printf '%s' "$S" | jq -r .total.cost)"
t "stats: unpriced count" "1" "$(printf '%s' "$S" | jq -r .total.unpriced)"
t "stats: models grouped" "3" "$(printf '%s' "$S" | jq -r '.by_model | length')"
t "stats: projects grouped" "2" "$(printf '%s' "$S" | jq -r '.by_project | length')"
t "stats: days grouped" "2" "$(printf '%s' "$S" | jq -r '.by_day | length')"
t "stats: paid model cost" "0.014875" "$(printf '%s' "$S" | jq -r '.by_model[] | select(.key == "test/paid") | .cost')"
t "stats: project attribution" "1500" "$(printf '%s' "$S" | jq -r '.by_project[] | select(.key == "-proj-a") | .i')"

TABLE="$("$ROOT/orc" stats 2>/dev/null | strip_ansi)"
t_contains "stats table: header" "MODEL" "$TABLE"
t_contains "stats table: total row" "TOTAL" "$TABLE"
t_contains "stats table: paid row priced" '$0.0149' "$TABLE"
t_contains "stats table: unpriced marker" '+?' "$TABLE"

TABLE_DAY="$("$ROOT/orc" stats --by day 2>/dev/null | strip_ansi)"
t_contains "stats --by day: day key" "2026-08-20" "$TABLE_DAY"

EMPTY_HOME="$TMP/empty-home"
mkdir -p "$EMPTY_HOME"
cp "$FIX/models.json" "$EMPTY_HOME/models.json"
OUT="$(ORC_HOME="$EMPTY_HOME" "$ROOT/orc" stats 2>&1 | strip_ansi)"
t_contains "stats: no transcripts message" "no transcripts" "$OUT"

echo "== models: tool support =="
export ORC_HOME="$TMP/tools-home"
mkdir -p "$ORC_HOME"
cp "$FIX/models.json" "$ORC_HOME/models.json"

ROWS="$("$ROOT/orc" models 2>/dev/null | strip_ansi)"
NOTOOLS_COUNT="$(printf '%s\n' "$ROWS" | grep -c 'NO TOOLS' || true)"
t "models: no-tools rows flagged" "2" "$NOTOOLS_COUNT"
TOOLS_ONLY="$("$ROOT/orc" models --tools 2>/dev/null | strip_ansi)"
t_contains "models --tools keeps tool-capable model" "test/paid" "$TOOLS_ONLY"
case "$TOOLS_ONLY" in
  *test/notools*|*test/free*)
    FAIL=$((FAIL + 1))
    printf 'FAIL  models --tools lists a no-tools model\n'
    printf '  got:  %s\n' "$TOOLS_ONLY" ;;
  *)
    PASS=$((PASS + 1))
    printf '  ok  models --tools drops no-tools models\n' ;;
esac

FILTER_HOME="$TMP/free-tools-home"
mkdir -p "$FILTER_HOME"
jq '(.data[] | select(.id == "test/free").supported_parameters) = ["tools"]' "$FIX/models.json" > "$FILTER_HOME/models.json"
FREE_TOOLS="$(ORC_HOME="$FILTER_HOME" "$ROOT/orc" models --free --tools 2>/dev/null | strip_ansi)"
t_contains "models --free --tools keeps free tool model" "test/free" "$FREE_TOOLS"
case "$FREE_TOOLS" in
  *test/paid*|*test/notools*)
    FAIL=$((FAIL + 1))
    printf 'FAIL  models --free --tools lists a paid or no-tools model\n'
    printf '  got:  %s\n' "$FREE_TOOLS" ;;
  *)
    PASS=$((PASS + 1))
    printf '  ok  models --free --tools filters both dimensions\n' ;;
esac

echo "== profiles + project config =="
export ORC_HOME="$TMP/prof-home"
mkdir -p "$ORC_HOME"
cp "$FIX/models.json" "$ORC_HOME/models.json"
export OPENROUTER_API_KEY="sk-or-test-dummy"

"$ROOT/orc" model --set test/paid >/dev/null 2>&1
t "model --set writes config" "test/paid" "$(jq -r .model "$ORC_HOME/config.json")"

"$ROOT/orc" model --set nope/nope >/dev/null 2>&1
t "model --set rejects unknown model" "1" "$?"

"$ROOT/orc" small --set test/free >/dev/null 2>&1
t "small --set writes config" "test/free" "$(jq -r .small_model "$ORC_HOME/config.json")"

"$ROOT/orc" save work >/dev/null 2>&1
t "save snapshots model into profile" "test/paid" "$(jq -r .profiles.work.model "$ORC_HOME/config.json")"
t "save snapshots small model into profile" "test/free" "$(jq -r .profiles.work.small_model "$ORC_HOME/config.json")"

"$ROOT/orc" save 'bad name!' >/dev/null 2>&1
t "save rejects invalid profile name" "1" "$?"

LIST="$("$ROOT/orc" profiles 2>/dev/null | strip_ansi)"
t_contains "profiles lists saved profile" "@work" "$LIST"

PROJ="$TMP/proj"
mkdir -p "$PROJ"
printf '{"model":"test/free"}\n' > "$PROJ/.orc.json"
ENVOUT="$(cd "$PROJ" && "$ROOT/orc" env 2>/dev/null)"
t_contains ".orc.json model overrides global config" 'ANTHROPIC_MODEL="test/free"' "$ENVOUT"
t_contains "env exports OpenRouter base URL" 'export ANTHROPIC_BASE_URL="https://openrouter.ai/api"' "$ENVOUT"

ENVOUT="$(cd "$PROJ" && ORC_PROFILE=work "$ROOT/orc" env 2>/dev/null)"
t_contains "ORC_PROFILE beats .orc.json" 'ANTHROPIC_MODEL="test/paid"' "$ENVOUT"

printf '{"profile":"work"}\n' > "$PROJ/.orc.json"
"$ROOT/orc" model --set test/free >/dev/null 2>&1
ENVOUT="$(cd "$PROJ" && "$ROOT/orc" env 2>/dev/null)"
t_contains ".orc.json profile reference resolves" 'ANTHROPIC_MODEL="test/paid"' "$ENVOUT"

"$ROOT/orc" profiles rm work >/dev/null 2>&1
t "profiles rm deletes profile" "null" "$(jq -r '.profiles.work' "$ORC_HOME/config.json")"

echo "== status + resolved save + env + fit =="
export ORC_HOME="$TMP/status-home"
mkdir -p "$ORC_HOME"
cp "$FIX/models.json" "$ORC_HOME/models.json"
"$ROOT/orc" model --set test/paid >/dev/null 2>&1
"$ROOT/orc" small --set test/free >/dev/null 2>&1

ST="$("$ROOT/orc" status --json 2>/dev/null)"
t "status --json model" "test/paid" "$(printf '%s' "$ST" | jq -r .model)"
t "status --json source is config" "config" "$(printf '%s' "$ST" | jq -r .source.model)"
t "status --json fit untested" "UNTESTED" "$(printf '%s' "$ST" | jq -r .fit)"
t "status --json tools true" "true" "$(printf '%s' "$ST" | jq -r .tools)"

PROJ2="$TMP/proj2"
mkdir -p "$PROJ2"
printf '{"model":"test/free","mode":"plan"}\n' > "$PROJ2/.orc.json"
ST="$(cd "$PROJ2" && "$ROOT/orc" status --json 2>/dev/null)"
t "status honors .orc.json model" "test/free" "$(printf '%s' "$ST" | jq -r .model)"
t "status honors .orc.json mode" "plan" "$(printf '%s' "$ST" | jq -r .mode)"
t "status source is project" "project" "$(printf '%s' "$ST" | jq -r .source.model)"

"$ROOT/orc" save work >/dev/null 2>&1
ST="$(cd "$PROJ2" && ORC_PROFILE=work "$ROOT/orc" status --json 2>/dev/null)"
t "status ORC_PROFILE beats .orc.json" "test/paid" "$(printf '%s' "$ST" | jq -r .model)"
t "status source is profile" "profile" "$(printf '%s' "$ST" | jq -r .source.model)"

ST="$(cd "$PROJ2" && ORC_PROFILE=work "$ROOT/orc" -m test/notools status --json 2>/dev/null)"
# -m only applies to launch; status is its own command. Override via env:
ST="$(cd "$PROJ2" && ORC_MODEL_OVERRIDE=test/notools "$ROOT/orc" status --json 2>/dev/null)"
t "status ORC_MODEL_OVERRIDE wins" "test/notools" "$(printf '%s' "$ST" | jq -r .model)"
t "status override source is flag" "flag" "$(printf '%s' "$ST" | jq -r .source.model)"

(cd "$PROJ2" && "$ROOT/orc" save fromproj >/dev/null 2>&1)
t "save snapshots resolved project model" "test/free" "$(jq -r .profiles.fromproj.model "$ORC_HOME/config.json")"
t "save snapshots resolved project mode" "plan" "$(jq -r .profiles.fromproj.mode "$ORC_HOME/config.json")"

ENVOUT="$(cd "$PROJ2" && "$ROOT/orc" env 2>/dev/null)"
t_contains "env exports DEFAULT_HAIKU" 'export ANTHROPIC_DEFAULT_HAIKU_MODEL="test/free"' "$ENVOUT"
t_contains "env exports gateway discovery" 'export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1' "$ENVOUT"
t_contains "env exports context window" 'export CLAUDE_CODE_MAX_CONTEXT_TOKENS="64000"' "$ENVOUT"

ROWS="$("$ROOT/orc" models 2>/dev/null | strip_ansi)"
t_contains "models lists UNTESTED fit column" "UNTESTED" "$ROWS"
NOW="$(date +%s)"
printf '{"test/paid":{"ok":true,"http":200,"ttft":0.1,"tool_roundtrip":0.2,"error":null,"checked_at":%s}}\n' "$NOW" > "$ORC_HOME/fit.json"
FIT_ONLY="$("$ROOT/orc" models --fit 2>/dev/null | strip_ansi)"
t_contains "models --fit keeps FIT model" "test/paid" "$FIT_ONLY"
case "$FIT_ONLY" in
  *test/notools*|*test/free*)
    FAIL=$((FAIL + 1))
    printf 'FAIL  models --fit lists an untested model\n'
    printf '  got:  %s\n' "$FIT_ONLY" ;;
  *)
    PASS=$((PASS + 1))
    printf '  ok  models --fit drops untested models\n' ;;
esac
ROWS="$("$ROOT/orc" models 2>/dev/null | strip_ansi)"
t_contains "models marks cached FIT" "FIT" "$ROWS"

echo "== models: quality ranking =="
export ORC_HOME="$TMP/quality-home"
mkdir -p "$ORC_HOME"
cp "$FIX/models.json" "$ORC_HOME/models.json"
cp "$FIX/quality.json" "$ORC_HOME/quality.json"

# All 3 fixture models are in the quality cache, so the picker should
# list test/paid (score 63) first, then test/free (score 22), then
# test/notools (no score) last. test/notools has no tools either, so
# it should still appear in `orc models` (just at the bottom).
RANKED_FIRST="$(ORC_HOME="$ORC_HOME" "$ROOT/orc" models 2>/dev/null | strip_ansi | head -1 | awk '{print $1}')"
t "models: ranked by quality (paid first)" "test/paid" "$RANKED_FIRST"

# Quality order should also hold with the --tools filter, which drops
# test/free and test/notools (no `supported_parameters:["tools"]` in
# the fixture). The remaining test/paid should still be at the top.
TOOLS_RANKED="$(ORC_HOME="$ORC_HOME" "$ROOT/orc" models --tools 2>/dev/null | strip_ansi)"
TOOLS_FIRST="$(printf '%s' "$TOOLS_RANKED" | head -1 | awk '{print $1}')"
t "models --tools: top pick is the highest-quality model" "test/paid" "$TOOLS_FIRST"

# No-quality rows must still be listed (fail-soft: a missing or stale
# quality cache never removes models from the picker).
ALL_LISTED="$(ORC_HOME="$ORC_HOME" "$ROOT/orc" models 2>/dev/null | strip_ansi)"
t_contains "models: no-quality rows still listed" "test/notools" "$ALL_LISTED"

# `orc quality` should print a table; check the header is present and
# the top entry is test/paid.
QUALITY_OUT="$(ORC_HOME="$ORC_HOME" "$ROOT/orc" quality 2>/dev/null | strip_ansi)"
t_contains "quality subcommand prints header" "OR_ID" "$QUALITY_OUT"
PAID_LINE="$(printf '%s\n' "$QUALITY_OUT" | grep -n 'test/paid' | head -1 | cut -d: -f1)"
FREE_LINE="$(printf '%s\n' "$QUALITY_OUT" | grep -n 'test/free' | head -1 | cut -d: -f1)"
if [ -n "$PAID_LINE" ] && [ -n "$FREE_LINE" ] && [ "$PAID_LINE" -lt "$FREE_LINE" ]; then
  PASS=$((PASS + 1))
  printf '  ok  quality subcommand lists test/paid before test/free\n'
else
  FAIL=$((FAIL + 1))
  printf 'FAIL  quality subcommand ordering wrong\n  got:  %s\n' "$QUALITY_OUT"
fi

echo
if [ "$FAIL" -gt 0 ]; then
  printf '%d passed, %d FAILED\n' "$PASS" "$FAIL"
  exit 1
fi
printf 'all %d tests passed\n' "$PASS"
