# 🧌 orc — OpenRouter × Claude

Run Claude Code against any OpenRouter model — GPT, Gemini, Kimi, DeepSeek, free
stealth previews like `stealth/ox-alpha` — with one command. `orc` handles the
env wiring, model selection, key management, and keeps its Claude state fully
isolated from your normal Anthropic login.

## Requirements

- macOS or Linux
- [Claude Code](https://claude.com/claude-code) (`claude` on PATH)
- `jq`, `curl`, `fzf` — `brew install jq fzf` on macOS, e.g.
  `sudo apt-get install jq curl fzf` on Debian/Ubuntu
- An [OpenRouter](https://openrouter.ai) API key

## Install

```bash
./install.sh
```

Copies `orc` to `~/.local/bin` (override with `DEST=/somewhere ./install.sh`)
and checks dependencies. Then:

```bash
orc
```

First run launches the setup wizard: it finds your API key (or helps you set
one up), fetches the live OpenRouter model catalog, and gives you an fzf
picker with per-model pricing and context sizes. It also offers a launch
permission mode (default / auto / acceptEdits / plan / dontAsk / yolo).
Choices are saved; the next run shows the resolved model + mode (and
`@profile` / `.orc.json` when those apply) and lets you launch with Enter
or change this invocation first:

```
orc → stealth/ox-alpha · mode: auto · @work · .orc.json
  [Enter] launch   [m] model   [a] profile   [f] free   [p] mode   [s] save   [k] key   [q]
```

`[m]` / `[p]` write the global default unless a profile or `.orc.json` is
in effect — then they override this launch only. `[s]` snapshots the
resolved combo as a named profile.

## Usage

```
orc                     launch (first run starts the setup wizard)
orc [claude args...]    pass args through to claude (orc -c, orc -p "...", orc --resume)
orc -m <model> [...]    one-off model override (not saved)
orc @<profile> [...]    launch with a saved profile (one-off, not saved as default)
orc setup               re-run the setup wizard (key + model)
orc model [query]       pick + save a new default model
orc model --set <id>    set the default model non-interactively
orc free [query]        pick + save a currently free model
orc mode                pick + save the launch permission mode
orc small [query]       pick + save a small/fast model for background tasks
orc small --set <id>    set the small model non-interactively
orc save <name>         snapshot the resolved model/small/mode as profile @<name>
orc profiles [rm <n>]   list saved profiles / remove one
orc status [--json]     print the resolved launch (model/mode/profile/source)
orc stats               token + cost totals across all orc transcripts
     [--json] [--by model|project|day]
orc hud [on|off|demo]   toggle the statusline HUD / preview it on the newest transcript
orc models [query]      list models with pricing + context + tool + fit
orc models --free [...] list only currently free models
orc models --tools [..] list only models advertising tool support
orc models --fit [...]  list only models that passed orc probe --fit
orc quality             dump the Artificial Analysis quality cache as a table
orc key                 configure key source (env var name or key store)
orc env                 print the export lines launch uses (contains your key)
orc refresh             force-refresh the cached model list
orc doctor              check everything end to end (resolved model + fit)
orc probe [model]       smoke-test the launch path (1-token request)
orc probe --fit [model] tool-loop smoke test; result cached 24h as FIT/FAIL
orc config              open config in $EDITOR
```

The model picker shows live OpenRouter input/output prices per million tokens.
Free models are marked `FREE`; type `FREE` in the regular picker or use
`orc free` to search only models whose current usage prices are all zero.
Every row also carries a tool-support flag read from the catalog's
`supported_parameters`: models marked `NO TOOLS` tend to feel broken under
Claude Code, which leans hard on tools. `orc models --tools` lists only
tool-capable models, and `orc doctor` warns when the *resolved* model
(not just the global default) lacks tool support.

**Ranking.** The picker is sorted by the Artificial Analysis
Intelligence Index, a 0–~63 score for general model capability.
Within a score, ties break on prompt price (cheaper first), then
`FIT` over `UNTESTED` over `FAIL`, then model id. A bundled snapshot
of the leaderboard ships with orc at `data/quality.json`; on first
launch it is copied into `$ORC_HOME/quality.json` and used for
ranking without any network call. `orc refresh` re-fetches both
the OpenRouter catalog and the quality snapshot; `make refresh-quality`
(from the orc source) re-runs the cmndcntr fetcher and copies a
fresh snapshot into `data/quality.json` for the next release.

Use `orc quality` to inspect the current cache as a sorted table:

```
OR_ID                            II    CODE   AGENT  CREATOR          SLUG
anthropic/claude-opus-5          63.05  77.98  59.17  Anthropic        claude-opus-5
anthropic/claude-fable-5         62.07  76.49  56.59  Anthropic        claude-fable-5
openai/gpt-5.6-sol               60.92  77.38  57.78  OpenAI           gpt-5.6-sol
x-ai/grok-4.6                    60.92  76.78  58.67  SpaceXAI         grok-4-6
...
```

A second column, `FIT` / `FAIL` / `UNTESTED`, is orc's own measurement:
`orc probe --fit` forces a one-tool round trip through OpenRouter's
Anthropic-compatible endpoint and caches the result for 24h. The picker
sorts last-known-good models first; `orc models --fit` lists only those.
`orc doctor` runs the fit probe when the cache is empty (`ORC_NO_FIT=1`
skips it). Catalog `tools` is an advertisement; FIT is whether the model
survived a Claude Code-shaped loop.

## Profiles

A profile is a named snapshot of the *resolved* `model` + `small_model` +
`mode` — including a one-off `-m`, `ORC_MODE`, or `.orc.json` pin. Keep one
combo for real work and one for throwaway experiments, and switch per launch
without touching your saved default. `orc status` prints the combo that
would launch from this directory. `orc env` prints the same exports
`launch` would set (including context window and gateway discovery).

```bash
orc status              # what would launch right now
orc status --json       # same object, for wrappers
```

Resolution order for each of `model` / `small_model` / `mode`:

1. one-off flags: `-m` / `ORC_MODEL_OVERRIDE` / `ORC_MODE`
2. `orc @<profile>` / `ORC_PROFILE`
3. `.orc.json` inline keys
4. the profile named by `.orc.json`'s `"profile"`
5. `~/.config/orc/config.json`

```bash
orc save work           # snapshot the current setup as @work
orc @work               # launch with it (default config unchanged)
orc @work -c            # profile + claude args compose
orc profiles            # list; orc profiles rm work removes
```

`ORC_PROFILE=work orc` is equivalent to `orc @work` — useful for wrappers.

## Per-project config: .orc.json

Drop a `.orc.json` in a repo (found by walking up from the current directory)
to pin settings for that project:

```json
{ "profile": "work" }
```

or inline, without needing a profile:

```json
{ "model": "moonshotai/kimi-k2", "mode": "plan" }
```

Resolution is the same stack `orc status` prints — see above. The launch
menu, `orc env`, `orc save`, `orc doctor`, and `orc probe` all consume
that object, so a project pin or `@work` is never silently ignored.

## Stats

`orc stats` aggregates every transcript orc has ever produced (they all live
under orc's isolated state dir) and prices them against the cached catalog —
the same math as the HUD, across all sessions, subagent transcripts included:

```
MODEL                     MSGS  IN       OUT     CACHE  COST
anthropic/claude-opus-5   185   11.74M   143.7k  93%    $13.8608
stealth/ox-alpha          1024  184.86M  533.3k  94%    $0
TOTAL                     1246  200.48M  737.5k  94%    $24.5181
```

`--by project` or `--by day` regroups the table; `--json` emits the full
structured breakdown (totals plus all three groupings) for scripts. Responses
from models missing from the cached catalog are flagged `+?` rather than
silently priced at zero. Unlike the OpenRouter dashboard, this splits spend
per project and per model as seen from your machine.

## How the key is resolved

1. The env var named in your config — `OPENROUTER_API_KEY` by default,
   changeable via `orc key`.
2. The system key store, where the key wizard stores pasted keys: the macOS
   Keychain (service `orc-openrouter`) on macOS; on Linux a file at
   `~/.config/orc/key`, created with `0600` permissions and never made
   group/world readable (orc warns if it is). Nothing is ever written to a
   plaintext config file.

If neither is found, **orc refuses to launch** (fail closed) and tells you how
to fix it.

`orc doctor` goes further than static checks: as a final step it probes the
real launch path — a 1-token request to OpenRouter's Anthropic-compatible
endpoint (`/api/v1/messages`) with your resolved key and saved model — and
reports HTTP status and latency, so breakage surfaces before you are inside a
session. The probe costs a fraction of a cent on paid models; skip it with
`ORC_NO_PROBE=1`, or run it standalone against any model with `orc probe <id>`.

## What it sets for Claude Code

- `ANTHROPIC_BASE_URL` → OpenRouter's Anthropic-compatible endpoint
- `ANTHROPIC_AUTH_TOKEN` → your resolved key (`ANTHROPIC_API_KEY` is set to
  an empty string to avoid conflicts)
- `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` → your saved models
- `CLAUDE_CODE_MAX_CONTEXT_TOKENS` → the model's real context window from the
  OpenRouter catalog (Claude Code otherwise assumes 200k for unknown models)
- `CLAUDE_CONFIG_DIR` → `~/.config/orc/claude-state`, so orc never touches
  your real `~/.claude` login and you never have to `/logout` between
  Anthropic and OpenRouter sessions

The base URL and model are additionally forced via `claude --settings` (CLI
settings outrank directory settings), so a project-level
`.claude/settings.json` with its own `env.ANTHROPIC_BASE_URL` — a proxy, a
gateway — can't silently hijack an orc session.

## The HUD

Every orc launch injects a Claude Code statusline (on by default, your real
`~/.claude` settings are never touched). It renders one line:

```
stealth/ox-alpha │ ↑ 3.50M ↓ 45.8k │ cache 89% │ ctx ▓▓░░░░░░░░░ 11% │ $0.8412 │ +$0.31 agents
```

- **↑ / ↓** — total tokens in (including cached) and out on the parent
  transcript. Subagent tokens are priced separately so the parent window
  stays honest.
- **cache** — share of parent input tokens served from prompt cache
- **ctx** — context gauge of the parent window; green under 60%, yellow
  under 85%, red at the top. Prefers Claude Code's own context reading,
  falls back to the last parent API call over the catalog context length
- **cost** — parent-transcript cost from usage × live OpenRouter catalog
  prices, joined per response (mid-session model switches price correctly).
  Shows `FREE` for zero-priced models, `$—` when a model isn't in the
  cached catalog. After `orc -c` / `/resume`, a second figure appears:
  `$0.12 this $1.24 file` — spend since this join vs the whole file
- **+agents** — sibling spend from `<session>/subagents/agent-*.jsonl`,
  same math as `orc stats`. Shown only when a subagent has billed tokens

The HUD keeps an incremental cache at `~/.config/orc/sessions/<id>.json`
and only reads new bytes on each statusline tick, so a multi-megabyte
transcript does not get fully reparsed every time. `orc` stamps
`~/.config/orc/last-launch` at exec so a resume can split "this join"
from "the file".

Claude Code's built-in cost figure is deliberately ignored: it prices tokens
at Anthropic list rates, which is wrong when you're billed OpenRouter rates.

The script is generated at `~/.config/orc/hud.sh` on launch (self-contained
bash + jq, no extra dependencies); its source of truth is `hud.sh` in this
repo. Toggle with `orc hud off` / `orc hud on`, preview against your newest
transcript with `orc hud demo`, or disable for a single launch with
`ORC_HUD=0`.

The HUD now folds sibling subagent transcripts and splits resume spend
as `$this` vs `$file`. Compacted / rewritten transcript files reset the
byte-offset cache automatically.

## Config

`~/.config/orc/config.json`:

```json
{
  "key_env": "OPENROUTER_API_KEY",
  "model": "stealth/ox-alpha",
  "small_model": "google/gemini-2.5-flash",
  "hud": "on",
  "profiles": {
    "work": { "model": "anthropic/claude-opus-5", "mode": "plan" }
  }
}
```

`ORC_YES=1` skips the launch confirmation (for scripts). `ORC_HOME` moves the
config dir. `ORC_HUD=0` hides the statusline HUD for one invocation.
`ORC_NO_PROBE=1` skips doctor's launch probe. `ORC_NO_FIT=1` skips doctor's
tool-loop fit probe. `ORC_MODE` overrides the launch permission mode for
one invocation (`default` / `auto` / `acceptEdits` / `plan` / `dontAsk` /
`yolo`) without touching the saved config — this is how wrappers like
cmndcntr launch orc with their own per-run policy. `ORC_PROFILE` does the
same for profiles. `ORC_MODEL_OVERRIDE` is the env form of `-m`. The model
catalog and fit cache both live 24h (`orc refresh` / `orc probe --fit`
to force). The Artificial Analysis quality cache lives 24h too;
`orc refresh` re-checks it but won't re-fetch the leaderboard (the
scraper is in cmndcntr; run `make refresh-quality` from the orc source
to update the bundled snapshot, then `./build.sh` and reinstall).
`orc env` is `launch` without the `exec`.

## Fusion: Claude lead + Codex sidekick

`fusion` adds a small lead/sidekick harness beside `orc`. It keeps the lead agent in charge of the user conversation and final review, then delegates bounded work to Claude Code or Codex through a shared task contract. Each run records its task, JSONL events, stdout, stderr, result, and reusable session id under `.fusion/` in the workspace.

The lead can call the other agent through an MCP server:

```sh
fusion lead                 # Claude leads by default
fusion lead --agent codex   # Codex leads and can call Claude
fusion doctor
fusion trace --limit 50
fusion usage --limit 1000
```

For a direct worker call:

```sh
fusion delegate --agent codex --role implementation \
  --success 'tests pass' \
  'Add the requested feature and run the narrowest meaningful test suite.'
fusion ultra 'Add the requested feature and ship the smallest tested change.'
fusion ultra --cheap-only 'Explore and review this without spending on a strong route.'
fusion ultra --harness codex 'Run the full bounded pipeline through Codex.'
```

`fusion` uses a single writer lock for a workspace, so two write tasks cannot edit the same checkout at once. Use separate Git worktrees when you want parallel write tasks. Read-only tasks can run independently. The default Codex sidekick uses `codex exec --json`; the default Claude sidekick uses Claude Code print mode with structured JSON output.

### Ultra without the token fire

Ultra is an explicit bounded pipeline modeled after the useful part of
[UltraCode](https://github.com/diepquynh/ultracode): explore, plan, implement,
review, and synthesize. It uses fresh stage contexts and JSON handoff files in
`.fusion/ultra/`, so later stages read evidence instead of inheriting every
earlier transcript. The stage count is capped, writes are serialized, and the
example routes cap each ORC-backed Claude call with `--max-budget-usd`.

Copy `.fusion.json.example` to `.fusion.json` in a project, then make sure ORC
has a current model catalog and key. The `orc-free` route selects the highest
ranked currently free tool-capable model; `orc-best` selects the highest ranked
tool-capable model from ORC's live catalog. The IDs are resolved at run time so
the pipeline does not pin a stale model name. Override either route with an
explicit `model`, `profile`, or `launcher_args` when you want a fixed lane.

```sh
cp .fusion.json.example .fusion.json
orc refresh
fusion ultra 'Refactor the cache layer and keep the existing tests green.'
orc fusion ultra --cheap-only 'Review the current diff for regressions.'
```

`fusion ultra` is opt-in because a multi-stage workflow can spend more tokens
than a direct lead/sidekick run. Use `fusion delegate --route orc-free ...`
for one bounded cheap worker, or `--route orc-best` when the stage needs a
stronger model. `--harness codex` runs every configured stage through Codex;
`fusion lead --agent codex` makes Codex the interactive lead and exposes the
same MCP delegation tools for Claude workers.

### Traces and dogfood

Every dispatched worker writes a metadata-only span to
`.fusion/traces.jsonl`. Spans include the trace and parent IDs, agent, route,
resolved model when the provider reports it, duration, status, token usage,
tests, blockers, and links to the raw run artifacts. Prompts and model output
are not copied into telemetry; inspect the run's `stdout.log` when you need
that detail. Set `telemetry.enabled` to `false` in `.fusion.json` to disable
the trace ledger.

```sh
make test                 # deterministic unit tests
make dogfood               # actual fusion CLI + fake Claude/Codex subprocesses
fusion trace --limit 50   # inspect spans
fusion usage              # aggregate tokens, latency, and reported costs
FUSION_REAL=1 make dogfood-real  # opt-in provider smoke; consumes quota
```

The real smoke command returns exit code 2 when the CLI was reached but a
provider blocked the turn for quota, authentication, or session limits. That
keeps provider availability separate from harness regressions.

The architecture and source map are in [FUSION_RESEARCH.md](FUSION_RESEARCH.md).

## Development

The shipped scripts are assembled. Sources of truth:

- `pricing.jq` — the shared jq math behind the HUD, `orc stats`, and the model
  picker (token normalization, transcript validation, pricing, free/tool
  flags, incremental session aggregates). The HUD and stats are guaranteed
  to price identically because they run the same definitions.
- `hud.sh.in` — the statusline HUD body.
- `orc` — everything else.

`./build.sh` expands the `#INCLUDE pricing.jq` markers, writes the generated
`hud.sh`, splices it into `orc`'s embedded heredoc, and stamps `ORC_HUD_VERSION`
with a content hash over both sources plus `data/quality.json` — installed
HUDs regenerate automatically on the next launch. Edit the sources, then run
`./build.sh`; CI fails if `orc` or `hud.sh` have drifted from them.

The quality cache is a static JSON snapshot shipped at `data/quality.json`
(generated by cmndcntr's `scripts/fetch-artificial-analysis-leaderboard.mjs`,
output passed through `make refresh-quality`). Records on the leaderboard
that lack an `openrouterApiId` are routed through `SLUG_TO_OR_ID` inside
`orc` (an inlined JSON map of effort variants and ambiguous slugs) so
they still rank. New slugs the fetcher can't auto-join are listed in the
`unmappedSlugs` field of the output; add them to the map after deciding
which OpenRouter id they belong to, then rerun `make build`.

`./test/run.sh` runs the test suite: HUD rendering against fixture
transcripts and a fixture catalog (paid / free / unknown-model pricing,
legacy and current cache-usage shapes, all three context-percentage
sources, subagent sibling spend, resume this-vs-file split), `orc stats`
aggregation, model tool-support and fit flags, profile / `.orc.json`
resolution, and `orc status` / `orc env` / `orc save` consuming the same
resolved object. CI runs shellcheck on every script plus the test suite.

## Caveats

- Tool-calling quality varies by model — Claude Code leans hard on tools, so
  weaker models will feel broken. Reasoning models with solid tool support
  work best.
- Extended thinking and prompt caching only work fully on Anthropic models;
  costs on other models may be higher than the same workflow on Anthropic
  first-party.
- Stealth/preview models (`stealth/*`) come from anonymous providers that
  retain prompts and completions — don't point them at anything sensitive.
- The HUD cost is an estimate: it trusts the catalog's per-token price fields,
  including OpenRouter's separate 5-minute/1-hour cache-write multipliers,
  which may not match what your route actually serves.
