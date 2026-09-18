#!/usr/bin/env bash
set -euo pipefail

ORC_HOME="${ORC_HOME:-$HOME/.config/orc}"
CONFIG="$ORC_HOME/config.json"
MODELS_CACHE="$ORC_HOME/models.json"
QUALITY_CACHE="$ORC_HOME/quality.json"
CLAUDE_STATE="$ORC_HOME/claude-state"
API="https://openrouter.ai/api/v1"
BASE_URL="https://openrouter.ai/api"
KEYCHAIN_SERVICE="orc-openrouter"
CACHE_TTL=86400
HUD_SCRIPT="$ORC_HOME/hud.sh"
FIT_CACHE="$ORC_HOME/fit.json"
LAST_LAUNCH="$ORC_HOME/last-launch"
# Bundled Artificial Analysis quality snapshot, shipped with the script.
# Resolves to data/quality.json next to the orc script, or to the path
# shipped in the installed copy under $ORC_HOME.
BUNDLED_QUALITY="${BUNDLED_QUALITY:-$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")/data/quality.json}"
# ORC_HUD_VERSION and the ORC_HUD_BODY heredoc are written by build.sh from hud.sh.in + pricing.jq
ORC_HUD_VERSION="75c071be539e"

err()  { printf '\033[31morc: %s\033[0m\n' "$*" >&2; }
info() { printf '\033[2m%s\033[0m\n' "$*" >&2; }
bold() { printf '\033[1m%s\033[0m\n' "$*" >&2; }

need() { command -v "$1" >/dev/null 2>&1 || { err "missing dependency: $1"; exit 1; }; }
need jq; need curl

ORC_OS="$(uname -s 2>/dev/null || printf unknown)"
KEY_FILE="$ORC_HOME/key"
is_mac() { [ "$ORC_OS" = "Darwin" ]; }

key_read() {
  if is_mac; then
    security find-generic-password -ws "$KEYCHAIN_SERVICE" 2>/dev/null || true
  else
    [ -f "$KEY_FILE" ] || return 0
    if [ -n "$(find "$KEY_FILE" -perm -004 2>/dev/null)" ]; then
      info "note: $KEY_FILE is readable by other users — fix with: chmod 600 $KEY_FILE"
    fi
    cat "$KEY_FILE"
  fi
}

key_store() {
  if is_mac; then
    security add-generic-password -U -s "$KEYCHAIN_SERVICE" -a "$USER" -w "$1"
  else
    mkdir -p "$ORC_HOME"
    (umask 077; printf '%s' "$1" > "$KEY_FILE")
  fi
}

key_noun() {
  if is_mac; then printf 'macOS keychain (%s)' "$KEYCHAIN_SERVICE"
  else printf 'key file (%s)' "$KEY_FILE"
  fi
}

mtime_of() {
  if is_mac; then stat -f %m "$1"; else stat -c %Y "$1"; fi
}

mtime_lines() {
  if is_mac; then xargs -0 stat -f '%m %N'; else xargs -0 stat -c '%Y %n'; fi
}

table() {
  if command -v column >/dev/null 2>&1; then
    column -t -s "$(printf '\t')"
  else
    awk -F'\t' '
      { lines[NR] = $0; n = NR
        for (i = 1; i <= NF; i++) if (length($i) > w[i]) w[i] = length($i) }
      END { for (r = 1; r <= n; r++) {
              nf = split(lines[r], f, "\t"); s = ""
              for (i = 1; i <= nf; i++)
                s = s sprintf("%-*s", w[i] + (i < nf ? 2 : 0), f[i])
              print s } }'
  fi
}

cfg() { [ -f "$CONFIG" ] || return 0; jq -r "$1 // empty" "$CONFIG" 2>/dev/null || true; }

save_cfg() {
  mkdir -p "$ORC_HOME"
  local tmp="$CONFIG.tmp"
  if [ -f "$CONFIG" ]; then
    jq --arg v "$2" ".$1 = \$v" "$CONFIG" > "$tmp"
  else
    jq -n --arg v "$2" "{$1: \$v}" > "$tmp"
  fi
  mv "$tmp" "$CONFIG"
}

find_project_cfg() {
  local d="$PWD"
  while :; do
    if [ -f "$d/.orc.json" ]; then printf '%s' "$d/.orc.json"; return 0; fi
    [ "$d" = "/" ] && return 1
    d="$(dirname "$d")"
  done
}
PROJECT_CFG_FILE="$(find_project_cfg || true)"

pcfg() {
  if [ -n "$PROJECT_CFG_FILE" ]; then
    jq -r "$1 // empty" "$PROJECT_CFG_FILE" 2>/dev/null || true
  fi
}

profile_get() {
  if [ -f "$CONFIG" ]; then
    jq -r --arg n "$1" ".profiles[\$n].$2 // empty" "$CONFIG" 2>/dev/null || true
  fi
}

profile_exists() {
  [ -f "$CONFIG" ] && jq -e --arg n "$1" '(.profiles // {})[$n] != null' "$CONFIG" >/dev/null 2>&1
}

resolve_src() {
  local key="$1" v p
  if [ "$key" = "model" ] && [ -n "${ORC_MODEL_OVERRIDE:-}" ]; then
    printf 'flag'; return 0
  fi
  if [ "$key" = "mode" ] && [ -n "${ORC_MODE:-}" ]; then
    printf 'flag'; return 0
  fi
  if [ -n "${ORC_PROFILE:-}" ]; then
    v="$(profile_get "$ORC_PROFILE" "$key")"
    if [ -n "$v" ]; then printf 'profile'; return 0; fi
  fi
  v="$(pcfg ".$key")"
  if [ -n "$v" ]; then printf 'project'; return 0; fi
  p="$(pcfg .profile)"
  if [ -n "$p" ]; then
    v="$(profile_get "$p" "$key")"
    if [ -n "$v" ]; then printf 'project-profile'; return 0; fi
  fi
  v="$(cfg ".$key")"
  if [ -n "$v" ]; then printf 'config'; return 0; fi
  printf 'default'
}

resolve() {
  local key="$1" v p
  if [ "$key" = "model" ] && [ -n "${ORC_MODEL_OVERRIDE:-}" ]; then
    printf '%s' "$ORC_MODEL_OVERRIDE"; return 0
  fi
  if [ "$key" = "mode" ] && [ -n "${ORC_MODE:-}" ]; then
    printf '%s' "$ORC_MODE"; return 0
  fi
  if [ -n "${ORC_PROFILE:-}" ]; then
    v="$(profile_get "$ORC_PROFILE" "$key")"
    if [ -n "$v" ]; then printf '%s' "$v"; return 0; fi
  fi
  v="$(pcfg ".$key")"
  if [ -n "$v" ]; then printf '%s' "$v"; return 0; fi
  p="$(pcfg .profile)"
  if [ -n "$p" ]; then
    v="$(profile_get "$p" "$key")"
    if [ -n "$v" ]; then printf '%s' "$v"; return 0; fi
  fi
  cfg ".$key"
}

resolved_profile() {
  if [ -n "${ORC_PROFILE:-}" ]; then printf '%s' "$ORC_PROFILE"; return 0; fi
  pcfg .profile
}

model_ctx() {
  local id="$1"
  [ -z "$id" ] || [ ! -f "$MODELS_CACHE" ] && return 0
  jq -r --arg id "$id" '.data[] | select(.id == $id) | .context_length // empty' "$MODELS_CACHE" 2>/dev/null || true
}

