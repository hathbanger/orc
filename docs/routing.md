# Routing and auto mode

[Start here](../README.md) · [CLI reference](cli.md) · [MCP reference](mcp.md)

- [Automatic and explicit choices](#automatic-and-explicit-choices)
- [Cost tie-breakers](#cost-tie-breakers)
- [Accounts and quota headroom](#accounts-and-quota-headroom)
- [OpenCode lanes](#opencode-lanes)
- [Antigravity lanes](#antigravity-lanes)
- [Prompt cache and sessions](#prompt-cache-and-sessions)
- [Gym lane priors](#gym-lane-priors)

## Automatic and explicit choices

Use `fusion delegate --agent auto --read-only "Review the current diff"` to let
the router choose an eligible lane. Authored workflow nodes can likewise set
`"agent": "auto"`. Named routes live under `routes` in `.fusion.json`; a direct
CLI delegation infers `--agent` from `--route` when omitted. If the route has no
agent, supply `--agent` explicitly; an explicit agent must match the route.

```sh
fusion delegate --route orc-free --read-only "Review the diff"
fusion delegate --agent codex --read-only --model gpt-6-astra --reasoning-effort high "Inspect persistence boundaries"
```

Model names in examples are illustrative; installed CLI capabilities and account
access determine availability. Treat model and reasoning effort as a pair, not a
single universal quality scale. Explicit effort requires a resolved model; see
[model/effort validation, session identity and observed evidence](model-effort.md).

When an **Auto** stage hits a provider quota, Fusion excludes that route and
tries another installed, permitted worker within the existing attempt and budget
limits. This works with Laya off or in shadow mode. Explicitly selected workers
stay pinned. A quota notice identifies the exhausted worker and route separately
from Fusion's spend budget. **Resume workflow** lets you select the unfinished
stage, worker or named route, and an explicit attempt limit; accepted stages are
reused when their inputs still match. For example:

```sh
orc fusion workflow resume RUN_ID --node review --agent codex --max-attempts 3
```

Native workers are Codex, Claude, Antigravity (`agy`), and Grok Build (`grok`).
Local CLIs still use their own remote-provider accounts and quotas. Grok uses
headless `streaming-json` output, a fresh session, and no subagents; its default
`plan` permission mode makes it an automatic read-only review/discovery option
in restricted mode. The adapter reads model, usage and nonpartial cost when
reported. The older `plain` fallback lacks structured tool/usage receipts. Set
`grok.permission_mode` to `acceptEdits` for explicitly authorized writer work.
Named ORC routes can use other configured models; automatic ORC selection still requires passing tool-fit evidence.

Every eligible lane participates in ranking, including routes late in the
configuration. After outcome ranking, different-agent preference and quota
ordering, the first eight candidates become the choice options and routing log.

Task roles form evidence classes by lowercasing and joining whitespace with
`-`: `Triage Locate` becomes `triage-locate`. For each lane, routing uses that
role's local checked runs once there are at least `minimum` of them (the integer
`decisions.rank_by_outcomes`, or `3` by default). Below that threshold it uses
the existing read/write evidence, pooling all work classes only when that work
class has no evidence. Gym pseudo-counts do not satisfy the role minimum.
Each candidate records `evidence_scope: role|work` and its `local_class` in the
routing log. Missing, empty, unknown, or unseen roles retain work-class behavior.

Laya records the normalized role in decision context and exports it with training
examples. Calibration retains `kind:schema_hash:question` buckets and additionally
publishes `kind:schema_hash:role:question` buckets for roles that pass the same
Learn-then-Test rules: train-only temperature fitting, a certified held-out risk
threshold, enough held-out answers, and enough independent train and acted
held-out groups. Both decision scoring and action qualification prefer the role
bucket, falling back to the role-less bucket when no role bucket exists.

## Cost tie-breakers

With `decisions.rank_by_outcomes` enabled, optional integer `cost_tier` settings
prefer cheaper lanes when verified evidence is close. Set them on an agent
(`claude`, `codex`, `agy`, or `grok`) or a named route; the route overrides the
agent. Lower numbers rank first. Unset or `null` costs are tied after configured
costs; when no costs are configured, existing rankings are unchanged.

The router groups smoothed acceptance scores `(accepted + 1) / (checked + 2)`
within a band of the best score in each tier: `cache.warm_epsilon`, or, when
any cost tier is set, the wider of `cache.warm_epsilon` and
`decisions.cost_epsilon` (default `0.05`). Within a tier, quota headroom comes first, then lower
cost, then warm sessions, then existing order. Cost never crosses evidence
tiers: clearly better verified evidence wins. Unproven lanes are still tried
first so every arm earns evidence, cheapest tier first; quota demotion retains
priority across tiers.

Each logged candidate includes its resolved `cost_tier`; the log policy records
the effective `cost_epsilon` when cost ranking applies. `fusion decisions
routing-report` includes `routing_policies` with each decision's policy and
candidate cost tiers, even before an outcome exists.

### Automatic pool

`decisions.auto_routes` bounds automatic routing to the named lanes before any
ranking: evidence, cost, warmth and quota only order lanes inside the pool.
Use it to keep cheap or unproven lanes out of work that ships while still
letting `auto` pick model and effort among the lanes you trust. See
[configuration](configuration.md).

### Task needs

A task may name capabilities it needs (`--needs local_server`). Automatic
routing drops lanes whose config `lacks` one before ranking; if that leaves
none, it routes on the full pool and logs `needs_unmet`. The routing log
records `needs` for every such choice. Every writing task also needs `write`, in
every execution mode, so a lane that `lacks` it only takes read-only work and
the fallback never gives it a write. See [configuration](configuration.md).

### Overflow lanes

`decisions.overflow_routes` are dropped from automatic candidacy while any
other pooled lane survives the filters, and become the candidates when none
does. Use them for a metered API lane behind a subscription: quota exclusion
or a quota cooldown on the subscription moves work there, and the next choice
after the subscription resets goes back. See [configuration](configuration.md#metered-lanes-and-overflow).

## Accounts and quota headroom

Named routes in `.fusion.json` can set `env` to an object of string environment
variables. These override the inherited worker environment; values expand `~`
and environment variables such as `$HOME` before the worker starts. For example:

```json
{"routes": {"claude-second": {
  "agent": "claude",
  "env": {"CLAUDE_CONFIG_DIR": "~/.claude-second"},
  "account": "acct2"
}}}
```

The optional non-empty string `account` identifies the subscription for lane
health and quota/permission cooldowns, giving this route the lane `claude@acct2`.
Without it, Fusion uses the expanded `CLAUDE_CONFIG_DIR`, then `CODEX_HOME`, from
the route environment as the account identity. Routes using the same account
share cooldowns; a second account remains available for automatic fallback.
ORC keeps its separate launcher lane (`claude@orc@acct2` with an account).
Routes without account settings retain their existing lane keys and cooldowns.

Automatic routing also uses the latest recorded quota for each lane, including
its account. Claude runs use `stream-json --verbose`; the final result retains
the existing result fields, with an additional `quota` when reported. Claude
rate-limit events and Codex `rate_limits` are normalized into `quota.windows`
with `used` fractions (0–1), `resets_at` Unix seconds, and Codex window duration
in `window_minutes`, and saved in the run result and trace. The existing
`fusion usage` headroom reader also exposes these trace observations.

Configure the thresholds in `.fusion.json`:

```json
{"quota": {"pace_margin": 0.15, "soft": 0.85, "hard": 0.97}}
```

A lane is **tight** when any active window's usage exceeds `soft`, or exceeds
the elapsed fraction of that window plus `pace_margin`. Elapsed time uses
Claude's five-hour/seven-day windows or Codex's reported duration. Tight lanes
rank after other eligible lanes for the same work class, including after
outcome and cache ranking. A lane is **exhausted** above `hard`, or when its
status is `rejected` before its most-used window resets; automatic routing
excludes it. Comparisons are strict, thresholds must be fractions, and `soft`
cannot exceed `hard`. Expired windows stop constraining routing. Unknown
duration disables only the pacing comparison; missing quota preserves the
existing order.

Explicit routes stay pinned to their model and effort; when the pinned account
is exhausted they move to a same-model overflow route (`quota_twin`). Existing
authorized exploration still applies.
Quota-free traces do not erase an earlier observation, and observations without
a recorded lane key cannot constrain unrelated accounts. Routing logs and
`fusion decisions routing-report` include quota windows, classifications,
thresholds, and reasons for demotions and exclusions, even before an outcome
is recorded. These quota decisions are logged even with decision advice off.

### `fusion quota`

`fusion quota` shows each recorded account: its classification (available,
tight or exhausted) with the reasons, every window's used fraction and reset
time, how old the reading is, the lanes on that account, and any account and
model a recent quota failure is cooling. Readings are passive, taken from the
traces of runs that used the account, so an idle account keeps its last one.

`fusion quota probe` sends one tiny read-only run (role `quota-probe`) to each
account whose exhausted window has reset with no reading since, choosing the
account's own lane (an automatic one first, cheapest first). It never moves to
an overflow twin, records a fresh reading, and does nothing when no account is
due, so it is safe on a short schedule. `--dry-run` lists what it would probe;
`--stale HOURS` also probes any reading older than that.

`fusion quota rates [--days 7]` measures how much of each window ORC runs
consume: every rise between consecutive readings of one account and window is
charged to the ORC runs on that account that ended in between, as window share
per USD (Claude) or per million tokens (Codex). Per lane it shows p50/p90 run
cost and minutes and the share of each window a p90 run uses. Other sessions on
the same subscription are folded in, so the figures are upper bounds.

## OpenCode lanes

`opencode` (`opencode run --format json`) is a fifth worker harness that routes
any provider OpenCode supports — Anthropic, OpenAI, Google, and xAI — through
the `provider/model` `model` key. It runs as a sidekick (`--agent opencode`), a
workflow node, an Ultra stage/harness, or an interactive lead
(`fusion lead --agent opencode`). Sessions resume through `--session` the same
way Codex threads do.

```sh
fusion delegate --agent opencode --route oc-sonnet --read-only \
  "Review the current diff and return the five-field handoff."
fusion --json ultra --harness opencode 'Explore and review this change.'
```

**Automatic routing requires an explicit `model`** (`provider/model`). Without
one, Fusion cannot know which provider the lane will use and excludes it from
automatic candidacy with the message `OpenCode lanes need an explicit
provider/model for automatic routing`.

`reasoning_effort` on an OpenCode lane passes `--variant` to `opencode run`.
The value is forwarded unchecked to the provider; OpenCode and the upstream
provider validate it at request time. The Codex local capability cache is not
consulted. See [OpenCode and --variant](model-effort.md#opencode).

For keys, wrappers and per-provider routes see [OpenCode settings](configuration.md#opencode-settings).

## Antigravity lanes

`agy` — the Google Antigravity CLI — is a third worker harness. It runs as a
sidekick (`--agent agy`), an Ultra stage/harness, or a workflow node, and its
host model list covers Gemini, Claude, and GPT-OSS under a separate quota pool
(`agy models`). Sessions resume through `--conversation` the same way Codex
threads do.

```sh
fusion delegate --agent agy --read-only --role countercheck \
  --success 'structured handoff' 'Re-verify the diff against the spec.'
fusion --json ultra --harness agy 'Explore and review this change.'
```

Restricted-mode automatic AGY routing requires explicit
`dangerously_skip_permissions: true` on the lane; sandbox/native command settings
alone do not qualify it. Explicit AGY selection can use a narrower setup.
See [AGY permissions and setup](configuration.md#antigravity-settings).

## Prompt cache and sessions

Trace spans record the session key, whether the run resumed, idle time since the
session's last run, and cache-read ratio. Optional `cache` configuration can
start fresh instead of cold-resuming and let warm lanes break routing ties.
See [Prompt cache and sessions](../FUSION_DECISIONS.md#prompt-cache-and-sessions).

## Gym lane priors

`fusion gym priors GYM_DIR` exports measured per-lane, per-work-class outcomes to
`$ORC_HOME/lane_priors.json` (normally `~/.config/orc/lane_priors.json`). Routing
uses these as bounded pseudo-counts, with weight `0.5` per gym attempt and cap
`10` per lane/work class by default. Local verified outcomes accumulate alongside
them. A missing file contributes no prior; malformed data is rejected.

```json
{"decisions": {"priors": {"weight": 0.5, "cap": 10}}}
```

Read-only roles containing `interpret` use the gym's `interpret` prior when
present for that lane, otherwise `read`. Roles containing `locate` or `localize`
use `read`; writing tasks always use `write`. Other roles keep their existing
work class. The selected class is recorded in each candidate's `prior.class`.

Set `decisions.priors` to `false` to disable them, or set its `path` to a different
export. Priors do not confer permissions or bypass availability, fit or quota
checks. See [gym evidence](learning.md#gym) and
[configuration](configuration.md).
