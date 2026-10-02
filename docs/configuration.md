# Installation and configuration

[Start here](../README.md) · [CLI reference](cli.md) · [MCP reference](mcp.md)

- [Requirements](#requirements)
- [Install](#install)
- [Configuration files](#configuration-files)
- [Control workspace](#control-workspace)
- [Runtime permissions](#runtime-permissions)
- [Antigravity settings](#antigravity-settings)
- [Older Grok clients](#older-grok-clients)

## Requirements

Use macOS or Linux and Python 3. The base Fusion harness uses the standard
library. Optional Laya setup requires `uv`, creates a Python 3.12 environment,
and installs Laya/Transformers dependencies.

[Claude Code](https://claude.com/claude-code) (`claude`) is required by
`install.sh` regardless of which workers you plan to use — it exits if `claude`
is not on PATH.

**For the guild**, add `git` and whichever workers you want to dispatch to:
Codex (`codex`), Antigravity (`agy`) or Grok Build (`grok`). Snout and PR
publishing also need an authenticated [GitHub CLI](https://cli.github.com)
(`gh`).

`install.sh` also requires `jq` and `curl`, even for a harness-only installation.
**For the launcher**, add an [OpenRouter](https://openrouter.ai) API key. `fzf` is optional but the
interactive model picker needs it (`brew install jq fzf`, or `sudo apt-get
install jq curl fzf`).

## Install

```bash
./install.sh
```

Checks dependencies and copies `orc`, `fusion`, the Fusion Python modules,
UI assets and bundled quality data to `~/.local/bin`
(override with `DEST=/somewhere ./install.sh`). Add that destination to PATH. To go straight to the guild, run `orc fusion ui` from
a repository — the control room needs no OpenRouter key unless you use the
`orc-free` / `orc-best` routes.

Run `orc` for [launcher setup](launcher.md#setup-and-launch-menu).

## Configuration files

Configuration loads defaults, then `$ORC_HOME/fusion.json` (normally
`~/.config/orc/fusion.json`), then `FUSION_CONFIG` or the `.fusion.json` found
upward from the worker workspace. If a control directory is explicitly selected,
its own `.fusion.json` is deep-merged last for `routes`, `decisions`, `learning`,
`quota`, `cache`, and `gym` only. Controller policy wins for those keys; other
worker settings, including native agent commands, permissions, and timeouts,
remain worker-scoped. Named routes can supply their usual route-specific worker
settings. Config-source reporting continues to identify the worker/global file.
With no control-workspace flag or environment setting, storage and configuration
remain worker-scoped; run receipts still include the absolute `workspace`.

Setting a route to `null` removes it, including the built-in `orc-free`,
`orc-best`, `codex-read` and `codex-write` routes, for example when there is no
OpenRouter key on the machine. A later file can define it again:

```json
{"routes": {"orc-free": null, "orc-best": null}}
```

Ultra's default stages and `ultra --cheap-only` use `orc-free` and `orc-best`;
set `ultra.stages` to other routes when removing them.

The launcher uses a separate [`config.json` and project `.orc.json`](launcher.md#config).
The checked-in [Fusion example](../.fusion.json.example) is a starting point, not a
complete schema. Routes, account identity, quota thresholds and model/effort
pairs are documented in [Routing](routing.md). Publication settings are in
[Workflows](workflows.md#publishing-reviewed-work); telemetry settings are in
[Usage and telemetry](telemetry.md#remote-telemetry-on-by-default).

Optional integer `cost_tier` settings on native agents or named routes break
outcome-ranking ties in favor of lower cost. Route settings override agent
settings; `null` clears an inherited preference. Unset costs stay tied after
configured costs, and leaving all tiers unset preserves existing rankings.
For example:

```json
{
  "codex": {"cost_tier": 2},
  "claude": {"cost_tier": 3},
  "routes": {"economy": {"agent": "codex", "cost_tier": 1}},
  "decisions": {"rank_by_outcomes": 3, "cost_epsilon": 0.05}
}
```

`decisions.cost_epsilon` must be a number in `[0, 1)` and defaults to `0.05`.
It defines evidence tiers only when a cost is configured and no cache
configuration supplies `warm_epsilon`. Costs never override clearly better
evidence or change unproven-lane exploration. See [cost tie-breakers](routing.md#cost-tie-breakers).

`decisions.auto_routes` limits which lanes `--agent auto` may choose. It is a
non-empty list of named routes or bare agents (`claude`, `codex`, `agy`,
`grok`); every other lane is dropped from automatic candidacy with the reason
`not in decisions.auto_routes`. It never binds a task that names its own agent
or route. Unset, every configured lane is a candidate.

```json
{"decisions": {"auto_routes": ["claude-opus-medium", "claude-opus-high", "codex-astra-medium", "codex-astra-high"]}}
```

`decisions.write_trials` lists configured route names that may take work that
ships or gates (a writer or a review) before they have evidence for it. Gating
work never explores, so without it a new lane never gets its first write while
a proven lane survives. While a listed lane survives the automatic filters and
has fewer local checked runs than the `decisions.rank_by_outcomes` minimum
(default `3`; gym prior pseudo-attempts do not count), it takes the pick ahead
of the ranked order; at the minimum it ranks on
its evidence. Lanes not listed are never promoted, overflow lanes stay out
while a primary survives, and a qualified Laya recommendation still wins. It
must be a list of route names; anything else is an error. See
[write trials](routing.md#write-trials).

```json
{"decisions": {"auto_routes": ["claude-opus-high", "claude-fable-high"], "write_trials": ["claude-fable-high"]}}
```

An agent or route may declare capabilities it `lacks`, as a list of names. A
task that `--needs` one of them (CLI `fusion delegate --needs local_server`,
MCP `needs`) skips that lane during automatic routing, with the reason
`lacks <name> this task needs`. A `lacks` on an agent applies to every route
of that agent. When no lane can meet a task's needs, automatic routing runs
on the full pool instead of refusing the work and records `needs_unmet: true`
in the routing log. Needs never move a task off a lane it named.

```json
{"codex": {"lacks": ["local_server"]}}
```

`local_server` is the need for binding and calling a server on 127.0.0.1 and
listing processes, which Codex's restricted sandbox denies.

`write` is a need every writing task has without asking, in every execution
mode including `yolo`. A lane with `"lacks": ["write"]` takes only read-only
work (reviews, investigations, interpretation) during automatic routing, and
the full-pool fallback for unmet needs never hands it a write. Use it for a
model you trust to read but not to change code:

```json
{"routes": {"agy-flash-medium": {"agent": "agy", "lacks": ["write"]}}}
```

### Metered lanes and overflow

Claude Code prefers `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`) over the
subscription login whenever one is in its environment. Fusion removes both
from every Claude worker unless its agent or route declares
`"billing": "api"`, even when the parent shell exports a key, and records the
credential Claude reports using (`api_key_source`) on each result. A
subscription lane that still ran on a key gets a `billing:` blocker.
`fusion doctor` warns when either variable is set in the current shell,
because interactive sessions started from it bill the key too.

A metered lane should get its key from Claude Code's `apiKeyHelper` setting
in a settings file only that lane passes (`launcher_args: ["--settings", ...]`):
Claude runs the helper itself, so the key never enters any environment its
Bash commands or child processes can read, and Claude reports
`apiKeySource: "apiKeyHelper"`. Keep the helper and key file mode 700/600 and
outside the worktree. Route settings that keep the lane bounded:

- `requires`: paths that must exist before the lane is a candidate (the key file).
- `daily_budget_usd`: the lane stops being a candidate once its runs reported
  that much USD in the last 24 hours.
- `max_budget_usd`: the per-run cap Claude Code enforces.
- `account`: a separate account keeps its quota and cooldowns apart from the subscription.

An API key cannot read its workspace spend limit; it learns about it only when
a request is refused ("You have reached your specified workspace API usage
limits. You will regain access on … UTC"). Fusion records that refusal as a
rejected `spend` window on the lane's account with the stated reset, so the
account is excluded until then (not retried every cooldown) and `fusion usage`
shows it beside the subscription windows.

`decisions.overflow_routes` lists automatic lanes that are candidates only when
no other pooled lane is (for example the subscription lanes are over the quota
hard limit or cooling down). Routing returns to the primary lanes as soon as
one is available again.

A pin names a model and effort, not an account. When a pinned run's account
cannot serve it (its quota reading is exhausted, or a run on the same account
and model hit its quota within the cooldown), Fusion runs it on an overflow
route with the same agent, model and effort instead, if that route passes the
automatic checks (`requires`, `daily_budget_usd`, its own quota). The result and
the routing log record `quota_twin` (`from`, `to`, `model`, `reasoning_effort`,
`reason`). With no matching overflow route the pin runs as named.

```json
{"decisions": {"auto_routes": ["claude-opus-medium", "claude-api-opus-medium"], "overflow_routes": ["claude-api-opus-medium"]},
 "routes": {"claude-api-opus-medium": {"agent": "claude", "model": "claude-opus-5-5", "reasoning_effort": "medium",
   "billing": "api", "account": "anthropic-api",
   "launcher_args": ["--settings", "/Users/you/.config/orc/claude-api-settings.json"],
   "requires": ["~/.config/orc/anthropic-api-key"], "daily_budget_usd": 150, "max_budget_usd": 15}}}
```

## Control workspace

To collect evidence from several checkouts in one controller directory:

```sh
fusion --workspace /path/to/product --control-workspace /path/to/controller --json delegate --agent codex "Implement the change"
export FUSION_CONTROL_WORKSPACE=/path/to/controller
fusion --workspace /path/to/another-product delegate --agent codex "Review the change"
fusion status                         # `runs` is an alias
fusion trace
fusion outcome RUN_ID --accepted --reason "Verified the tests and diff"
fusion decisions routing-report
```

The control directory is chosen by `--control-workspace`, then
`FUSION_CONTROL_WORKSPACE`, then the worker workspace. Paths are resolved to
absolute paths. Runs, traces, outcomes, decisions, routing logs, labels, and
workflow evidence live under the control directory's `.fusion/`. Workers still
execute in `--workspace`; each run receipt records that absolute `workspace`.
With an explicit control directory, sessions are scoped to the worker checkout,
and writer locks for other checkouts live in the control store. Receipts remain
available after a disposable worker checkout is removed. To inspect them later,
use the same flag/env or run Fusion from the controller directory.

## Runtime permissions

Runtime access is configured with `execution_mode`: `restricted` (default) or
`yolo`. Settings load from `~/.config/orc/fusion.json` (or `$ORC_HOME/fusion.json`)
and then the project's `.fusion.json`. To use YOLO across workspaces, set the
machine configuration to:

```json
{"execution_mode": "yolo"}
```

YOLO applies to leads, new and resumed workers, all workflow roles, and named
routes. Codex gets `--dangerously-bypass-approvals-and-sandbox`; Claude, ORC
routes, and AGY get `--dangerously-skip-permissions`; Grok gets
`--permission-mode bypassPermissions --sandbox none --no-plan`. Claude's
sandbox is disabled through invocation settings. AGY additionally requires
`enableTerminalSandbox: false` in its native settings if it was enabled there.
In YOLO, review/discovery scopes are worker instructions, not runtime read-only
guarantees. Attempt limits, budgets, provider quotas, and acceptance checks
remain in effect. The UI shows the selected access mode and lets you override
it per workspace under Settings → Runtime access.

In restricted mode, Codex writer runs use the `fusion_git_write` permission profile: the workspace
sandbox plus writable Git metadata, including linked worktrees. Branch creation,
staging, and commits work; discovery and review remain read-only. This requires
a Codex CLI with named permission profiles and `--strict-config` support.
Set `codex.git_write` to `false` to retain Codex's standard protected `.git` behavior.
Explicit legacy `sandbox_mode` settings in Codex config take precedence over named
profiles; remove those settings to use this scoped profile. Already-running workers
keep the permissions they started with; new launches and resumed workers use the update.

Codex writers have no network by default, which also blocks a server the
worker starts on 127.0.0.1. `codex.network` (on the agent or a route) adds a
network table to the `fusion_git_write` profile using Codex's own
permission-profile keys. Read-only runs and `git_write: false` ignore it.
When `mode` is `"limited"`, ORC also sets `features.network_proxy=true`:
Codex enforces the `domains` allowlist only through that proxy.

```json
{"codex": {"network": {"enabled": true, "allow_local_binding": true, "mode": "limited",
                       "domains": {"pypi.org": "allow", "files.pythonhosted.org": "allow", "registry.npmjs.org": "allow"}}}}
```

On macOS, `allow_local_binding` opens every localhost port, including other
services on the host, not just the worker's own server; Codex offers no
per-port rule. `ps` stays denied inside the sandbox.

## Antigravity settings

In restricted mode, Fusion launches `agy` with `--sandbox` and uses `plan` for
readers or `accept-edits` for writers. Execution mode alone does not grant command
permissions. In `~/.gemini/antigravity-cli/settings.json`, merge these settings
with your existing configuration:

```json
{"enableTerminalSandbox": true, "toolPermission": "proceed-in-sandbox"}
```

This permits sandboxed commands while preserving explicit permission rules;
commands outside the sandbox can still require approval.

Headless `agy -p` cannot show a prompt, so a tool it would ask about is denied.
To run one agy lane fully unattended without switching the whole workspace to
YOLO, set `"dangerously_skip_permissions": true` on that route or on `agy`.
Fusion then passes `--dangerously-skip-permissions`: **agy's sandbox and
permission prompts are off for that lane**, and it counts as ready for automatic
routing. Other agents ignore the key; Codex's `approval: "never"` still means
"do not prompt" with its sandbox intact. See the
[Antigravity sandbox documentation](https://www.antigravity.google/docs/sandbox?tab=cli).
If your shell or tools come from Nix and fail with a sandbox-blocked library
under `/nix/store`, add `read_file(/nix/store)` to the existing
`permissions.allow` list. This mounts the runtime files read-only; it does not
authorize unsandboxed commands. Preserve other permission rules.

Restricted-mode automatic selection requires explicit
`dangerously_skip_permissions: true`; sandbox/native command settings alone do
not qualify AGY. This readiness check is not a guarantee of authentication,
quota, or every tool's permission. Explicit AGY selections can still use a narrower custom allowlist.
`fusion doctor` and the UI show headless setup status. Permission denials stop
the attempt; resuming with Auto excludes that failed worker, while explicitly
selecting AGY lets you retry after correcting its configuration. Fusion never
adds a permission bypass implicitly in restricted mode; the explicit AGY setting
above is honored. Explicit `execution_mode: "yolo"`
uses the bypass flags described above. `agy` has no per-call budget flag, so cap its
cost with `timeout_seconds` and the workflow's `budget_usd`. OpenRouter models
are not in the `agy` host list — route those through the `orc` path instead.
`agy` is not (yet) a lead candidate; `fusion lead` accepts Claude, Codex or OpenCode.

## OpenCode settings

[OpenCode](https://opencode.ai) (`opencode run --format json`) is a fifth worker
harness. Any provider OpenCode is configured for works through
`model: "provider/model"`. Provider keys, base URLs and the model list belong
to OpenCode's own config (`~/.config/opencode/opencode.json`) and your local
`$ORC_HOME/fusion.json`, never to a repository. If your provider needs a
short-lived token or a custom CA bundle exported first, point `command` at a
small local wrapper that sets those variables and `exec`s `opencode "$@"`;
workers run headless, so the wrapper must not prompt.

```json
{
  "opencode": {
    "command": "opencode",
    "model": "anthropic/claude-sonnet-4-6",
    "opencode_agent": "",
    "disable_mcp": [],
    "bash_allow": [],
    "permission": {},
    "config": {}
  }
}
```

`command` is the binary (default `opencode`). `model` must include the provider
prefix (`provider/model`, e.g. `anthropic/claude-sonnet-4-6`). An explicit model
is required for automatic routing — OpenCode's configured default could be any
provider. Workers run as a dedicated OpenCode agent, `fusion-worker`, which
Fusion defines on the fly with the worker's permission policy; OpenCode applies
an agent's own permission rules over the global ones, so this keeps a user's
default `build` agent from loosening a read-only worker. Set `opencode_agent` to
run workers as one of your own OpenCode agents instead (the policy is applied
to it too). `agent` is not used for this: in a route it names the Fusion harness.

`empty_step_limit` (default 5; 0 disables) stops a worker after that many
consecutive empty responses: steps with no tokens and no text or tool call. A
provider or gateway that reports a failure as an empty successful stream would
otherwise leave OpenCode retrying until `timeout_seconds`; the run instead ends
as an error that names the cause.
`disable_mcp` is a list of MCP server names to disable for this worker (sets
`enabled: false` in `OPENCODE_CONFIG_CONTENT`). `bash_allow` is a list of
additional Bash patterns to permit in restricted write-mode workers.
`permission` is an object of extra OpenCode permission rules merged over Fusion's
base policy (last-matching-rule-wins). `config` is merged into
`OPENCODE_CONFIG_CONTENT`.

`reasoning_effort` passes `--variant` to `opencode run` and is validated
by OpenCode and the upstream provider at request time — the Codex local catalog
is not consulted. Accepted values are `none`, `minimal`, `low`, `medium`,
`high`, `xhigh`, `max` (not `ultra`, which is Codex-only). See
[OpenCode and --variant](model-effort.md#opencode).

Permissions are set through `OPENCODE_PERMISSION`, which OpenCode deep-merges
over the user's own `opencode.json(c)`. Read-only workers get a deny-by-default
policy with read tools and read-only Git commands allowed, plus trailing denies
for shell redirection, `tee`, `sed -i`, `find -delete`/`-exec` and `git push`; writers additionally get `edit` and an
allowlist-only Bash policy. Nothing is left at `ask`: a headless run
auto-rejects asks and ends the turn, while an explicit `deny` is returned to
the model which can work around it. Route-level `permission` overrides can
customize the policy, but core read-only denies (`edit: deny`,
`external_directory: deny`) and `git push*` are always re-applied last.

### One route per provider

Routes let automatic routing compare providers. Give each provider its own
`account` so a rate limit on one cools only that provider's lanes:

```json
{
  "routes": {
    "oc-sonnet": {"agent": "opencode", "model": "anthropic/claude-sonnet-4-6", "account": "oc/anthropic", "cost_tier": 2},
    "oc-gpt":    {"agent": "opencode", "model": "openai/gpt-5.5",              "account": "oc/openai",    "cost_tier": 2},
    "oc-gemini": {"agent": "opencode", "model": "google/gemini-2.5-pro",       "account": "oc/google",    "cost_tier": 1},
    "oc-grok":   {"agent": "opencode", "model": "xai/grok-4",                  "account": "oc/xai",       "cost_tier": 1}
  }
}
```

Use model ids from `opencode models`; the example ids are illustrative. Use
`fusion doctor` to check each route's command and
`fusion delegate --agent opencode --route oc-sonnet --read-only "Check the diff"`
to try a route before adding it to `decisions.auto_routes`.

## Older Grok clients

Grok workers default to `streaming-json` output. For an older CLI without that
format, set `grok.output_format` to `plain` in `.fusion.json`; public text still
appears live, but that format does not provide individual tool receipts or usage.