fit_map() {
  if [ ! -f "$FIT_CACHE" ]; then printf '{}'; return 0; fi
  jq -c --argjson now "$(date +%s)" --argjson ttl "$CACHE_TTL" '
    if type != "object" then {}
    else
      [to_entries[]
       | select(($now - (.value.checked_at // 0)) < $ttl)
       | {key, value: (if .value.ok == true then "FIT" else "FAIL" end)}]
      | from_entries
    end' "$FIT_CACHE" 2>/dev/null || printf '{}'
}

fit_of() {
  local id="$1"
  [ -z "$id" ] && { printf 'UNTESTED'; return 0; }
  [ ! -f "$FIT_CACHE" ] && { printf 'UNTESTED'; return 0; }
  jq -r --arg id "$id" --argjson now "$(date +%s)" --argjson ttl "$CACHE_TTL" '
    (.[$id] // null) as $f
    | if $f == null then "UNTESTED"
      elif (($now - ($f.checked_at // 0)) >= $ttl) then "UNTESTED"
      elif $f.ok == true then "FIT"
      else "FAIL" end' "$FIT_CACHE" 2>/dev/null || printf 'UNTESTED'
}

status_json() {
  local model small mode profile project ctx tools fit
  model="$(resolve model)"
  small="$(resolve small_model)"; small="${small:-$model}"
  mode="$(resolve mode)"; mode="${mode:-default}"
  profile="$(resolved_profile)"
  project="${PROJECT_CFG_FILE:-}"
  ctx="$(model_ctx "$model")"
  tools="false"
  if [ -n "$model" ] && [ -f "$MODELS_CACHE" ] && model_has_tools "$model"; then tools="true"; fi
  fit="$(fit_of "$model")"
  jq -nc \
    --arg model "$model" --arg small "$small" --arg mode "$mode" \
    --arg profile "$profile" --arg project "$project" --arg ctx "$ctx" \
    --argjson tools "$tools" --arg fit "$fit" \
    --arg sm "$(resolve_src model)" --arg ss "$(resolve_src small_model)" --arg so "$(resolve_src mode)" \
    '{
      model:$model,
      small_model:$small,
      mode:$mode,
      profile:$profile,
      project_file:$project,
      ctx: (if $ctx == "" then null else ($ctx|tonumber) end),
      tools:$tools,
      fit:$fit,
      source:{model:$sm, small_model:$ss, mode:$so}
    }'
}

status_cmd() {
  case "${1:-}" in
    --json) status_json; return 0 ;;
    "" ) ;;
    *) err "unknown option: status $1"; info "usage: orc status [--json]"; exit 1 ;;
  esac
  local model mode profile project src
  model="$(resolve model)"
  mode="$(resolve mode)"; mode="${mode:-default}"
  profile="$(resolved_profile)"
  project="${PROJECT_CFG_FILE:-}"
  src="$(resolve_src model)"
  bold "orc status"
  printf '  model:   %s  (%s)\n' "${model:-—}" "$src"
  printf '  small:   %s  (%s)\n' "$(resolve small_model)" "$(resolve_src small_model)"
  printf '  mode:    %s  (%s)\n' "$mode" "$(resolve_src mode)"
  [ -n "$profile" ] && printf '  profile: @%s\n' "$profile"
  [ -n "$project" ] && printf '  project: %s\n' "$project"
  local ctx; ctx="$(model_ctx "$model")"
  [ -n "$ctx" ] && printf '  ctx:     %s\n' "$ctx"
  if [ -n "$model" ] && [ -f "$MODELS_CACHE" ]; then
    if model_has_tools "$model"; then
      printf '  tools:   yes · fit: %s\n' "$(fit_of "$model")"
    else
      printf '  tools:   no\n'
    fi
  fi
}

save_profile() {
  local n="$1"
  if [[ ! "$n" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    err "invalid profile name: $n (use letters, digits, dash, underscore)"
    exit 1
  fi
  local model small mode
  model="$(resolve model)"
  small="$(resolve small_model)"
  mode="$(resolve mode)"
  [ -z "$model" ] && { err "no model configured — run: orc setup"; exit 1; }
  mkdir -p "$ORC_HOME"
  [ -f "$CONFIG" ] || printf '{}\n' > "$CONFIG"
  local tmp="$CONFIG.tmp"
  jq --arg n "$n" --arg model "$model" --arg small "$small" --arg mode "$mode" '
    .profiles[$n] = ({model:$model, small_model:$small, mode:$mode}
      | with_entries(select(.value != null and .value != "")))
  ' "$CONFIG" > "$tmp"
  mv "$tmp" "$CONFIG"
  local extra=""
  [ -n "$small" ] && extra="$extra · small: $small"
  [ -n "$mode" ] && extra="$extra · mode: $mode"
  info "profile @$n saved: $model$extra"
  info "launch with: orc @$n"
}

profiles_cmd() {
  case "${1:-}" in
    rm)
      shift
      [ -z "${1:-}" ] && { err "usage: orc profiles rm <name>"; exit 1; }
      profile_exists "$1" || { err "unknown profile: $1"; exit 1; }
      local tmp="$CONFIG.tmp"
      jq --arg n "$1" 'del(.profiles[$n])' "$CONFIG" > "$tmp"
      mv "$tmp" "$CONFIG"
      info "profile removed: $1"
      ;;
    "")
      if [ ! -f "$CONFIG" ] || [ "$(jq -r '(.profiles // {}) | length' "$CONFIG" 2>/dev/null)" = "0" ]; then
        info "no profiles saved — snapshot the current setup with: orc save <name>"
        return 0
      fi
      { printf 'PROFILE\tMODEL\tSMALL\tMODE\n'
        jq -r '.profiles | to_entries | sort_by(.key)[]
               | [("@" + .key), (.value.model // "-"), (.value.small_model // "-"), (.value.mode // "-")]
               | @tsv' "$CONFIG"
      } | table
      ;;
    *)
      err "unknown option: profiles $1"
      info "usage: orc profiles [rm <name>]"
      exit 1
      ;;
  esac
}

key_env_name() {
  local n
  n="$(cfg .key_env)"
  if [[ "$n" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
    printf '%s' "$n"
  else
    printf 'OPENROUTER_API_KEY'
  fi
}

resolve_key() {
  local envname v
  envname="$(key_env_name)"
  v="${!envname:-}"
  if [ -n "$v" ]; then printf '%s' "$v"; return 0; fi
  key_read
}

key_source() {
  local envname
  envname="$(key_env_name)"
  if [ -n "${!envname:-}" ]; then printf 'env $%s' "$envname"
  elif [ -n "$(key_read)" ]; then key_noun
  else printf 'none'
  fi
}

require_key() {
  local key
  key="$(resolve_key)"
  if [ -z "$key" ]; then
    err "no OpenRouter API key found"
    info "looked in: env \$$(key_env_name), then $(key_noun)"
    info "fix: export $(key_env_name)=sk-or-... in your shell, or run: orc key"
    exit 1
  fi
  printf '%s' "$key"
}

fetch_models() {
  mkdir -p "$ORC_HOME"
  if [ -f "$MODELS_CACHE" ] && [ "${1:-}" != "force" ]; then
    local age=$(( $(date +%s) - $(mtime_of "$MODELS_CACHE" 2>/dev/null || echo 0) ))
    [ "$age" -lt "$CACHE_TTL" ] && return 0
  fi
  info "fetching model list from OpenRouter..."
  if curl -sf --max-time 30 "$API/models" -o "$MODELS_CACHE.tmp"; then
    mv "$MODELS_CACHE.tmp" "$MODELS_CACHE"
  elif [ -f "$MODELS_CACHE" ]; then
    info "fetch failed; using cached list"
  else
    err "could not fetch model list from $API/models"
    exit 1
  fi
}

# Artificial Analysis quality ranking.
#
# fetch_quality mirrors fetch_models but is fail-soft: if the cache is
# missing AND the bundled seed isn't there AND we can't reach the
# network, the picker still works (it just falls back to the old
# FIT-then-id sort). Never block a launch on this.
#
# The bundled seed (data/quality.json in the orc source) is shipped with
# the script and copied to $ORC_HOME/quality.json on first run, so the
# picker has a real ranking out of the box. `orc refresh` re-fetches.

fetch_quality() {
  mkdir -p "$ORC_HOME"
  # Seed the cache from the bundled data on first run only.
  if [ ! -f "$QUALITY_CACHE" ] && [ -f "$BUNDLED_QUALITY" ]; then
    cp "$BUNDLED_QUALITY" "$QUALITY_CACHE"
  fi
  if [ -f "$QUALITY_CACHE" ] && [ "${1:-}" != "force" ]; then
    local age=$(( $(date +%s) - $(mtime_of "$QUALITY_CACHE" 2>/dev/null || echo 0) ))
    [ "$age" -lt "$CACHE_TTL" ] && return 0
  fi
  # Refreshing requires the cmndcntr fetcher to actually be present.
  # We don't try to re-implement the leaderboard scrape here; the bundled
  # snapshot is authoritative until the user runs `make refresh-quality`
  # (or copies a fresh quality.json into $ORC_HOME by hand).
  info "quality cache is stale; refresh with: cp graphify-out/artificial-analysis-quality.json $ORC_HOME/quality.json"
}

# Static slug -> openrouterId map for records Artificial Analysis lists
# under a different slug than OpenRouter (e.g. effort variants like
# "claude-opus-5-xhigh" don't have their own OR id — they live under
# "anthropic/claude-opus-5"). 8 entries; grew from the live fetcher's
# unmappedSlugs list. Add new entries here as they show up in
# `unmappedSlugs` from a quality.json refresh.
SLUG_TO_OR_ID='{
  "claude-opus-5-xhigh":   "anthropic/claude-opus-5",
  "claude-opus-5-high":    "anthropic/claude-opus-5",
  "claude-opus-5-medium":  "anthropic/claude-opus-5",
  "claude-opus-5-low":     "anthropic/claude-opus-5",
  "claude-fable-5-xhigh":  "anthropic/claude-fable-5",
  "gpt-5-6-sol-xhigh":     "openai/gpt-5.6-sol",
  "grok-4-6-xhigh":        "x-ai/grok-4.6",
  "gemini-3-5-flash-minimal": "google/gemini-3.5-flash"
}'

# quality_map: produce { "<openrouterId>": <intelligenceIndex, ...> }
# for every record in the quality cache. Records with no openrouterApiId
# fall through to slug_to_or_id_map. Returns "{}" on a missing cache
# so the picker sort degrades to "no rank" cleanly.
quality_map() {
  [ -f "$QUALITY_CACHE" ] || { printf '{}'; return 0; }
  jq -c --argjson slugmap "$SLUG_TO_OR_ID" '
    .records
    | map(
        if .openrouterApiId then {key: .openrouterApiId, value: .}
        elif $slugmap[.slug] then {key: $slugmap[.slug], value: .}
        else empty
        end
      )
    | from_entries
  ' "$QUALITY_CACHE" 2>/dev/null || printf '{}'
}

model_rows() {
  local filter="${1:-all}"
  jq -r --arg filter "$filter" --argjson fit "$(fit_map)" --argjson quality "$(quality_map)" '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.
# Constraint: no single quotes here (consumer programs are bash single-quoted).

def n2($x):
  if $x == null then 0
  elif ($x|type)=="number" then $x
  elif ($x|type)=="string" then (($x|tonumber?) // 0)
  else 0 end;

def response_ok:
  ((type)=="object")
  and ((.message // null)|type=="object")
  and ((.message.id // null)|type=="string")
  and ((.message.usage // null)|type=="object")
  and ((.message.model // null)|type=="string")
  and ((.message.model|startswith("<"))|not);

def usage_row:
  { m:  .m,
    i:  n2(.u.input_tokens),
    o:  n2(.u.output_tokens),
    r:  n2(.u.cache_read_input_tokens),
    w5: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_5m_input_tokens)
         else n2(.u.cache_creation_input_tokens) end),
    w1: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_1h_input_tokens)
         else 0 end)};

def row_cost($P):
  ($P[.m] // null) as $pr
  | if ($pr|type)=="object"
    then (.i*n2($pr.p)) + (.o*n2($pr.c)) + (.r*n2($pr.cr))
         + (.w5*n2($pr.w5)) + (.w1*n2($pr.w1h))
    else null end;

def catalog_map:
  .data
  | map({key:.id,
         value:{ctx:(.context_length // 0),
                p:.pricing.prompt, c:.pricing.completion,
                cr:.pricing.input_cache_read,
                w5:.pricing.input_cache_write,
                w1h:.pricing.input_cache_write_1h}})
  | from_entries;

def model_price($f): ((.pricing[$f] // "0") | tonumber);

def is_free:
  model_price("prompt") == 0
  and model_price("completion") == 0
  and model_price("request") == 0
  and model_price("image") == 0
  and model_price("web_search") == 0
  and model_price("internal_reasoning") == 0;

def has_tools:
  ((.supported_parameters // []) | index("tools")) != null;

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | if (.ids[$id] // null) != null
    then .agg = (.agg | dec_row($P; (.ids[$id]|usage_row)) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
    .data
    | sort_by(
        -((($quality[.id].intelligenceIndex // -1))),
        ((.pricing.prompt // "0") | tonumber? // 0),
        (if ($fit[.id] // "") == "FIT" then 0
         elif has_tools then 1
         else 2 end),
        .id)[]
    | select($filter == "all"
             or ($filter == "free" and is_free)
             or ($filter == "free-tools" and is_free and has_tools)
             or ($filter == "tools" and has_tools)
             or ($filter == "fit" and ($fit[.id] // "") == "FIT"))
    | [ .id,
        (if is_free
         then "FREE"
         else "$\((((.pricing.prompt // "0")|tonumber) * 100000000 | round) / 100)/M in  $\((((.pricing.completion // "0")|tonumber) * 100000000 | round) / 100)/M out"
         end),
        "\(((.context_length // 0) / 1000) | round)k ctx",
        (if has_tools then "tools" else "NO TOOLS" end),
        ($fit[.id] // "UNTESTED")
      ] | @tsv' "$MODELS_CACHE"
}

model_exists() { jq -e --arg id "$1" '.data[] | select(.id == $id)' "$MODELS_CACHE" >/dev/null 2>&1; }

model_has_tools() {
  jq -e --arg id "$1" '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.
# Constraint: no single quotes here (consumer programs are bash single-quoted).

def n2($x):
  if $x == null then 0
  elif ($x|type)=="number" then $x
  elif ($x|type)=="string" then (($x|tonumber?) // 0)
  else 0 end;

def response_ok:
  ((type)=="object")
  and ((.message // null)|type=="object")
  and ((.message.id // null)|type=="string")
  and ((.message.usage // null)|type=="object")
  and ((.message.model // null)|type=="string")
  and ((.message.model|startswith("<"))|not);

def usage_row:
  { m:  .m,
    i:  n2(.u.input_tokens),
    o:  n2(.u.output_tokens),
    r:  n2(.u.cache_read_input_tokens),
    w5: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_5m_input_tokens)
         else n2(.u.cache_creation_input_tokens) end),
    w1: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_1h_input_tokens)
         else 0 end)};

def row_cost($P):
  ($P[.m] // null) as $pr
  | if ($pr|type)=="object"
    then (.i*n2($pr.p)) + (.o*n2($pr.c)) + (.r*n2($pr.cr))
         + (.w5*n2($pr.w5)) + (.w1*n2($pr.w1h))
    else null end;

def catalog_map:
  .data
  | map({key:.id,
         value:{ctx:(.context_length // 0),
                p:.pricing.prompt, c:.pricing.completion,
                cr:.pricing.input_cache_read,
                w5:.pricing.input_cache_write,
                w1h:.pricing.input_cache_write_1h}})
  | from_entries;

def model_price($f): ((.pricing[$f] // "0") | tonumber);

def is_free:
  model_price("prompt") == 0
  and model_price("completion") == 0
  and model_price("request") == 0
  and model_price("image") == 0
  and model_price("web_search") == 0
  and model_price("internal_reasoning") == 0;

def has_tools:
  ((.supported_parameters // []) | index("tools")) != null;

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | if (.ids[$id] // null) != null
    then .agg = (.agg | dec_row($P; (.ids[$id]|usage_row)) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
    .data[] | select(.id == $id) | has_tools' "$MODELS_CACHE" >/dev/null 2>&1
}

pick_model() {
  need fzf
  fetch_models
  fetch_quality
  local sel filter header
  filter="${3:-all}"
  case "$filter" in
    free) header="currently free on OpenRouter · ranked by Artificial Analysis intelligence · FIT = probed" ;;
    fit) header="models that survived orc probe --fit in the last 24h · ranked by quality" ;;
    *) header="ranked by Artificial Analysis intelligence index · type FREE for free models · NO TOOLS = poor Claude Code fit · FIT = probed" ;;
  esac
  sel="$(model_rows "$filter" | table \
        | fzf --prompt="${2:-model}> " --query="${1:-}" --header="$header" --height=20 --reverse)" || return 1
  printf '%s' "${sel%% *}"
}

# `orc quality` — dump the quality cache as a sorted table.
# Columns: openrouter id, intelligence index, code/agent scores, creator.
# Reads $QUALITY_CACHE only; does not touch the network. Useful for
# spot-checking what the picker will see, without opening a browser.
quality_cmd() {
  fetch_quality
  if [ ! -f "$QUALITY_CACHE" ]; then
    err "no quality cache at $QUALITY_CACHE"
    info "ship a fresh copy to that path, or re-run: make refresh-quality (from the orc source)"
    return 1
  fi
  {
    printf '%s\n' "OR_ID	II	CODE	AGENT	CREATOR	SLUG"
    jq -r --argjson slugmap "$SLUG_TO_OR_ID" '
      .records
      | map(
          ((if .openrouterApiId then .openrouterApiId
           elif $slugmap[.slug] then "[map] " + $slugmap[.slug]
           else "(no OR id)" end)) as $orid
        | ((if .intelligenceIndex == null then "—"
           else (.intelligenceIndex | tostring | .[0:5]) end)) as $ii
        | ((if .codingIndex == null then "—"
           else (.codingIndex | tostring | .[0:5]) end)) as $ci
        | ((if .agenticIndex == null then "—"
           else (.agenticIndex | tostring | .[0:5]) end)) as $ai
        | ((if .intelligenceIndexIsEstimated then " est" else "" end)) as $est
        | [(.intelligenceIndex // -1), $orid, $ii + $est, $ci, $ai, (.modelCreatorName // "-"), .slug]
      )
      | sort_by(.[0]) | reverse
      | .[] | .[1:] | @tsv
    ' "$QUALITY_CACHE"
  } | table
}

key_wizard() {
  local envname
  envname="$(key_env_name)"
  bold "orc key setup"
  info "key lookup order: env \$$envname, then $(key_noun)"
  info "current source: $(key_source)"
  printf '  [1] change which env var orc reads\n  [2] paste a key -> store in %s\n  [Enter] keep as is\n' "$(key_noun)" >&2
  printf '> ' >&2
  local ans; IFS= read -r ans
  case "$ans" in
    1)
      printf 'env var name [%s]: ' "$envname" >&2
      local n; IFS= read -r n
      if [ -n "$n" ]; then
        if [[ ! "$n" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
          err "invalid environment variable name: $n"
          return 1
        fi
        save_cfg key_env "$n"
        info "orc will now read \$$n"
        [ -z "${!n:-}" ] && info "note: \$$n is not set in this shell — add 'export $n=sk-or-...' to your ~/.zshrc"
      fi
      ;;
    2)
      printf 'paste key (input hidden): ' >&2
      local k; IFS= read -rs k; printf '\n' >&2
      if [ -n "$k" ]; then
        key_store "$k"
        info "stored in $(key_noun); orc will use it whenever \$$(key_env_name) is unset"
      fi
      ;;
  esac
}

choose_mode() {
  bold "permission mode:"
  printf '  [1] default          normal permission prompting\n' >&2
  printf '  [2] auto             auto-approve safe actions\n' >&2
  printf '  [3] acceptEdits      auto-accept file edits\n' >&2
  printf '  [4] plan             read-only planning mode\n' >&2
  printf '  [5] dontAsk          never prompt (denies what would need asking)\n' >&2
  printf '  [6] yolo             bypass ALL permission checks\n' >&2
  printf '> ' >&2
  local ans; IFS= read -r ans
  case "$ans" in
    1) printf 'default' ;;
    2) printf 'auto' ;;
    3) printf 'acceptEdits' ;;
    4) printf 'plan' ;;
    5) printf 'dontAsk' ;;
    6) printf 'yolo' ;;
    *) return 1 ;;
  esac
}

pick_mode() {
  local m
  m="$(choose_mode)" || return 1
  save_cfg mode "$m"
  info "mode saved: $m"
}

pick_profile() {
  need fzf
  if [ ! -f "$CONFIG" ] || [ "$(jq -r '(.profiles // {}) | length' "$CONFIG" 2>/dev/null)" = "0" ]; then
    err "no profiles saved — snapshot the current setup with: orc save <name>"
    return 1
  fi
  local sel
  sel="$(jq -r '.profiles | to_entries | sort_by(.key)[]
                | [.key, (.value.model // "-"), (.value.mode // "-")] | @tsv' "$CONFIG" \
        | table \
        | fzf --prompt="profile> " --header="saved profiles" --height=12 --reverse)" || return 1
  printf '%s' "${sel%% *}"
}

orc_mode() {
  local m="${ORC_MODE:-$(resolve mode)}"
  case "$m" in
    ""|default|auto|acceptEdits|plan|dontAsk|yolo|bypassPermissions) printf '%s' "$m" ;;
    *) err "invalid mode: $m"; return 1 ;;
  esac
}

hud_enabled() {
  [ "${ORC_HUD:-}" = "0" ] && return 1
  [ "${ORC_HUD:-}" = "1" ] && return 0
  [ "$(cfg .hud)" = "off" ] && return 1
  return 0
}

setup_wizard() {
  bold "orc — OpenRouter x Claude Code setup"
  if [ "$(key_source)" = "none" ]; then
    info "no API key found"
    key_wizard
    [ -z "$(resolve_key)" ] && { err "still no key; aborting"; exit 1; }
  else
    info "API key found via $(key_source)"
  fi
  local m
  if m="$(pick_model "" "pick a model")"; then
    save_cfg model "$m"
    info "model saved: $m"
  else
    err "no model selected; run 'orc setup' again"
    exit 1
  fi
  printf 'also pick a small/fast model for background tasks? [y/N] ' >&2
  local ans; IFS= read -r ans
  if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
    local s
    if s="$(pick_model "" "small model")"; then
      save_cfg small_model "$s"
      info "small model saved: $s"
    fi
  fi
  printf 'pick a launch permission mode? [y/N] ' >&2
  local mans; IFS= read -r mans
  if [ "$mans" = "y" ] || [ "$mans" = "Y" ]; then pick_mode || true; fi
  info "setup complete — config: $CONFIG"
}

session_override() {
  local src="$1"
  [ "$src" = "flag" ] || [ "$src" = "profile" ] || [ "$src" = "project" ] || [ "$src" = "project-profile" ]
}

confirm_or_change() {
  [ -t 0 ] || return 0
  [ "${ORC_YES:-}" = "1" ] && return 0
  while true; do
    local model mode profile project hint="" src
    model="$(resolve model)"
    mode="$(resolve mode)"; mode="${mode:-default}"
    profile="$(resolved_profile)"
    project="${PROJECT_CFG_FILE:-}"
    src="$(resolve_src model)"
    [ -n "$profile" ] && hint="$hint · @$profile"
    [ -n "$project" ] && hint="$hint · $project"
    [ "$src" != "config" ] && [ "$src" != "default" ] && hint="$hint · $src"
    bold "orc → ${model:-—} · mode: $mode$hint"
    printf '  [Enter] launch   [m] model   [a] profile   [f] free   [p] mode   [s] save   [k] key   [q] quit\n' >&2
    local ans; IFS= read -rsn1 ans || true
    case "$ans" in
      m|M)
        local mm
        if mm="$(pick_model "" "model")"; then
          if session_override "$(resolve_src model)"; then
            ORC_MODEL_OVERRIDE="$mm"
            info "this launch: $mm  (not saved — [s] to snapshot)"
          else
            save_cfg model "$mm"
            info "model saved: $mm"
          fi
        fi
        ;;
      f|F)
        local fm
        if fm="$(pick_model "" "free model" "free")"; then
          if session_override "$(resolve_src model)"; then
            ORC_MODEL_OVERRIDE="$fm"
            info "this launch: $fm  (not saved — [s] to snapshot)"
          else
            save_cfg model "$fm"
            info "model saved: $fm"
          fi
        fi
        ;;
      a|A)
        local pn
        if pn="$(pick_profile)"; then
          ORC_PROFILE="$pn"
          unset ORC_MODEL_OVERRIDE
          info "this launch: @$pn"
        fi
        ;;
      p|P)
        local nm
        if nm="$(choose_mode)"; then
          if session_override "$(resolve_src mode)" || [ -n "${ORC_PROFILE:-}" ]; then
            ORC_MODE="$nm"
            info "this launch: mode $nm  (not saved — [s] to snapshot)"
          else
            save_cfg mode "$nm"
            info "mode saved: $nm"
          fi
        fi
        ;;
      s|S)
        printf 'profile name: ' >&2
        local pn; IFS= read -r pn
        [ -n "$pn" ] && save_profile "$pn"
        ;;
      k|K) key_wizard ;;
      q|Q) exit 0 ;;
      *) return 0 ;;
    esac
  done
}

install_hud() {
  local cur=""
  [ -f "$HUD_SCRIPT" ] && cur="$(sed -n 's/^# ORC_HUD_VERSION=//p' "$HUD_SCRIPT" 2>/dev/null | head -1)"
  [ "$cur" = "$ORC_HUD_VERSION" ] && return 0
  mkdir -p "$ORC_HOME" || return 1
  {
    printf '#!/usr/bin/env bash\n'
    printf '# ORC_HUD_VERSION=%s\n' "$ORC_HUD_VERSION"
    printf '# generated by orc — edit hud.sh in the orc repo and run build.sh\n'
    cat <<'ORC_HUD_BODY'
ORC_HOME="${ORC_HOME:-$HOME/.config/orc}"
MODELS_CACHE="${MODELS_CACHE:-$ORC_HOME/models.json}"
SESSION_DIR="${ORC_SESSION_CACHE:-$ORC_HOME/sessions}"
LAST_LAUNCH="${ORC_LAST_LAUNCH:-$ORC_HOME/last-launch}"

payload="$(cat 2>/dev/null || true)"
DIM=$'\033[2m'; RST=$'\033[0m'; BOLD=$'\033[1m'
GRN=$'\033[32m'; YLW=$'\033[33m'; RED=$'\033[31m'

if [ -z "$payload" ]; then
  printf '%s—%s\n' "$DIM" "$RST"
  exit 0
fi

eval "$(printf '%s' "$payload" | jq -r '
  "P_TX="    + (@sh "\(.transcript_path // "")"),
  "P_MID="   + (@sh "\(.model.id // "")"),
  "P_MNAME=" + (@sh "\(.model.display_name // "")"),
  "P_UPCT="  + (@sh "\(.context_window.used_percentage // "")"),
  "P_CSIZE=" + (@sh "\(.context_window.context_window_size // "")"),
  "P_CU="    + (@sh "\(.context_window.current_usage // null | tostring)")
' 2>/dev/null)" || true
: "${P_TX:=}" "${P_MID:=}" "${P_MNAME:=}" "${P_UPCT:=}" "${P_CSIZE:=}"
[ -z "${P_CU:-}" ] && P_CU="null"

P="{}"
if [ -f "$MODELS_CACHE" ]; then
  P="$(jq -c '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.
# Constraint: no single quotes here (consumer programs are bash single-quoted).

def n2($x):
  if $x == null then 0
  elif ($x|type)=="number" then $x
  elif ($x|type)=="string" then (($x|tonumber?) // 0)
  else 0 end;

def response_ok:
  ((type)=="object")
  and ((.message // null)|type=="object")
  and ((.message.id // null)|type=="string")
  and ((.message.usage // null)|type=="object")
  and ((.message.model // null)|type=="string")
  and ((.message.model|startswith("<"))|not);

def usage_row:
  { m:  .m,
    i:  n2(.u.input_tokens),
    o:  n2(.u.output_tokens),
    r:  n2(.u.cache_read_input_tokens),
    w5: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_5m_input_tokens)
         else n2(.u.cache_creation_input_tokens) end),
    w1: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_1h_input_tokens)
         else 0 end)};

def row_cost($P):
  ($P[.m] // null) as $pr
  | if ($pr|type)=="object"
    then (.i*n2($pr.p)) + (.o*n2($pr.c)) + (.r*n2($pr.cr))
         + (.w5*n2($pr.w5)) + (.w1*n2($pr.w1h))
    else null end;

def catalog_map:
  .data
  | map({key:.id,
         value:{ctx:(.context_length // 0),
                p:.pricing.prompt, c:.pricing.completion,
                cr:.pricing.input_cache_read,
                w5:.pricing.input_cache_write,
                w1h:.pricing.input_cache_write_1h}})
  | from_entries;

def model_price($f): ((.pricing[$f] // "0") | tonumber);

def is_free:
  model_price("prompt") == 0
  and model_price("completion") == 0
  and model_price("request") == 0
  and model_price("image") == 0
  and model_price("web_search") == 0
  and model_price("internal_reasoning") == 0;

def has_tools:
  ((.supported_parameters // []) | index("tools")) != null;

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | (.ids[$id] // null) as $old
  | (if $old != null then ($old|usage_row) else null end) as $orow
  | if $orow != null
    then .agg = (.agg | dec_row($P; $orow) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
    catalog_map' "$MODELS_CACHE" 2>/dev/null || true)"
  [ -z "${P:-}" ] && P="{}"
fi

Z='{"i":0,"o":0,"r":0,"w5":0,"w1":0,"cost":0,"unknown":false,"msgs":0}'
EMPTY_ST='{"size":0,"off":0,"ids":{},"agg":'"$Z"',"last":null}'

file_bytes() { wc -c < "$1" 2>/dev/null | tr -d ' \t\n'; }

complete_chunk() {
  local src="$1" dest="$2" hex
  if [ ! -s "$src" ]; then
    : > "$dest"
    printf '0'
    return 0
  fi
  hex="$(tail -c 1 "$src" 2>/dev/null | od -An -tx1 | tr -d ' \n')"
  if [ "$hex" = "0a" ]; then
    cp "$src" "$dest" 2>/dev/null || : > "$dest"
  else
    sed '$d' "$src" > "$dest" 2>/dev/null || : > "$dest"
  fi
  file_bytes "$dest"
}

ingest_file() {
  local path="$1" state="$2" tmp chunk complete consumed size off newstate
  if [ ! -f "$path" ]; then
    printf '%s' "$EMPTY_ST"
    return 0
  fi
  size="$(file_bytes "$path")"
  [ -z "$size" ] && size=0
  state="$(printf '%s' "$state" | jq -c --argjson z "$Z" '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.
# Constraint: no single quotes here (consumer programs are bash single-quoted).

def n2($x):
  if $x == null then 0
  elif ($x|type)=="number" then $x
  elif ($x|type)=="string" then (($x|tonumber?) // 0)
  else 0 end;

def response_ok:
  ((type)=="object")
  and ((.message // null)|type=="object")
  and ((.message.id // null)|type=="string")
  and ((.message.usage // null)|type=="object")
  and ((.message.model // null)|type=="string")
  and ((.message.model|startswith("<"))|not);

def usage_row:
  { m:  .m,
    i:  n2(.u.input_tokens),
    o:  n2(.u.output_tokens),
    r:  n2(.u.cache_read_input_tokens),
    w5: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_5m_input_tokens)
         else n2(.u.cache_creation_input_tokens) end),
    w1: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_1h_input_tokens)
         else 0 end)};

def row_cost($P):
  ($P[.m] // null) as $pr
  | if ($pr|type)=="object"
    then (.i*n2($pr.p)) + (.o*n2($pr.c)) + (.r*n2($pr.cr))
         + (.w5*n2($pr.w5)) + (.w1*n2($pr.w1h))
    else null end;

def catalog_map:
  .data
  | map({key:.id,
         value:{ctx:(.context_length // 0),
                p:.pricing.prompt, c:.pricing.completion,
                cr:.pricing.input_cache_read,
                w5:.pricing.input_cache_write,
                w1h:.pricing.input_cache_write_1h}})
  | from_entries;

def model_price($f): ((.pricing[$f] // "0") | tonumber);

def is_free:
  model_price("prompt") == 0
  and model_price("completion") == 0
  and model_price("request") == 0
  and model_price("image") == 0
  and model_price("web_search") == 0
  and model_price("internal_reasoning") == 0;

def has_tools:
  ((.supported_parameters // []) | index("tools")) != null;

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | (.ids[$id] // null) as $old
  | (if $old != null then ($old|usage_row) else null end) as $orow
  | if $orow != null
    then .agg = (.agg | dec_row($P; $orow) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
    if type == "object" then
      . + {size:(.size // 0), off:(.off // 0),
           ids:(.ids // {}), agg:(.agg // empty_agg), last:(.last // null)}
    else empty_file_state end' 2>/dev/null)" || state=""
  [ -z "$state" ] && state="$EMPTY_ST"
  off="$(printf '%s' "$state" | jq -r '.off // 0')"
  case "$off" in ''|*[!0-9]*) off=0 ;; esac
  if [ "$size" -lt "$off" ]; then
    state="$EMPTY_ST"
    off=0
  fi
  tmp="$(mktemp "${TMPDIR:-/tmp}/orc-hud.XXXXXX")" || {
    printf '%s' "$state"
    return 0
  }
  chunk="$tmp.chunk"
  complete="$tmp.ok"
  if [ "$size" -ne "$(printf '%s' "$state" | jq -r '.size // 0')" ] || [ "$size" -ne "$off" ]; then
    if [ "$off" -eq 0 ]; then
      cp "$path" "$chunk" 2>/dev/null || : > "$chunk"
    else
      tail -c +"$((off + 1))" "$path" > "$chunk" 2>/dev/null || : > "$chunk"
    fi
    consumed="$(complete_chunk "$chunk" "$complete")"
    case "$consumed" in ''|*[!0-9]*) consumed=0 ;; esac
    if [ "$consumed" -gt 0 ]; then
      newstate="$(jq -nc --argjson st "$state" --argjson P "$P" '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.
# Constraint: no single quotes here (consumer programs are bash single-quoted).

def n2($x):
  if $x == null then 0
  elif ($x|type)=="number" then $x
  elif ($x|type)=="string" then (($x|tonumber?) // 0)
  else 0 end;

def response_ok:
  ((type)=="object")
  and ((.message // null)|type=="object")
  and ((.message.id // null)|type=="string")
  and ((.message.usage // null)|type=="object")
  and ((.message.model // null)|type=="string")
  and ((.message.model|startswith("<"))|not);

def usage_row:
  { m:  .m,
    i:  n2(.u.input_tokens),
    o:  n2(.u.output_tokens),
    r:  n2(.u.cache_read_input_tokens),
    w5: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_5m_input_tokens)
         else n2(.u.cache_creation_input_tokens) end),
    w1: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_1h_input_tokens)
         else 0 end)};

def row_cost($P):
  ($P[.m] // null) as $pr
  | if ($pr|type)=="object"
    then (.i*n2($pr.p)) + (.o*n2($pr.c)) + (.r*n2($pr.cr))
         + (.w5*n2($pr.w5)) + (.w1*n2($pr.w1h))
    else null end;

def catalog_map:
  .data
  | map({key:.id,
         value:{ctx:(.context_length // 0),
                p:.pricing.prompt, c:.pricing.completion,
                cr:.pricing.input_cache_read,
                w5:.pricing.input_cache_write,
                w1h:.pricing.input_cache_write_1h}})
  | from_entries;

def model_price($f): ((.pricing[$f] // "0") | tonumber);

def is_free:
  model_price("prompt") == 0
  and model_price("completion") == 0
  and model_price("request") == 0
  and model_price("image") == 0
  and model_price("web_search") == 0
  and model_price("internal_reasoning") == 0;

def has_tools:
  ((.supported_parameters // []) | index("tools")) != null;

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | (.ids[$id] // null) as $old
  | (if $old != null then ($old|usage_row) else null end) as $orow
  | if $orow != null
    then .agg = (.agg | dec_row($P; $orow) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
        $st | ingest_lines($P)
      ' "$complete" 2>/dev/null)" || newstate=""
      if [ -n "$newstate" ]; then
        state="$(printf '%s' "$newstate" | jq -c --argjson size "$size" --argjson off "$((off + consumed))" '
          .size = $size | .off = $off' 2>/dev/null || printf '%s' "$newstate")"
      fi
    else
      state="$(printf '%s' "$state" | jq -c --argjson size "$size" '
        .size = $size' 2>/dev/null || printf '%s' "$state")"
    fi
  fi
  state="$(printf '%s' "$state" | jq -c --argjson P "$P" '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.
# Constraint: no single quotes here (consumer programs are bash single-quoted).

def n2($x):
  if $x == null then 0
  elif ($x|type)=="number" then $x
  elif ($x|type)=="string" then (($x|tonumber?) // 0)
  else 0 end;

def response_ok:
  ((type)=="object")
  and ((.message // null)|type=="object")
  and ((.message.id // null)|type=="string")
  and ((.message.usage // null)|type=="object")
  and ((.message.model // null)|type=="string")
  and ((.message.model|startswith("<"))|not);

def usage_row:
  { m:  .m,
    i:  n2(.u.input_tokens),
    o:  n2(.u.output_tokens),
    r:  n2(.u.cache_read_input_tokens),
    w5: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_5m_input_tokens)
         else n2(.u.cache_creation_input_tokens) end),
    w1: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_1h_input_tokens)
         else 0 end)};

def row_cost($P):
  ($P[.m] // null) as $pr
  | if ($pr|type)=="object"
    then (.i*n2($pr.p)) + (.o*n2($pr.c)) + (.r*n2($pr.cr))
         + (.w5*n2($pr.w5)) + (.w1*n2($pr.w1h))
    else null end;

def catalog_map:
  .data
  | map({key:.id,
         value:{ctx:(.context_length // 0),
                p:.pricing.prompt, c:.pricing.completion,
                cr:.pricing.input_cache_read,
                w5:.pricing.input_cache_write,
                w1h:.pricing.input_cache_write_1h}})
  | from_entries;

def model_price($f): ((.pricing[$f] // "0") | tonumber);

def is_free:
  model_price("prompt") == 0
  and model_price("completion") == 0
  and model_price("request") == 0
  and model_price("image") == 0
  and model_price("web_search") == 0
  and model_price("internal_reasoning") == 0;

def has_tools:
  ((.supported_parameters // []) | index("tools")) != null;

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | (.ids[$id] // null) as $old
  | (if $old != null then ($old|usage_row) else null end) as $orow
  | if $orow != null
    then .agg = (.agg | dec_row($P; $orow) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
    reprice($P)
  ' 2>/dev/null || printf '%s' "$state")"
  rm -f "$tmp" "$chunk" "$complete"
  printf '%s' "$state"
}

A="$Z"
LAST_U="null"
LAST_M=""
AGENTS_COST="0"
AGENTS_ON=0
SESSION="$Z"
HAVE_SESSION=0

if [ -n "$P_TX" ] && [ -f "$P_TX" ]; then
  sid="$(basename "$P_TX" .jsonl)"
  cache_path="$SESSION_DIR/$sid.json"
  cache="{}"
  [ -f "$cache_path" ] && cache="$(cat "$cache_path" 2>/dev/null || printf '{}')"
  main_st="$(printf '%s' "$cache" | jq -c '.main // {}' 2>/dev/null || printf '{}')"
  main_st="$(ingest_file "$P_TX" "$main_st")"
  agent_map="$(printf '%s' "$cache" | jq -c '.agents // {}' 2>/dev/null || printf '{}')"
  new_agents="{}"
  agent_dir="${P_TX%.jsonl}/subagents"
  if [ -d "$agent_dir" ]; then
    while IFS= read -r -d '' af; do
      an="$(basename "$af")"
      ast="$(printf '%s' "$agent_map" | jq -c --arg n "$an" '.[$n] // {}' 2>/dev/null || printf '{}')"
      ast="$(ingest_file "$af" "$ast")"
      new_agents="$(printf '%s' "$new_agents" | jq -c --arg n "$an" --argjson st "$ast" '.[$n] = $st' 2>/dev/null || printf '%s' "$new_agents")"
    done < <(find "$agent_dir" -type f -name 'agent-*.jsonl' -print0 2>/dev/null)
  fi
  A="$(printf '%s' "$main_st" | jq -c --argjson z "$Z" '.agg // $z' 2>/dev/null || printf '%s' "$Z")"
  LAST_U="$(printf '%s' "$main_st" | jq -c '.last.u // null' 2>/dev/null || printf 'null')"
  LAST_M="$(printf '%s' "$main_st" | jq -r '.last.m // empty' 2>/dev/null || true)"
  agent_agg="$(printf '%s' "$new_agents" | jq -c --argjson z "$Z" '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.
# Constraint: no single quotes here (consumer programs are bash single-quoted).

def n2($x):
  if $x == null then 0
  elif ($x|type)=="number" then $x
  elif ($x|type)=="string" then (($x|tonumber?) // 0)
  else 0 end;

def response_ok:
  ((type)=="object")
  and ((.message // null)|type=="object")
  and ((.message.id // null)|type=="string")
  and ((.message.usage // null)|type=="object")
  and ((.message.model // null)|type=="string")
  and ((.message.model|startswith("<"))|not);

def usage_row:
  { m:  .m,
    i:  n2(.u.input_tokens),
    o:  n2(.u.output_tokens),
    r:  n2(.u.cache_read_input_tokens),
    w5: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_5m_input_tokens)
         else n2(.u.cache_creation_input_tokens) end),
    w1: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_1h_input_tokens)
         else 0 end)};

def row_cost($P):
  ($P[.m] // null) as $pr
  | if ($pr|type)=="object"
    then (.i*n2($pr.p)) + (.o*n2($pr.c)) + (.r*n2($pr.cr))
         + (.w5*n2($pr.w5)) + (.w1*n2($pr.w1h))
    else null end;

def catalog_map:
  .data
  | map({key:.id,
         value:{ctx:(.context_length // 0),
                p:.pricing.prompt, c:.pricing.completion,
                cr:.pricing.input_cache_read,
                w5:.pricing.input_cache_write,
                w1h:.pricing.input_cache_write_1h}})
  | from_entries;

def model_price($f): ((.pricing[$f] // "0") | tonumber);

def is_free:
  model_price("prompt") == 0
  and model_price("completion") == 0
  and model_price("request") == 0
  and model_price("image") == 0
  and model_price("web_search") == 0
  and model_price("internal_reasoning") == 0;

def has_tools:
  ((.supported_parameters // []) | index("tools")) != null;

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | (.ids[$id] // null) as $old
  | (if $old != null then ($old|usage_row) else null end) as $orow
  | if $orow != null
    then .agg = (.agg | dec_row($P; $orow) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
    reduce (to_entries[] | .value.agg // empty_agg) as $g ($z; merge_agg($g))
  ' 2>/dev/null || printf '%s' "$Z")"
  AGENTS_COST="$(printf '%s' "$agent_agg" | jq -r '.cost // 0')"
  AGENTS_ON="$(printf '%s' "$agent_agg" | jq -r 'if (.msgs // 0) > 0 then 1 else 0 end')"
  launch_ts=""
  [ -f "$LAST_LAUNCH" ] && launch_ts="$(tr -d '[:space:]' < "$LAST_LAUNCH" 2>/dev/null || true)"
  case "$launch_ts" in ''|*[!0-9]*) launch_ts="" ;; esac
  prev_launch="$(printf '%s' "$cache" | jq -r '.launch_ts // empty' 2>/dev/null || true)"
  baseline="$(printf '%s' "$cache" | jq -c --argjson z "$Z" '.baseline // $z' 2>/dev/null || printf '%s' "$Z")"
  if [ -n "$launch_ts" ]; then
    if [ "$launch_ts" != "$prev_launch" ]; then
      baseline="$A"
    fi
    SESSION="$(jq -nc --argjson a "$A" --argjson b "$baseline" '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.
# Constraint: no single quotes here (consumer programs are bash single-quoted).

def n2($x):
  if $x == null then 0
  elif ($x|type)=="number" then $x
  elif ($x|type)=="string" then (($x|tonumber?) // 0)
  else 0 end;

def response_ok:
  ((type)=="object")
  and ((.message // null)|type=="object")
  and ((.message.id // null)|type=="string")
  and ((.message.usage // null)|type=="object")
  and ((.message.model // null)|type=="string")
  and ((.message.model|startswith("<"))|not);

def usage_row:
  { m:  .m,
    i:  n2(.u.input_tokens),
    o:  n2(.u.output_tokens),
    r:  n2(.u.cache_read_input_tokens),
    w5: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_5m_input_tokens)
         else n2(.u.cache_creation_input_tokens) end),
    w1: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_1h_input_tokens)
         else 0 end)};

def row_cost($P):
  ($P[.m] // null) as $pr
  | if ($pr|type)=="object"
    then (.i*n2($pr.p)) + (.o*n2($pr.c)) + (.r*n2($pr.cr))
         + (.w5*n2($pr.w5)) + (.w1*n2($pr.w1h))
    else null end;

def catalog_map:
  .data
  | map({key:.id,
         value:{ctx:(.context_length // 0),
                p:.pricing.prompt, c:.pricing.completion,
                cr:.pricing.input_cache_read,
                w5:.pricing.input_cache_write,
                w1h:.pricing.input_cache_write_1h}})
  | from_entries;

def model_price($f): ((.pricing[$f] // "0") | tonumber);

def is_free:
  model_price("prompt") == 0
  and model_price("completion") == 0
  and model_price("request") == 0
  and model_price("image") == 0
  and model_price("web_search") == 0
  and model_price("internal_reasoning") == 0;

def has_tools:
  ((.supported_parameters // []) | index("tools")) != null;

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | (.ids[$id] // null) as $old
  | (if $old != null then ($old|usage_row) else null end) as $orow
  | if $orow != null
    then .agg = (.agg | dec_row($P; $orow) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
      $a | sub_agg($b)
    ' 2>/dev/null || printf '%s' "$Z")"
    HAVE_SESSION=1
  fi
  mkdir -p "$SESSION_DIR" 2>/dev/null || true
  if jq -nc --argjson main "$main_st" --argjson agents "$new_agents" \
        --argjson base "$baseline" --arg lts "$launch_ts" \
    '{main:$main, agents:$agents, baseline:$base, launch_ts:$lts}' \
    > "$cache_path.tmp" 2>/dev/null
  then
    mv "$cache_path.tmp" "$cache_path" 2>/dev/null || rm -f "$cache_path.tmp"
  else
    rm -f "$cache_path.tmp"
  fi
fi

jq -rn --argjson A "$A" --argjson P "$P" --argjson cu "$P_CU" \
   --argjson S "$SESSION" --argjson agents_cost "$AGENTS_COST" \
   --argjson agents_on "$AGENTS_ON" --argjson have_session "$HAVE_SESSION" \
   --argjson lastu "$LAST_U" --arg lastm "$LAST_M" \
   --arg mid "$P_MID" --arg mname "$P_MNAME" \
   --arg upct "$P_UPCT" --arg csize "$P_CSIZE" '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.
# Constraint: no single quotes here (consumer programs are bash single-quoted).

def n2($x):
  if $x == null then 0
  elif ($x|type)=="number" then $x
  elif ($x|type)=="string" then (($x|tonumber?) // 0)
  else 0 end;

def response_ok:
  ((type)=="object")
  and ((.message // null)|type=="object")
  and ((.message.id // null)|type=="string")
  and ((.message.usage // null)|type=="object")
  and ((.message.model // null)|type=="string")
  and ((.message.model|startswith("<"))|not);

def usage_row:
  { m:  .m,
    i:  n2(.u.input_tokens),
    o:  n2(.u.output_tokens),
    r:  n2(.u.cache_read_input_tokens),
    w5: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_5m_input_tokens)
         else n2(.u.cache_creation_input_tokens) end),
    w1: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_1h_input_tokens)
         else 0 end)};

def row_cost($P):
  ($P[.m] // null) as $pr
  | if ($pr|type)=="object"
    then (.i*n2($pr.p)) + (.o*n2($pr.c)) + (.r*n2($pr.cr))
         + (.w5*n2($pr.w5)) + (.w1*n2($pr.w1h))
    else null end;

def catalog_map:
  .data
  | map({key:.id,
         value:{ctx:(.context_length // 0),
                p:.pricing.prompt, c:.pricing.completion,
                cr:.pricing.input_cache_read,
                w5:.pricing.input_cache_write,
                w1h:.pricing.input_cache_write_1h}})
  | from_entries;

def model_price($f): ((.pricing[$f] // "0") | tonumber);

def is_free:
  model_price("prompt") == 0
  and model_price("completion") == 0
  and model_price("request") == 0
  and model_price("image") == 0
  and model_price("web_search") == 0
  and model_price("internal_reasoning") == 0;

def has_tools:
  ((.supported_parameters // []) | index("tools")) != null;

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | (.ids[$id] // null) as $old
  | (if $old != null then ($old|usage_row) else null end) as $orow
  | if $orow != null
    then .agg = (.agg | dec_row($P; $orow) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
  def num($s): if ($s|type)=="string" and ($s|length)>0 then (($s|tonumber?) // -1) else -1 end;
  def pct($t;$d): if $t >= 0 and $d > 0 then ((($t/$d)*100)|floor) else -1 end;
  def cents($c): (($c*10000)|round);
  $A as $a
  | (if $mname != "" then $mname elif $mid != "" then $mid
     elif $lastm != "" then $lastm else "?" end) as $name
  | (if ($a.unknown|not) and $a.cost == 0
       and (($a.i+$a.o+$a.r+$a.w5+$a.w1) > 0)
     then 1 else 0 end) as $free
  | (num($upct)|if . >= 0 then floor else -1 end) as $p1
  | (if ($cu|type)=="object" then ($cu|ctx_tokens) else -1 end) as $cutok
  | num($csize) as $cs
  | (if ($lastu|type)=="object" then ($lastu|ctx_tokens) else 0 end) as $ctx_est
  | n2(($P[(if $lastm != "" then $lastm else $mid end)] // {}) | (.ctx // 0)) as $mc
  | (if $cs > $mc then $cs else $mc end) as $den
  | (if $p1 >= 0 then $p1 else pct($cutok;$cs) end) as $p2
  | (if $p2 >= 0 then $p2 else pct($ctx_est;$den) end) as $p3
  | (if $p3 > 100 then 100 elif $p3 < 0 then -1 else $p3 end) as $ctxp
  | (($a.i+$a.r+$a.w5+$a.w1)|round) as $tin
  | (($a.i+$a.r+$a.w5+$a.w1)) as $cden
  | (if $cden > 0 then ((($a.r/$cden)*100)|round) else -1 end) as $cachep
  | cents($a.cost) as $c4
  | cents($S.cost) as $sc4
  | cents($agents_cost) as $ac4
  | (if $a.unknown then "u" elif $free == 1 then "f" else "n" end) as $flag
  | (if $have_session == 1 and $flag == "n" and $sc4 > 0 and ($c4 - $sc4) > 1
     then 1 else 0 end) as $show_sess
  | [$name, $tin, ($a.o|round), $cachep, $ctxp, $c4, $sc4, $ac4,
     $flag, $show_sess, $agents_on]
  | map(tostring) | join("")
' 2>/dev/null | {
  IFS=$'\037' read -r R_NAME R_IN R_OUT R_CACHE R_CTX R_COST R_SESS R_AGENTS R_FLAGS R_SHOW_SESS R_AGENTS_ON || true
  human() {
    case "${1:-}" in ''|*[!0-9]*) printf '?'; return ;; esac
    local t="$1"
    if [ "$t" -ge 1000000 ]; then
      printf '%d.%02dM' $(( t / 1000000 )) $(( (t % 1000000) / 10000 ))
    elif [ "$t" -ge 1000 ]; then
      printf '%d.%dk' $(( t / 1000 )) $(( (t % 1000) / 100 ))
    else
      printf '%d' "$t"
    fi
  }
  money() {
    local c4="${1:-0}"
    case "$c4" in ''|*[!0-9-]*) c4=0 ;; esac
    if [ "$c4" -lt 0 ]; then c4=0; fi
    printf '$%d.%04d' $(( c4 / 10000 )) $(( c4 % 10000 ))
  }
  bar() {
    local p="$1" fill="" rem="" i fn
    if [ "$p" -lt 0 ] 2>/dev/null; then printf '░░░░░░░░░░░░'; return; fi
    fn=$(( p * 12 / 100 ))
    [ "$fn" -gt 12 ] && fn=12
    for ((i=0; i<fn; i++)); do fill+='▓'; done
    for ((i=fn; i<12; i++)); do rem+='░'; done
    printf '%s%s' "$fill" "$rem"
  }
  segs=()
  segs+=("${BOLD}${R_NAME:-?}${RST}")
  segs+=("↑ $(human "${R_IN:-0}") ↓ $(human "${R_OUT:-0}")")
  if [ "${R_CACHE:--1}" -ge 0 ] 2>/dev/null; then
    segs+=("cache ${R_CACHE}%")
  fi
  if [ "${R_CTX:--1}" -ge 0 ] 2>/dev/null; then
    c=$GRN
    [ "$R_CTX" -ge 60 ] && c=$YLW
    [ "$R_CTX" -ge 85 ] && c=$RED
    segs+=("ctx ${c}$(bar "$R_CTX")${RST} ${DIM}${R_CTX}%${RST}")
  fi
  case "${R_FLAGS:-n}" in
    u) segs+=("${DIM}\$—${RST}") ;;
    f) segs+=("${GRN}FREE${RST}") ;;
    *)
      if [ "${R_SHOW_SESS:-0}" = "1" ]; then
        segs+=("${BOLD}$(money "${R_SESS:-0}") this${RST} ${DIM}$(money "${R_COST:-0}") file${RST}")
      else
        segs+=("${BOLD}$(money "${R_COST:-0}")${RST}")
      fi
      ;;
  esac
  if [ "${R_AGENTS_ON:-0}" = "1" ]; then
    segs+=("${DIM}+$(money "${R_AGENTS:-0}") agents${RST}")
  fi
  line=""
  sep="${DIM}│${RST}"
  for s in "${segs[@]}"; do
    [ -n "$line" ] && line+="$sep"
    line+="$s"
  done
  printf '%s\n' "$line"
}
exit 0
ORC_HUD_BODY
  } > "$HUD_SCRIPT.tmp" || { rm -f "$HUD_SCRIPT.tmp"; return 1; }
  chmod 755 "$HUD_SCRIPT.tmp" || { rm -f "$HUD_SCRIPT.tmp"; return 1; }
  mv "$HUD_SCRIPT.tmp" "$HUD_SCRIPT" || return 1
}

require_profile() {
  if [ -n "${ORC_PROFILE:-}" ] && ! profile_exists "$ORC_PROFILE"; then
    err "unknown profile: $ORC_PROFILE"
    info "saved profiles: $(jq -r '(.profiles // {}) | keys | join(", ")' "$CONFIG" 2>/dev/null || printf 'none')"
    exit 1
  fi
}

launch_env_lines() {
  local key model small ctx
  key="$(require_key)"
  model="$(resolve model)"
  [ -z "$model" ] && { err "no model configured — run: orc setup"; exit 1; }
  small="$(resolve small_model)"; small="${small:-$model}"
  ctx="$(model_ctx "$model")"
  printf 'export ANTHROPIC_BASE_URL="%s"\n' "$BASE_URL"
  printf 'export ANTHROPIC_AUTH_TOKEN="%s"\n' "$key"
  printf 'export ANTHROPIC_MODEL="%s"\n' "$model"
  printf 'export ANTHROPIC_SMALL_FAST_MODEL="%s"\n' "$small"
  printf 'export ANTHROPIC_DEFAULT_HAIKU_MODEL="%s"\n' "$small"
  printf 'export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1\n'
  printf 'export CLAUDE_CONFIG_DIR="%s"\n' "$CLAUDE_STATE"
  printf 'export ANTHROPIC_API_KEY=""\n'
  [ -n "$ctx" ] && printf 'export CLAUDE_CODE_MAX_CONTEXT_TOKENS="%s"\n' "$ctx"
}

launch() {
  local key model small ctx profile
  require_profile
  key="$(require_key)"
  model="$(resolve model)"
  [ -z "$model" ] && { err "no model configured — run: orc setup"; exit 1; }
  small="$(resolve small_model)"; small="${small:-$model}"
  ctx="$(model_ctx "$model")"
  profile="$(resolved_profile)"
  mkdir -p "$CLAUDE_STATE"
  date +%s > "$LAST_LAUNCH" 2>/dev/null || true
  local envargs=( ANTHROPIC_API_KEY=""
    ANTHROPIC_BASE_URL="$BASE_URL"
    ANTHROPIC_AUTH_TOKEN="$key"
    ANTHROPIC_MODEL="$model"
    ANTHROPIC_SMALL_FAST_MODEL="$small"
    ANTHROPIC_DEFAULT_HAIKU_MODEL="$small"
    CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1
    CLAUDE_CONFIG_DIR="$CLAUDE_STATE" )
  [ -n "$ctx" ] && envargs+=( CLAUDE_CODE_MAX_CONTEXT_TOKENS="$ctx" )
  local forced sl_args=( --arg sl "" )
  if hud_enabled; then
    if install_hud; then
      sl_args=( --arg sl "\"$HUD_SCRIPT\"" )
    else
      info "hud unavailable — launching without statusline"
    fi
  fi
  forced="$(jq -nc "${sl_args[@]}" --arg burl "$BASE_URL" --arg model "$model" --arg small "$small" --arg ctx "$ctx" \
    '{env: ({ANTHROPIC_BASE_URL: $burl, ANTHROPIC_API_KEY: "", ANTHROPIC_MODEL: $model, ANTHROPIC_SMALL_FAST_MODEL: $small, ANTHROPIC_DEFAULT_HAIKU_MODEL: $small}
      + (if $ctx != "" then {CLAUDE_CODE_MAX_CONTEXT_TOKENS: $ctx} else {} end))}
     + (if $sl != "" then {statusLine:{type:"command",command:$sl,padding:0}} else {} end)')"
  local mode; mode="$(orc_mode)" || exit 1
  local modeargs=()
  case "$mode" in
    ""|default) ;;
    yolo|bypassPermissions) modeargs+=( --dangerously-skip-permissions ) ;;
    *) modeargs+=( --permission-mode "$mode" ) ;;
  esac
  info "launching claude via OpenRouter — model: $model · mode: ${mode:-default}${ctx:+ · ctx: $ctx}${profile:+ · profile: @$profile}"
  exec env "${envargs[@]}" claude --settings "$forced" ${modeargs[@]+"${modeargs[@]}"} "$@"
}

stats() {
  local json=0 by="model"
  while [ $# -gt 0 ]; do
    case "$1" in
      --json) json=1 ;;
      --by) shift; by="${1:-}" ;;
      *) err "unknown option: stats $1"; info "usage: orc stats [--json] [--by model|project|day]"; exit 1 ;;
    esac
    shift
  done
  case "$by" in
    model|project|day) ;;
    *) err "--by must be model, project, or day"; exit 1 ;;
  esac
  [ -f "$MODELS_CACHE" ] || fetch_models
  local files=()
  while IFS= read -r -d '' f; do files+=("$f"); done \
    < <(find "$CLAUDE_STATE/projects" -type f -name '*.jsonl' -print0 2>/dev/null)
  if [ "${#files[@]}" -eq 0 ]; then
    info "no transcripts under $CLAUDE_STATE/projects — stats appear after your first orc session"
    exit 0
  fi
  local P="{}"
  P="$(jq -c '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.
# Constraint: no single quotes here (consumer programs are bash single-quoted).

def n2($x):
  if $x == null then 0
  elif ($x|type)=="number" then $x
  elif ($x|type)=="string" then (($x|tonumber?) // 0)
  else 0 end;

def response_ok:
  ((type)=="object")
  and ((.message // null)|type=="object")
  and ((.message.id // null)|type=="string")
  and ((.message.usage // null)|type=="object")
  and ((.message.model // null)|type=="string")
  and ((.message.model|startswith("<"))|not);

def usage_row:
  { m:  .m,
    i:  n2(.u.input_tokens),
    o:  n2(.u.output_tokens),
    r:  n2(.u.cache_read_input_tokens),
    w5: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_5m_input_tokens)
         else n2(.u.cache_creation_input_tokens) end),
    w1: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_1h_input_tokens)
         else 0 end)};

def row_cost($P):
  ($P[.m] // null) as $pr
  | if ($pr|type)=="object"
    then (.i*n2($pr.p)) + (.o*n2($pr.c)) + (.r*n2($pr.cr))
         + (.w5*n2($pr.w5)) + (.w1*n2($pr.w1h))
    else null end;

def catalog_map:
  .data
  | map({key:.id,
         value:{ctx:(.context_length // 0),
                p:.pricing.prompt, c:.pricing.completion,
                cr:.pricing.input_cache_read,
                w5:.pricing.input_cache_write,
                w1h:.pricing.input_cache_write_1h}})
  | from_entries;

def model_price($f): ((.pricing[$f] // "0") | tonumber);

def is_free:
  model_price("prompt") == 0
  and model_price("completion") == 0
  and model_price("request") == 0
  and model_price("image") == 0
  and model_price("web_search") == 0
  and model_price("internal_reasoning") == 0;

def has_tools:
  ((.supported_parameters // []) | index("tools")) != null;

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | if (.ids[$id] // null) != null
    then .agg = (.agg | dec_row($P; (.ids[$id]|usage_row)) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
    catalog_map' "$MODELS_CACHE" 2>/dev/null || printf '{}')"
  [ -z "$P" ] && P="{}"
  local S
  S="$(jq -nc --argjson P "$P" '
# orc pricing/catalog math — single source of truth; spliced by build.sh into marked jq programs.
# Constraint: no single quotes here (consumer programs are bash single-quoted).

def n2($x):
  if $x == null then 0
  elif ($x|type)=="number" then $x
  elif ($x|type)=="string" then (($x|tonumber?) // 0)
  else 0 end;

def response_ok:
  ((type)=="object")
  and ((.message // null)|type=="object")
  and ((.message.id // null)|type=="string")
  and ((.message.usage // null)|type=="object")
  and ((.message.model // null)|type=="string")
  and ((.message.model|startswith("<"))|not);

def usage_row:
  { m:  .m,
    i:  n2(.u.input_tokens),
    o:  n2(.u.output_tokens),
    r:  n2(.u.cache_read_input_tokens),
    w5: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_5m_input_tokens)
         else n2(.u.cache_creation_input_tokens) end),
    w1: (if ((.u.cache_creation // null)|type)=="object"
         then n2(.u.cache_creation.ephemeral_1h_input_tokens)
         else 0 end)};

def row_cost($P):
  ($P[.m] // null) as $pr
  | if ($pr|type)=="object"
    then (.i*n2($pr.p)) + (.o*n2($pr.c)) + (.r*n2($pr.cr))
         + (.w5*n2($pr.w5)) + (.w1*n2($pr.w1h))
    else null end;

def catalog_map:
  .data
  | map({key:.id,
         value:{ctx:(.context_length // 0),
                p:.pricing.prompt, c:.pricing.completion,
                cr:.pricing.input_cache_read,
                w5:.pricing.input_cache_write,
                w1h:.pricing.input_cache_write_1h}})
  | from_entries;

def model_price($f): ((.pricing[$f] // "0") | tonumber);

def is_free:
  model_price("prompt") == 0
  and model_price("completion") == 0
  and model_price("request") == 0
  and model_price("image") == 0
  and model_price("web_search") == 0
  and model_price("internal_reasoning") == 0;

def has_tools:
  ((.supported_parameters // []) | index("tools")) != null;

def empty_agg:
  {i:0,o:0,r:0,w5:0,w1:0,cost:0,unknown:false,msgs:0};

def acc_row($P; $x):
  .i += $x.i | .o += $x.o | .r += $x.r | .w5 += $x.w5 | .w1 += $x.w1
  | .msgs += 1
  | ($x|row_cost($P)) as $c
  | if $c == null
    then (if ($x.i+$x.o+$x.r+$x.w5+$x.w1) > 0 then .unknown = true else . end)
    else .cost += $c end;

def dec_row($P; $x):
  .i -= $x.i | .o -= $x.o | .r -= $x.r | .w5 -= $x.w5 | .w1 -= $x.w1
  | .msgs -= 1
  | ($x|row_cost($P)) as $c
  | if $c == null then . else .cost -= $c end;

def merge_agg($b):
  .i += $b.i | .o += $b.o | .r += $b.r | .w5 += $b.w5 | .w1 += $b.w1
  | .msgs += $b.msgs | .cost += $b.cost
  | .unknown = (.unknown or $b.unknown);

def sub_agg($b):
  .i -= $b.i | .o -= $b.o | .r -= $b.r | .w5 -= $b.w5 | .w1 -= $b.w1
  | .msgs -= $b.msgs | .cost -= $b.cost
  | .unknown = (.unknown or $b.unknown);

def apply_msg($P; $id; $nu):
  ($nu|usage_row) as $nr
  | if (.ids[$id] // null) != null
    then .agg = (.agg | dec_row($P; (.ids[$id]|usage_row)) | acc_row($P; $nr))
    else .agg = (.agg | acc_row($P; $nr)) end
  | .ids[$id] = $nu
  | .last = $nu;

def ingest_lines($P):
  reduce inputs as $r (.;
    if ($r|response_ok)
    then apply_msg($P; $r.message.id; {m:$r.message.model, u:$r.message.usage})
    else . end);

def ctx_tokens:
  if . == null then 0
  else
    (n2(.input_tokens) + n2(.cache_read_input_tokens))
    + (if ((.cache_creation // null)|type)=="object"
       then n2(.cache_creation.ephemeral_5m_input_tokens)
          + n2(.cache_creation.ephemeral_1h_input_tokens)
       else n2(.cache_creation_input_tokens) end)
  end;

def empty_file_state:
  {size:0, off:0, ids:{}, agg:empty_agg, last:null};

def reprice($P):
  .agg = reduce (.ids | to_entries[] | .value | usage_row) as $x (empty_agg; acc_row($P; $x));
    def summarize:
      { msgs: length,
        i: (map(.i) | add // 0),
        o: (map(.o) | add // 0),
        r: (map(.r) | add // 0),
        w: (map(.w5 + .w1) | add // 0),
        cost: ((map(.cost // 0) | add // 0) * 1000000 | round / 1000000),
        unpriced: (map(select(.cost == null)) | length) };
    [ reduce inputs as $rec ({};
        if ($rec|response_ok)
        then .[$rec.message.id] = {
            m: $rec.message.model,
            day: ((($rec.timestamp // "")[0:10]) as $d | if $d == "" then "?" else $d end),
            project: (input_filename | try (capture("projects/(?<p>[^/]+)/").p) catch "?"),
            u: $rec.message.usage }
        else . end)
      | .[] ]
    | map(usage_row + {day:.day, project:.project})
    | map(. + {cost: row_cost($P)})
    | { messages: length,
        total: summarize,
        by_model:   (group_by(.m)       | map({key: .[0].m}       + summarize) | sort_by(-.cost)),
        by_project: (group_by(.project) | map({key: .[0].project} + summarize) | sort_by(-.cost)),
        by_day:     (group_by(.day)     | map({key: .[0].day}     + summarize) | sort_by(.key)) }
  ' "${files[@]}")" || { err "failed to parse transcripts under $CLAUDE_STATE/projects"; exit 1; }
  if [ "$json" = "1" ]; then
    printf '%s\n' "$S" | jq .
    exit 0
  fi
  printf '%s' "$S" | jq -r --arg by "$by" '
    def hum:
      if . >= 1000000 then "\((. / 10000 | round) / 100)M"
      elif . >= 1000 then "\((. / 100 | round) / 10)k"
      else tostring end;
    def money: "$\(. * 10000 | round / 10000)";
    def cachep: ((.i + .r + .w) as $d | if $d > 0 then "\((.r / $d * 100) | round)%" else "-" end);
    def line($k): [ $k, (.msgs | tostring), (.i + .r + .w | hum), (.o | hum), cachep,
                    ((.cost | money) + (if .unpriced > 0 then "+?" else "" end)) ] | @tsv;
    (if $by == "model" then .by_model elif $by == "project" then .by_project else .by_day end) as $rows
    | ([($by | ascii_upcase), "MSGS", "IN", "OUT", "CACHE", "COST"] | @tsv),
      ($rows[] | line(.key)),
      (.total | line("TOTAL"))
  ' | table
  local unpriced
  unpriced="$(printf '%s' "$S" | jq -r '.total.unpriced')"
  if [ "$unpriced" != "0" ]; then
    info "$unpriced response(s) from models missing in the cached catalog were not priced (+?) — try: orc refresh"
  fi
  exit 0
}

or_post() {
  local key="$1" payload="$2"
  curl -sS --max-time 45 \
    -H "Authorization: Bearer $key" \
    -H "anthropic-version: 2023-06-01" \
    -H "content-type: application/json" \
    -w '\n%{http_code} %{time_total}' \
    -d "$payload" "$API/messages" 2>&1
}

split_curl() {
  local raw="$1"
  _OR_META="$(printf '%s\n' "$raw" | tail -n1)"
  _OR_BODY="$(printf '%s\n' "$raw" | sed '$d')"
  _OR_HTTP="${_OR_META%% *}"
  _OR_SECS="${_OR_META#* }"
  case "$_OR_HTTP" in ''|*[!0-9]*) _OR_HTTP="000"; _OR_BODY="$raw" ;; esac
}

record_fit() {
  local model="$1" ok="$2" http="$3" ttft="$4" loop="$5" errm="$6"
  mkdir -p "$ORC_HOME"
  local tmp="$FIT_CACHE.tmp" now
  now="$(date +%s)"
  if [ ! -f "$FIT_CACHE" ]; then printf '{}\n' > "$FIT_CACHE"; fi
  jq --arg m "$model" --argjson ok "$ok" --arg http "$http" \
     --arg ttft "$ttft" --arg loop "$loop" --arg err "$errm" --argjson now "$now" '
    .[$m] = {ok:$ok, http:($http|tonumber? // 0), ttft:($ttft|tonumber? // 0),
             tool_roundtrip:($loop|tonumber? // null),
             error:(if $err == "" then null else $err end),
             checked_at:$now}
  ' "$FIT_CACHE" > "$tmp" && mv "$tmp" "$FIT_CACHE"
}

probe_model() {
  local fit=0 model=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --fit) fit=1 ;;
      --*) err "unknown option: probe $1"; info "usage: orc probe [--fit] [model]"; return 1 ;;
      *) model="$1" ;;
    esac
    shift
  done
  [ -z "$model" ] && model="$(resolve model)"
  if [ -z "$model" ]; then
    err "no model to probe — pass one: orc probe <model-id>"
    return 1
  fi
  if [ "$fit" = "1" ]; then
    probe_fit "$model"
    return $?
  fi
  local key raw
  key="$(require_key)"
  info "probing $model — POST $API/messages (max_tokens: 1)..."
  if ! raw="$(or_post "$key" "$(jq -nc --arg m "$model" '{model:$m, max_tokens:1, messages:[{role:"user", content:"ping"}]}')")"; then
    err "probe failed: $raw"
    return 1
  fi
  split_curl "$raw"
  local err_msg
  err_msg="$(printf '%s' "$_OR_BODY" | jq -r '.error.message // empty' 2>/dev/null || true)"
  if [ "$_OR_HTTP" = "200" ] && [ -z "$err_msg" ]; then
    bold "probe ok: $model — HTTP 200 in ${_OR_SECS}s (launch path verified)"
    return 0
  fi
  if [ -n "$err_msg" ]; then
    err "probe failed: $model — HTTP $_OR_HTTP: $err_msg"
  else
    err "probe failed: $model — HTTP $_OR_HTTP: $(printf '%s' "$_OR_BODY" | head -c 200)"
  fi
  return 1
}

probe_fit() {
  local model="$1" key raw err_msg tool_id
  key="$(require_key)"
  info "fit-probing $model — forced tool_use then tool_result..."
  local req
  req="$(jq -nc --arg m "$model" '{
    model:$m, max_tokens:128,
    tools:[{name:"orc_ping", description:"Return a one-word acknowledgement.",
            input_schema:{type:"object", properties:{note:{type:"string"}}, required:["note"]}}],
    tool_choice:{type:"tool", name:"orc_ping"},
    messages:[{role:"user", content:"Call orc_ping with note=ok."}]
  }')"
  if ! raw="$(or_post "$key" "$req")"; then
    err "fit probe failed: $raw"
    record_fit "$model" false 0 "" "" "$raw"
    return 1
  fi
  split_curl "$raw"
  local ttft="$_OR_SECS" http1="$_OR_HTTP"
  err_msg="$(printf '%s' "$_OR_BODY" | jq -r '.error.message // empty' 2>/dev/null || true)"
  if [ "$http1" != "200" ] || [ -n "$err_msg" ]; then
    err "fit probe failed: $model — HTTP $http1: ${err_msg:-$(printf '%s' "$_OR_BODY" | head -c 200)}"
    record_fit "$model" false "$http1" "$ttft" "" "${err_msg:-http $http1}"
    return 1
  fi
  tool_id="$(printf '%s' "$_OR_BODY" | jq -r '
    [.content[]? | select(.type=="tool_use" and .name=="orc_ping") | .id] | .[0] // empty
  ' 2>/dev/null || true)"
  if [ -z "$tool_id" ]; then
    err "fit probe failed: $model — no orc_ping tool_use in the first response"
    record_fit "$model" false "$http1" "$ttft" "" "no tool_use"
    return 1
  fi
  local follow
  follow="$(jq -nc --arg m "$model" --arg id "$tool_id" --argjson first "$_OR_BODY" '{
    model:$m, max_tokens:32,
    tools:[{name:"orc_ping", description:"Return a one-word acknowledgement.",
            input_schema:{type:"object", properties:{note:{type:"string"}}, required:["note"]}}],
    messages:[
      {role:"user", content:"Call orc_ping with note=ok."},
      {role:"assistant", content:($first.content // [])},
      {role:"user", content:[{type:"tool_result", tool_use_id:$id, content:"ok"}]}
    ]
  }')"
  if ! raw="$(or_post "$key" "$follow")"; then
    err "fit probe failed on tool_result: $raw"
    record_fit "$model" false 0 "$ttft" "" "$raw"
    return 1
  fi
  split_curl "$raw"
  err_msg="$(printf '%s' "$_OR_BODY" | jq -r '.error.message // empty' 2>/dev/null || true)"
  if [ "$_OR_HTTP" = "200" ] && [ -z "$err_msg" ]; then
    bold "fit ok: $model — tool loop in ${ttft}s + ${_OR_SECS}s"
    record_fit "$model" true "$_OR_HTTP" "$ttft" "$_OR_SECS" ""
    return 0
  fi
  err "fit probe failed: $model — HTTP $_OR_HTTP after tool_result: ${err_msg:-$(printf '%s' "$_OR_BODY" | head -c 200)}"
  record_fit "$model" false "$_OR_HTTP" "$ttft" "$_OR_SECS" "${err_msg:-http $_OR_HTTP}"
  return 1
}

doctor() {
  bold "orc doctor"
  local ok=0
  if command -v claude >/dev/null 2>&1; then
    printf '  ✓ claude: %s (%s)\n' "$(command -v claude)" "$(claude --version 2>/dev/null | head -1)"
  else
    printf '  ✗ claude not found on PATH\n'; ok=1
  fi
  for dep in jq curl fzf; do
    if command -v "$dep" >/dev/null 2>&1; then printf '  ✓ %s\n' "$dep"; else printf '  ✗ %s missing\n' "$dep"; ok=1; fi
  done
  local src; src="$(key_source)"
  if [ "$src" = "none" ]; then
    printf '  ✗ API key: not found (env $%s or stored key) — run: orc key\n' "$(key_env_name)"; ok=1
  else
    printf '  ✓ API key: %s\n' "$src"
    local resp
    if resp="$(curl -sf --max-time 15 -H "Authorization: Bearer $(resolve_key)" "$API/key" 2>/dev/null)"; then
      printf '  ✓ key valid — label: %s, usage: $%s\n' \
        "$(printf '%s' "$resp" | jq -r '.data.label // "?"')" \
        "$(printf '%s' "$resp" | jq -r '.data.usage // 0')"
    else
      printf '  ✗ key rejected by OpenRouter (%s/key)\n' "$API"; ok=1
    fi
  fi
  local model small profile project
  model="$(resolve model)"
  small="$(resolve small_model)"
  profile="$(resolved_profile)"
  project="${PROJECT_CFG_FILE:-}"
  if [ -z "$model" ]; then
    printf '  ✗ no model configured — run: orc setup\n'; ok=1
  else
    fetch_models
    printf '  · resolved from %s%s%s\n' "$(resolve_src model)" \
      "${profile:+ · @$profile}" "${project:+ · $project}"
    if model_exists "$model"; then
      if model_has_tools "$model"; then
        printf '  ✓ model: %s  (tools · fit: %s)\n' "$model" "$(fit_of "$model")"
      else
        printf '  ! model %s does not advertise tool support — Claude Code leans hard on tools\n' "$model"
        printf '    expect degraded behavior; pick another with: orc model\n'
      fi
    else
      printf '  ! model %s not in current OpenRouter list (may be delisted) — run: orc model\n' "$model"
    fi
  fi
  if [ -n "$small" ]; then printf '  ✓ small model: %s\n' "$small"; fi
  if [ "${ORC_NO_PROBE:-}" = "1" ]; then
    printf '  - launch probe skipped (ORC_NO_PROBE=1)\n'
  elif [ "$src" != "none" ] && [ -n "${model:-}" ]; then
    local prc=0 probe_out
    probe_out="$(probe_model "$model" 2>&1)" || prc=1
    printf '%s\n' "$probe_out" | sed 's/^/  /'
    [ "$prc" = "1" ] && ok=1
    if [ "$prc" = "0" ] && [ "${ORC_NO_FIT:-}" != "1" ]; then
      if [ "$(fit_of "$model")" = "UNTESTED" ]; then
        probe_out="$(probe_fit "$model" 2>&1)" || prc=1
        printf '%s\n' "$probe_out" | sed 's/^/  /'
        [ "$prc" = "1" ] && ok=1
      else
        printf '  · fit cache: %s\n' "$(fit_of "$model")"
      fi
    fi
  fi
  printf '  ✓ claude state dir: %s (isolated from ~/.claude)\n' "$CLAUDE_STATE"
  exit "$ok"
}

usage() {
  cat >&2 <<'EOF'
orc — run Claude Code against OpenRouter models

usage:
  orc                     launch (first run starts the setup wizard)
  orc [claude args...]    launch and pass args through to claude (e.g. orc -c, orc -p "...")
  orc -m <model> [...]    one-off model override (not saved)
  orc @<profile> [...]    launch with a saved profile (one-off, not saved as default)
  orc setup               re-run the setup wizard (key + model)
  orc model [query]       pick + save a new default model (fzf)
  orc model --set <id>    set the default model non-interactively
  orc free [query]        pick + save a currently free model (fzf)
  orc mode                pick + save the launch permission mode
  orc small [query]       pick + save the small/fast background model
  orc small --set <id>    set the small model non-interactively
  orc save <name>         snapshot the resolved model/small/mode as profile @<name>
  orc profiles [rm <n>]   list saved profiles / remove one
  orc status [--json]     print the resolved launch (model/mode/profile/source)
  orc stats               token + cost totals across all orc transcripts
       [--json] [--by model|project|day]
  orc models [query]      list models with pricing + context + tool + fit
  orc models --free [...] list only currently free models
  orc models --free --tools list free models that advertise tool support
  orc models --tools [..] list only models advertising tool support
  orc models --fit [...]  list only models that passed orc probe --fit
  orc hud [on|off|demo]   toggle the statusline HUD / preview it on newest transcript
  orc key                 configure key source (env var name or key store)
  orc env                 print the export lines launch uses (contains your key)
  orc refresh             force-refresh the cached model list
  orc doctor              check everything end to end (resolved model + fit)
  orc fusion [...]        run the Fusion/Ultra orchestration layer
  orc probe [model]       smoke-test the launch path (1-token request; default: resolved model)
  orc probe --fit [model] tool-loop smoke test; result cached 24h as FIT/FAIL
  orc config              open config in $EDITOR

project config:
  .orc.json               found upward from $PWD; keys: profile, model,
                          small_model, mode — pins settings per project

env:
  ORC_YES=1               skip the launch confirmation prompt
  ORC_HOME                config dir (default ~/.config/orc)
  ORC_MODE                per-launch permission-mode override (does not change saved config)
  ORC_PROFILE             per-launch profile override (same as orc @<name>)
  ORC_HUD=0               disable the statusline HUD for one launch
  ORC_NO_PROBE=1          skip the launch probe in orc doctor
  ORC_NO_FIT=1            skip the tool-loop fit probe in orc doctor
  ORC_MODEL_OVERRIDE      same as -m; used internally by the launch menu
EOF
}

case "${1:-}" in
  help|-h|--help) usage; exit 0 ;;
  fusion)
    shift
    fusion_bin="$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")/fusion"
    if [ ! -x "$fusion_bin" ]; then
      fusion_bin="$(command -v fusion || true)"
    fi
    [ -x "$fusion_bin" ] || { err "fusion is not installed beside orc and is not on PATH"; exit 127; }
    exec "$fusion_bin" "$@" ;;
  setup) setup_wizard; exit 0 ;;
  key) key_wizard; exit 0 ;;
  doctor) doctor ;;
  status)
    shift
    status_cmd "${1:-}"
    exit 0 ;;
  probe)
    shift
    probe_model "$@" ;;
  refresh) fetch_models force; fetch_quality force; info "model list and quality cache refreshed"; exit 0 ;;
  quality)
    shift
    quality_cmd "$@"
    exit 0 ;;
  config) mkdir -p "$ORC_HOME"; [ -f "$CONFIG" ] || printf '{}\n' > "$CONFIG"; exec "${EDITOR:-vi}" "$CONFIG" ;;
  model)
    shift
    if [ "${1:-}" = "--set" ]; then
      shift
      [ -z "${1:-}" ] && { err "usage: orc model --set <model-id>"; exit 1; }
      fetch_models
      if model_exists "$1"; then
        save_cfg model "$1"; info "model saved: $1"
      else
        err "model not in the current OpenRouter catalog: $1"
        info "try: orc refresh — or 'orc -m $1' to launch with it anyway"
        exit 1
      fi
      exit 0
    fi
    if m="$(pick_model "${1:-}" "model")"; then save_cfg model "$m"; info "model saved: $m"; fi
    exit 0 ;;
  save)
    shift
    [ -z "${1:-}" ] && { err "usage: orc save <name>"; exit 1; }
    save_profile "$1"
    exit 0 ;;
  profiles)
    shift
    profiles_cmd "$@"
    exit 0 ;;
  stats)
    shift
    stats "$@" ;;
  free)
    shift
    if m="$(pick_model "${1:-}" "free model" "free")"; then save_cfg model "$m"; info "model saved: $m"; fi
    exit 0 ;;
  mode) pick_mode || info "mode unchanged"; exit 0 ;;
  hud)
    case "${2:-}" in
      on) save_cfg hud on; info "hud on" ;;
      off) save_cfg hud off; info "hud off" ;;
      demo)
        install_hud || { err "could not generate $HUD_SCRIPT"; exit 1; }
        tp="$(find "$CLAUDE_STATE/projects" -type f -name '*.jsonl' -print0 2>/dev/null \
              | mtime_lines 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2- || true)"
        [ -z "$tp" ] && { err "no transcripts found under $CLAUDE_STATE/projects"; exit 1; }
        info "demo — rendering newest transcript: $tp"
        jq -nc --arg tp "$tp" --arg cwd "$PWD" \
          '{session_id:"demo",transcript_path:$tp,
            model:{id:"demo-model",display_name:"(demo)"},
            workspace:{current_dir:$cwd},context_window:null}' \
          | "$HUD_SCRIPT"
        ;;
      "") if hud_enabled; then bold "hud: on"; else bold "hud: off"; fi
          info "usage: orc hud [on|off|demo]" ;;
      *) err "unknown option: hud ${2:-}"; info "usage: orc hud [on|off|demo]"; exit 1 ;;
    esac
    exit 0 ;;
  small)
    shift
    if [ "${1:-}" = "--set" ]; then
      shift
      [ -z "${1:-}" ] && { err "usage: orc small --set <model-id>"; exit 1; }
      fetch_models
      if model_exists "$1"; then
        save_cfg small_model "$1"; info "small model saved: $1"
      else
        err "model not in the current OpenRouter catalog: $1"
        info "try: orc refresh"
        exit 1
      fi
      exit 0
    fi
    if s="$(pick_model "${1:-}" "small model")"; then save_cfg small_model "$s"; info "small model saved: $s"; fi
    exit 0 ;;
  models)
    shift
    fetch_models
    fetch_quality
    model_filter="all"
    want_free=0; want_tools=0; want_fit=0; query=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --free|free) want_free=1 ;;
        --tools|tools) want_tools=1 ;;
        --fit|fit) want_fit=1 ;;
        --json) err "models --json is not available in this release"; exit 2 ;;
        *) query="$1" ;;
      esac
      shift
    done
    if [ "$want_fit" = "1" ]; then model_filter="fit"
    elif [ "$want_free" = "1" ] && [ "$want_tools" = "1" ]; then model_filter="free-tools"
    elif [ "$want_free" = "1" ]; then model_filter="free"
    elif [ "$want_tools" = "1" ]; then model_filter="tools"
    fi
    if [ -n "$query" ]; then model_rows "$model_filter" | grep -i -- "$query" | table
    else model_rows "$model_filter" | table
    fi
    exit 0 ;;
  env)
    require_profile
    launch_env_lines
    exit 0 ;;
  -m)
    shift
    [ -z "${1:-}" ] && { err "-m requires a model id"; exit 1; }
    ORC_MODEL_OVERRIDE="$1"; shift
    launch "$@" ;;
  @*)
    ORC_PROFILE="${1#@}"; shift
    [ -z "$ORC_PROFILE" ] && { err "usage: orc @<profile> [claude args...]"; exit 1; }
    launch "$@" ;;
  "")
    if [ -z "$(resolve model)" ]; then setup_wizard; fi
    confirm_or_change
    launch ;;
  *)
    launch "$@" ;;
esac
