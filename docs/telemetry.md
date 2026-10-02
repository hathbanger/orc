# Usage and telemetry

[Start here](../README.md) · [CLI reference](cli.md) · [MCP reference](mcp.md)

- [Traces and dogfood](#traces-and-dogfood)
- [Local usage and quota headroom](#local-usage-and-quota-headroom)
- [Remote telemetry (on by default)](#remote-telemetry-on-by-default)
- [Shared control workspace](#shared-control-workspace)

## Traces and dogfood

Every dispatched worker writes a metadata-only span to
`.fusion/traces.jsonl`. Spans include the trace and parent IDs, agent, route,
resolved model when the provider reports it, duration, status, token usage,
tests, blockers, and links to the raw run artifacts. Prompts and model output
are not copied into telemetry; inspect the run's `stdout.log` when you need
that detail. Set `telemetry.enabled` to `false` in `.fusion.json` to disable
the trace ledger.

```sh
make test                 # deterministic Python unit tests
./test/run.sh             # the launcher + HUD suite (separate; CI runs both)
make dogfood               # actual fusion CLI + fake Claude/Codex subprocesses
fusion trace --limit 50   # inspect spans
fusion usage              # local coordinator/worker tokens and quota headroom
FUSION_REAL=1 make dogfood-real  # opt-in provider smoke; consumes quota
```

The real smoke command returns exit code 2 when the CLI was reached but a
provider blocked the turn for quota, authentication, or session limits. That
keeps provider availability separate from harness regressions.

## Local usage and quota headroom

`fusion usage` reads workspace `.fusion/traces.jsonl`, Claude Code transcripts
under `${CLAUDE_CONFIG_DIR:-~/.claude}/projects/*/*.jsonl`, and Codex rollouts
under `${CODEX_HOME:-~/.codex}/sessions/**/*.jsonl`. Provider transcripts cover
all projects in those directories. Sources are optional; missing/unreadable
files, malformed lines and unfinished JSON records are skipped. This command
runs offline and does not load routing configuration or send telemetry. It
writes reporting state only with `--record`.

```sh
fusion usage --since 24h --by session
fusion usage --since 7d --by model --top 10 --json
fusion --json usage --since 2026-09-01T00:00:00Z --by project
fusion usage --by agent --context-threshold 300000 --calls-per-hour-threshold 90
fusion usage --record
```

The default window is 24 hours. `--since` accepts positive hour/day durations
or an ISO timestamp; timezone-less timestamps mean UTC. Both boundaries are
inclusive and future observations are excluded. ORC spans use their end time
(start time if unavailable), Claude messages use their first recorded timestamp,
and Codex increments use the token-count event timestamp. Records without a
usable timestamp cannot contribute to usage totals. Calls/hour divides by the
whole selected window, including idle time. A session is marked `coordinator`
when its average context is **greater than** 200,000 tokens or its calls/hour
is **greater than** 60. Both thresholds are configurable as shown above. The
flag is computed per session before grouping; a group is flagged if it contains
any flagged session.

Claude streamed assistant messages are deduplicated within each file by message
ID or requestId; repeated chunks retain the largest reported value in each token
bucket. Each file is a session and its containing directory is its project.
Codex counters are cumulative: increments are calculated before time filtering,
so earlier usage is not attributed to the selected window. Unchanged counters
add no calls or tokens; a call means an advancing usage observation, and model
attribution uses available rollout context, otherwise `unknown`. ORC calls are
worker spans (which may themselves summarize multiple provider calls), with
session keys/IDs when recorded, otherwise run IDs. Repeated span IDs count once.
Dispatched runs record their `lane_key` (agent plus route account) in the span
and in `task.json`, so ORC rows and Claude headroom are attributed per account.

Token buckets are disjoint: `input_tokens` excludes cache reads/writes, and
`context_tokens` is input + cache-read + cache-write. OpenAI cached input is
subtracted from its inclusive input count; Anthropic cache fields are already
separate. Reasoning output is already included in output and is not added again.
ORC and provider records can describe the same work. Cross-source totals are
**observed records, not guaranteed unique billed requests**; source identity
stays visible and unrelated IDs are not heuristically merged. Costs are summed
only where reported and may cover only part of a group.

`--top N` displays groups ranked by descending context tokens, then group key;
it does not truncate totals or recorded snapshots. The legacy `--limit N`
option limits the last N valid ORC ledger records read; provider transcripts
remain complete. Without `--limit`, the ORC ledger is not capped.

`--json` works before or after `usage`. The `fusion.usage.v1` schema is:

| Field | Meaning |
| --- | --- |
| `schema` | `fusion.usage.v1` |
| `generated_at`, `since`, `until` | UTC ISO timestamps; `until` equals generation time |
| `by`, `window_hours` | Grouping and reporting window duration in hours |
| `thresholds` | `average_context_per_call` and `calls_per_hour` coordinator thresholds |
| `total` | Metrics across all matching records, independent of `--top` |
| `groups`, `group_count` | Displayed groups and total number before truncation |
| `coordinator_sessions` | Sorted source-qualified session keys that exceed a threshold |
| `sources` | Metrics separately for `orc`, `claude`, and `codex`; absent sources have zero observed calls/tokens |
| `headroom` | Latest recorded quota windows, described below |

Every metrics object contains `calls`, `input_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`,
`context_tokens`, `average_context_per_call`, `calls_per_hour`, `cost_usd`, and
`cost_reported_calls`. Token fields are counts, cost is reported USD, and averages
and rates are numbers. `cost_usd` is null when no cost is reported; zero is a
reported zero cost. Empty aggregates have zero calls/tokens/averages/rates.
Groups add `key`, `coordinator`, `coordinator_sessions`, and sorted metadata
arrays: `sources`, `sessions`, `projects`, `models`, `agents`, `routes`,
`lane_keys`, `session_keys`. Missing model/project/agent is `unknown`; unavailable
optional ORC metadata is omitted from its array. Session keys are source-qualified;
Claude/Codex session keys include their absolute file paths. JSON and recorded
snapshots can therefore contain local paths and recorded account labels.

Each headroom entry contains `provider` (`codex` or `claude`), `account` (recorded
account ID/label or lane key, null when unknown), `status` (`known` or `unknown`),
and `windows`. Codex windows are `primary`/`secondary`, preserving `used_percent`
(percent), `window_minutes` (minutes), and `resets_at` (provider Unix seconds).
Claude windows are `five_hour`/`seven_day`, preserving `utilization` (provider
fraction) and `resetsAt` (provider reset value). Each observed window also has
`observed_at` (UTC ISO) and `source` (local file path). Absent windows/fields
are null, **never an inferred zero**. A missing provider has an unknown entry.
Unknown identities share an unknown-account bucket; they are not assumed to
identify distinct accounts. Only the selected Codex home and recorded ORC logs
are inspected; account credentials are never read.

Headroom takes the latest observation **per window per account**, including
records older than `--since`. Codex reads rollout `rate_limits`; Claude reads
`rate_limit_event.rate_limit_info.unifiedWindows` in ORC run `stdout.log`,
`stderr.log`, and `events.jsonl`, using recorded task/trace identity when needed.
When quota event timestamps are absent, containing run timestamps or file mtime
provide freshness ordering. These are historical observations, not live quota
checks; inspect `observed_at` and reset values before routing. Reusable Python
functions are `fusion_usage.read_orc(workspace, limit=None)`,
`read_claude(config_dir=None)`, `read_codex(home=None)`,
`headroom(workspace=None, codex_home=None)`, and `report(workspace, ...)`.

`--record` appends one full report plus a UTC `date` field to
`.fusion/usage.jsonl`, at most once per day (the first snapshot wins). It records
the selected window/grouping/thresholds before display truncation; it does not
imply a midnight-to-midnight billing period. A file lock prevents concurrent
same-day duplicates. Existing snapshots survive transcript rotation. Regular
reports continue to read original sources rather than summing snapshots.

## Remote telemetry (on by default)

Fusion sends a small, deliberately reduced copy of each span to
`https://orc-telemetry.fly.dev/v1/ingest` by default, with a notice before
the first send. Reporting needs no token. The shared collector shows
aggregate agent/route/model/failure patterns across installations.

To stop sending while keeping local traces:

```sh
fusion telemetry off          # persists in .fusion.json
FUSION_TELEMETRY=0 fusion delegate --agent codex --read-only "Review the diff"
# Or disable for this shell and its children:
export FUSION_TELEMETRY=0
```

Or set this in the project's `.fusion.json`:

```json
{
  "telemetry": {
    "remote": {
      "enabled": false
    }
  }
}
```

The payload contains `schema`, a random per-machine `install_id`, and `spans`.
Each span sends `trace_id`, `span_id`, `parent_span_id`, agent, role, route,
model, whether it was a
write, status, a coarse `failure_class` (`quota` / `permission_denied` /
`timeout` / `missing_executable` / `worker_error` / `coordinator_error` —
never the raw blocker
text), `start_time_ms`, `end_time_ms`, `duration_ms`, and normalized token/cost
`usage`. The install ID is generated locally, not derived from an account identity. A workflow node reused from a
digest-matched receipt on resume (see [workflow digests](workflows.md#persisted-fan-out-workflows)) never actually
dispatches, so it emits its own `cache_hit` status rather than `success` —
otherwise "how much is caching actually saving the group" would be
invisible in the exact data source built to answer that.

There are no dedicated outbound prompt, model-output, changed-file, test-command,
raw-blocker, local-filesystem, workspace-path or source-repo fields. Role, route, model and
trace identifiers are copied directly, however; arbitrary configured strings are
not scrubbed. Local traces retain more detail than the remote payload.

`fusion telemetry status` reports effective send status in `remote_enabled`,
configured enablement in `remote_configured_enabled`, and any disabling reasons
in `remote_disabled_reasons`. Sending requires local telemetry and remote
reporting to be enabled, a configured endpoint, and no `FUSION_TELEMETRY=0` opt-out.
Sends run in detached child processes with a three-second network timeout. The
reduced payload is passed over stdin, and the sender can finish after the CLI
exits. Sends are best-effort: failures are swallowed, and dispatch never waits
for the network or the child. Remote reports refuse when configured
remote enablement is false; the env opt-out does not prevent reading reports.

`fusion telemetry report [--hours N]` (default 168, i.e. 7 days) reads your
own rows back — calls, cost, and average duration grouped by
agent/route/model/status/failure_class. It needs no token: this machine
already holds an unguessable install id and already sends it with every
span, so it can ask for its own rows back.

`--all` widens that to every install, and is the only thing that needs the
shared token in `.fusion.json` under `telemetry.remote.token` — that view
reveals how many people are running this and what they spend, so it stays
guarded. Use `fusion telemetry report --json` or
`fusion --json telemetry report` for the machine-readable form.

The collector itself (a small Fly + Postgres app) lives in
[`telemetry/`](../telemetry/).

The architecture and source map are in [FUSION_RESEARCH.md](../FUSION_RESEARCH.md).


## Shared control workspace

Use `fusion --control-workspace /path/to/controller usage` to read the controller's
Fusion evidence. Native Claude/Codex transcript sources still follow their selected
homes. Receipts from disposable checkouts remain available in the controller.
See [storage selection and configuration layering](configuration.md#control-workspace).
