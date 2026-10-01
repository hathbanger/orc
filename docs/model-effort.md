# Native Codex and Claude Code model and effort choices

Claude Code workers take the same `model` + `reasoning_effort` pair and receive
`claude --effort <level>`. Claude Code accepts `low`, `medium`, `high`, `xhigh`
and `max`; `none`, `minimal` and `ultra` are Codex-only and refused for Claude.
Claude Code publishes no per-model effort catalog and its JSON result does not
report the applied effort, so `execution_choice.catalog` is `unchecked` and
`observed` stays `unobserved`. Observed on `claude-sonnet-5` (2026-09-24, same
read-only task): `low` used 730 output tokens in 15 s, `high` 1,596 in 26 s.
On a smaller one-sentence task (2026-09-24), `claude-opus-5-5` used 238 vs 310
output tokens and `claude-fable-5-1` 291 vs 354 (low vs high); both reported the
requested model. A trivial task shows the flag is accepted, not that effort
changes quality; judge that with verdicts on real work.
Effort sent through an `orc` (OpenRouter) route is passed to Claude Code; whether
the upstream model honors it is unverified.

```sh
fusion delegate --agent claude --read-only --model claude-sonnet-5 --reasoning-effort high "Review the diff"
```

## Codex

Fusion accepts an explicit model and reasoning effort for each native Codex
worker. A pair is one choice: `gpt-6-sol / xhigh` and `gpt-6-astra / high` are
different candidates, with no assumed ordering across models.

An authored workflow node can override the configured Codex pair:

```json
{
  "id": "inspect",
  "agent": "codex",
  "write": false,
  "model": "gpt-6-astra",
  "reasoning_effort": "high",
  "task": "Inspect the persistence boundaries and report concrete failure cases.",
  "acceptance": {"required_handoff": ["summary"]}
}
```

The same fields work in `codex` settings and existing named Codex routes.
One-off delegations take the same pair from the CLI or the MCP `fusion_delegate`
tool; `model` alone also pins Claude and other workers:

```sh
fusion delegate --agent codex --read-only --model gpt-6-astra --reasoning-effort high "Inspect the persistence boundaries"
fusion delegate --agent claude --model claude-haiku-4-5-20251001 "Summarize the failing test"
```

```json
{"agent": "codex", "task": "Inspect the persistence boundaries", "write": false,
 "model": "gpt-6-astra", "reasoning_effort": "high"}
```
Explicit effort requires an explicit resolved model. Fusion passes
`-c model_reasoning_effort="high"` to stock Codex for fresh and resumed workers.
Pinned pairs separate worker sessions and invalidate a workflow receipt when
the pair changes. Workflows without pinned effort retain their legacy digest
and session behavior.

Local `$CODEX_HOME/models_cache.json` (default `~/.codex/models_cache.json`)
provides model-specific capabilities. A known unsupported pair is rejected;
missing, malformed or incomplete metadata is recorded as unchecked and Codex
validates its live support. Reading this cache makes no network request and
does not prove current availability, permissions, quota or runtime application.

`ultra` can enable native task delegation. It requires explicit
`allow_native_delegation: true` in Codex settings or the node, and currently
requires `write: false`. Child traces and child cost are not qualified by this
integration. The capability is authorization, not proof that no children exist
when false.

## OpenCode

OpenCode workers pass `reasoning_effort` as `--variant` to `opencode run`
without consulting the Codex local capability cache. The value is forwarded
to OpenCode and the upstream provider validates it at request time.

Accepted values for OpenCode lanes: `none`, `minimal`, `low`, `medium`,
`high`, `xhigh`, `max`. `ultra` is Codex-only and refused.

```sh
fusion delegate --agent opencode --route oc-sonnet --read-only \
  --model anthropic/claude-opus-5-5 --reasoning-effort high \
  "Review the persistence boundaries."
```

`execution_choice.catalog` is `unchecked` for OpenCode workers: the harness
records the requested variant but cannot observe what the provider applied.
`observed` stays `unobserved` in the run result.

Laya model/effort advice works for OpenCode lanes the same way it does for
`agy`: entries in `decisions.model_effort_pairs` with `"agent": "opencode"`
are offered as candidates for tasks pinned to an OpenCode worker.

## Laya advice using the existing router

```json
{
  "codex": {"model": "gpt-6-astra", "reasoning_effort": "high"},
  "decisions": {
    "mode": "shadow",
    "model_effort_pairs": [
      {"model": "gpt-6-astra", "reasoning_effort": "high"},
      {"model": "gpt-6-sol", "reasoning_effort": "xhigh"},
      {"agent": "claude", "model": "claude-opus-5-5", "reasoning_effort": "high"},
      {"agent": "claude", "model": "claude-fable-5-1", "reasoning_effort": "medium"},
      {"agent": "agy", "model": "gemini-3-pro", "reasoning_effort": "low"}
    ]
  }
}
```

An entry's `agent` defaults to `codex`. A pinned worker is only offered pairs
for its own harness: a Claude task pinned to Opus 5.5 / high gets Laya advice
among the Claude pairs, never a switch to Codex. Each harness validates its own
pairs: Codex against the cached native catalog; Claude Code accepts
`low`–`max` except on Haiku (no effort support), and records `xhigh` on 4.6
models as `requested_may_downgrade` because they run it as `high`; agy accepts
`low`, `medium`, `high` and `max`. agy also names a thinking level in its
model ids (`gemini-3.8-flash-high|medium|low`, `gemini-3.1-pro-high|low`; see
`agy models`); its default is `gemini-3.8-flash-high`. Grok has no effort
control here yet.

The explicit pair must be in the list (at most eight pairs). The existing
`routing` decision asks Laya for one pair; its recommendation and the retained
pair are recorded separately in DecisionStore. Explicit workers retain their
pair even if standalone decisions mode is active and the classifier is
qualified. Missing or failed Laya inference leaves the explicit selection
unchanged. In off mode there is no inference. Ultra is excluded from advice
for writers and workers without delegation authorization.

Existing `agent: auto` routes retain their existing qualification gate and
now carry model and effort together. This does not qualify a new model/effort
policy or turn shadow recommendations into autonomous choices.

## What the evidence means

Worker results, local traces and workflow reports expose `execution_choice`:

- `requested`: the resolved model/effort pair, or null effort for inherited defaults.
- `dispatch`: prepared, attempted, or returned; attempted records the actual argv.
- `catalog`: capability cache provenance and observed support, or unchecked.
- `observed`: model reported by native JSON when available; effort remains null.
- `native_delegation`: authorization plus explicitly unobserved child visibility.

Successful exit does not attest effective effort. The stock JSON event stream
does not provide that evidence here. Failed launch can remain attempted; it
does not prove inference began. Usage remains whatever the harness reports;
there is no fabricated cost or complete child billing claim.

This is an initial worker-invocation choice, not effort adjustment between
generations. A future native checkpoint adapter must confirm settings captured
by the next sampling step, handle replay/cancellation/compaction, and preserve
operator authority before making that claim.

Acceptance outcomes join routing decisions by task/run ID. An accepted result
is evidence about the chosen pair, not a label that it was the best pair.
Keep optimal-pair labels pending matched alternatives, independent behavioral
checks and review. Model routing and reasoning control do not train a coding
model or a coding LoRA.

Run local contract tests with
`python3 -m unittest discover -s test -p reasoning_test.py -v`.
Fixtures run benign local worker scripts; they do not establish model quality.
