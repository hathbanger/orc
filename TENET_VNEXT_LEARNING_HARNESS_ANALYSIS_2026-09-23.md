# TENET vNext: execution, harnesses and learning

Analysis date: 2026-09-23. Source revisions: ORC `1f21ac9a002b46ad715e43880f6a0023e297f6c4`; TENET CLI `b0ec9c4b3e26fcc16c6fec697a31c41994f2e172`. Scope: the execution/learning boundary. State storage and platform/UI have separate companion reviews. No production source, configuration, model, installed MCP registration or deployment was changed. All diagnostic execution used fixtures; no paid harness/model calls were made.

**Concurrent-change update:** after these probes, ORC `f1de13d` added plan-declared `required_files` that write nodes inherit and enforce for existence/change. This repairs the missing plan-to-artifact connection described in finding 5. Its `verification` commands are explicitly recorded for reviewer use, not executed by the coordinator; malformed/absent contracts add no new gate. The earlier dummy-file reproduction is evidence at `1f21ac9`, not a fresh test of every generated path at `f1de13d`. Behavioral acceptance remains a separate requirement. CLI also advanced to `48820464`, routing three state producers through the shared store; see the state report's update. Historical probe receipts and counts below are preserved.

## Recommendation

Build one coherent TENET product contract around existing mechanisms. Preserve ORC as an independently usable execution implementation. Do not combine two orchestrators by letting each independently own retries, worktrees, budgets, acceptance and publication for the same run.

The strongest division is:

- TENET owns project intent, task/spec identity, context selection, work portfolio, durable evidence indexing, product UI, and learning eligibility/promotion policy.
- A selected executor owns one run's worktree, process tree, node scheduling, retries, cancellation and terminal execution receipt. ORC/Fusion can fill this role; TENET's existing build loop can also fill it for a different run.
- The chosen harness owns its native model interaction and tool execution, bounded by the executor's declared permissions. Codex, Claude Code and Pi are not interchangeable merely because they can accept a prompt.
- Behavioral evaluators produce observations linked to an immutable artifact and evaluator version. A semantic reviewer can reject or escalate; it cannot convert failed deterministic evidence into acceptance.
- A learning service consumes approved, provenance-preserving views of those records. Its choice of Laya, an MLP, a transformer action selector or a future coding LoRA is internal implementation, not four incompatible definitions of “learning.”

This is consistent with the current convergence document's preference for mechanism exchange and an optional runtime shim, rather than absorption ([PRD_ORC_CONVERGENCE.md:64](/Users/alectaggart/CascadeProjects/tenet-cli/PRD_ORC_CONVERGENCE.md:64)). A shim alone does not resolve execution ownership or product coherence.

## What actually runs today

| Path | Present behavior | Implication |
|---|---|---|
| ORC generated build | Intake → explore → plan → implement → independent review; executable workflow owns retry/budget/cache; optional publication | A real workflow executor, worth retaining |
| ORC worker | `make_task` → route policy → native harness command → parsed handoff and receipts | Richer structured harness observations than a bare CLI exit |
| TENET build session | Agent instruction files → registry-selected API/CLI → fingerprint/outcome inspection → optional guard → frozen eval → keep/revert → subsequent rounds/publication | A second real execution owner, not merely an ORC frontend |
| TENET Peter local route | Healthcheck → one chat completion → cost/tokens → return | Output is discarded; this is not a working tool-using local coding executor |
| ORC learning | Decision observation → label drafts/review → grouped export → supervised decision-head candidate → evaluation/calibration → optionally qualified action | A coherent bounded classifier lifecycle, currently untrained for this workspace |
| TENET policy learning | Multiple buffers → lossy projection → reward regression or action classification → checkpoint serving | Important mechanisms exist, but producer/trainer/serving semantics disagree |

Relevant paths: [fusion_build.py:146](/Users/alectaggart/orc/fusion_build.py:146), [fusion_workflow.py:657](/Users/alectaggart/orc/fusion_workflow.py:657), [agent-session.ts:829](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/agent-session.ts:829), [agent-session.ts:1165](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/agent-session.ts:1165), [agent-session.ts:1073](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/agent-session.ts:1073), [peter.ts:423](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/peter.ts:423).

TENET's registry contains seven entries: local Qwen, Anthropic API, OpenRouter API, Claude CLI, Codex CLI, Pi CLI, Aider CLI. Aider explicitly cannot dispatch. There is no `orc-fusion` entry. ORC workers support Codex, Claude, agy and Grok; Pi is not an ORC workflow agent. Preserve truthful capability distinctions rather than announcing universal harness parity ([agent-runtimes.ts:28](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/agent-runtimes.ts:28), [fusion_workflow.py:162](/Users/alectaggart/orc/fusion_workflow.py:162)).

## Findings that block trusting learning or acceptance

### 1. Training projection leaks the target and removes evidence that the outcome was synthetic

`train combine` maps a surface state hash into values named `correctness`, `coverage`, `architecture`, `value` and `risk`. Those numbers are hash bytes, not measured quality. It sets the projected pre-action `state.recent_deltas` to the very reward being predicted. It omits the original `metadata.synthesized_outcome` and original outcome. The Qwen-ask projection also puts its current reward into `recent_deltas`, and defines that reward using output length and whether a briefing was used, without a correctness observation.

Sources: [train-combine.ts:62](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/train-combine.ts:62), [train-combine.ts:91](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/train-combine.ts:91), [train-combine.ts:107](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/train-combine.ts:107), [train-combine.ts:116](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/train-combine.ts:116).

The actual Python trainer includes recent deltas in the input, uses `composite_delta` as target, and random-splits rows. Its loader does not filter `tuple_status` or synthetic evidence. Preprocessing normalization is fit before the split. A numeric zero reward is often dropped, selecting away legitimate neutral/no-op outcomes. These choices can yield apparently strong validation without learning what will make the next task succeed ([train-policy-head.py:164](/Users/alectaggart/CascadeProjects/tenet-cli/scripts/train/train-policy-head.py:164), [train-policy-head.py:202](/Users/alectaggart/CascadeProjects/tenet-cli/scripts/train/train-policy-head.py:202), [train-policy-head.py:483](/Users/alectaggart/CascadeProjects/tenet-cli/scripts/train/train-policy-head.py:483)).

**Controlled reproduction:** the checked-in producer, fed one fake surface tuple and one fake Qwen tuple, emitted both targets verbatim into the input feature `recent_deltas`. The surface's `synthesized_outcome: true` vanished; its “correctness” became `0.6598039215686274`, calculated from a hash. Exact inputs and outputs: [training-probe.json](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-learning-2026-09-23/training-probe.json); rerunnable [probe-training.cjs](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-learning-2026-09-23/probe-training.cjs).

**Required correction:** keep observation-at-decision-time separate from later outcome; retain source/provenance and settlement; prohibit target-derived features; remove invented quality dimensions; train/evaluate observed and synthetic data separately. Hashes identify evidence; their numerical bytes are not quality features.

### 2. The “RL surface” driver often assigns reward without executing the chosen action

The driver observes, enumerates, epsilon-selects an action, immediately calls `synthesizeOutcome`, then computes reward and records a tuple. There is no execution step between selection and outcome. Examples: calibration treats the current verdict as a future proxy; refactor safety sets `testsHeld` from the action name. These are synthetic exercises, not observed policy improvement ([rl-surface.ts:182](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/rl-surface.ts:182), [rl-surface.ts:241](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/rl-surface.ts:241), [rl-surface.ts:414](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/rl-surface.ts:414), [rl-surface.ts:467](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/rl-surface.ts:467)).

Keep these fixtures useful for schema/logic tests, explicitly typed as synthetic. For actual policy data, record `decision → selected action → execution receipt → delayed observed outcome`, including decision propensity and a pending/settled lifecycle. The lifecycle type already exists; wire it through the producer, projection and trainer ([training-buffer.ts:42](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/training-buffer.ts:42)).

### 3. Default local training and serving do not use the same embedding model

Autoloop defaults to local `bge-small`. The trainer exports an `embedder` field and explicitly warns that serving must match it. The TypeScript weight interface omits that field; `embedText` requires `STRATUS_API_KEY` and always requests `stratus-x1ac-base`. Matching vector width would not fix a semantic embedding-space mismatch ([train-autoloop.ts:122](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/train-autoloop.ts:122), [train-policy-head.py:367](/Users/alectaggart/CascadeProjects/tenet-cli/scripts/train/train-policy-head.py:367), [policy-head.ts:14](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/policy-head.ts:14), [policy-head.ts:158](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/policy-head.ts:158)).

**Controlled reproduction:** a fixture checkpoint marked `embedder: bge-small` throws `STRATUS_API_KEY not set`. With a fake key and mocked fetch, it requests Stratus embeddings. No external API was contacted. Require one versioned model bundle containing preprocessing, tokenizer/embedder identity, input schema, weights, calibration and training/eval manifest; validate it before serving.

### 4. “Reward,” “confidence,” “agreement” and “success” are different quantities

The v2 `predictReward` path returns action-class confidence (or a discounted alternative probability); v1 returns a rescaled regression estimate. Peter logs this as predicted reward beside actual eval delta. Those numbers do not share units ([policy-head.ts:408](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/policy-head.ts:408), [peter.ts:1088](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/peter.ts:1088)). The fixture returned precisely `0.93` “reward” from a `0.93` action-selection confidence.

Use typed outputs: `ActionDistribution`, `PredictedOutcome` with target/unit/horizon, `DecisionLabelAgreement`, `ObservedEvaluation`, `AcceptanceDecision`. Never compare a class probability directly with an eval delta. Also, the trainer's direction accuracy is evaluated against normalized targets; with per-source centering, above-source-average is not the same as positive real-world improvement ([train-policy-head.py:323](/Users/alectaggart/CascadeProjects/tenet-cli/scripts/train/train-policy-head.py:323)).

### 5. ORC's no-change fix is real; general acceptance is still weak by default

Commit `1f21ac9` rejects a write node that leaves its Git tree unchanged. That fixes the original no-op reproduction. Generated implementation/review nodes still require only nonempty `summary` and `tests`; the planning prose never becomes executable `acceptance.checks` or required artifacts automatically. A dummy file containing `x`, with the worker claiming “No implementation was performed” and “TESTS: not run,” is accepted by the repository's current fixture. A workspace without Git deliberately skips the no-change gate ([fusion_build.py:147](/Users/alectaggart/orc/fusion_build.py:147), [fusion_workflow.py:622](/Users/alectaggart/orc/fusion_workflow.py:622), [test/fusion_test.py:356](/Users/alectaggart/orc/test/fusion_test.py:356)).

**Controlled reproduction:** Git/no changes → failed; Git/dummy file → success; non-Git/no changes → success. [orc-probe.json](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-learning-2026-09-23/orc-probe.json).

Keep the fix and add an executable acceptance contract before dispatch. Record checks as coordinator-owned observations with command, environment, artifact hash, exit status and output references. Report “worker completed, acceptance unverified” when no adequate evaluator exists. A fingerprint shows that something changed; it cannot establish that behavior improved.

Laya's semantic check is correctly one-way: structural failures remain failures; only a qualified active classifier can add rejection ([fusion_policy.py:200](/Users/alectaggart/orc/fusion_policy.py:200)). Preserve that invariant.

### 6. TENET's local runtime still throws away its answer

The local response parser only extracts usage. It has no completion-content return field and no tool loop; Peter treats a non-null cost object as completion and returns. A valid fixture answer was lost exactly as in the earlier review ([agent-runtime-local.ts:94](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/agent-runtime-local.ts:94), [peter.ts:429](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/peter.ts:429)).

Fix output capture first; advertise this adapter as text/structured inference until tool execution, artifact handling and acceptance are implemented. A chat endpoint answering `/models` does not demonstrate coding capability.

### 7. Some learning product surfaces are simulated

`RLManager.getAgentPerformance` emits fixed example improvements and random update/training times; insights are static examples. Production commands instantiate it, including `tenet-agents`. This must be labeled demo or replaced with actual records before claiming continuous learning ([rl-manager.ts:161](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/rl-manager.ts:161), [rl-manager.ts:194](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/rl-manager.ts:194), [rl-manager.ts:213](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/rl-manager.ts:213), [tenet-agents.ts:50](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/tenet-agents.ts:50)).

Similarly, module existence does not establish that a model is trained, active, calibrated or useful. The fresh PRD's MCP count is not source truth: actual source construction advertises **33** tools (25 hub + 5 transaction + 3 MCP-only), not its claimed 37. ORC advertises seven. Counts are an inventory, not interoperability evidence ([context-hub-mcp.ts:632](/Users/alectaggart/CascadeProjects/tenet-cli/src/mcp/context-hub-mcp.ts:632)).

## Laya: retain the good design, constrain the claims

Current local summary: **36 decisions, 52 eligible questions, zero reviewed decisions/approved answers, zero candidates/evaluations/exports, zero qualified buckets, mode shadow**. The mechanism exists; this workspace has not demonstrated personalized learned improvement. [laya-summary.json](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-learning-2026-09-23/laya-summary.json).

Retain these mechanisms:

- Teachers do not see the classifier's predictions, policy applications or prior labels. Evidence is tied to the attempt. Instructions distinguish worker claims from verification and explicitly require abstention where route optimality is unobserved ([fusion_labeling.py:22](/Users/alectaggart/orc/fusion_labeling.py:22), [fusion_labeling.py:156](/Users/alectaggart/orc/fusion_labeling.py:156)).
- Drafts remain unverified until human or explicitly configured council approval; automatic approval preserves human-reviewed labels ([fusion_labeling.py:288](/Users/alectaggart/orc/fusion_labeling.py:288), [fusion_labeling.py:307](/Users/alectaggart/orc/fusion_labeling.py:307)).
- Export retains label provenance, stable group split and model/schema identity. Candidate evaluation reports group/input ancestry contamination and majority/shuffled-state controls ([fusion_decisions.py:285](/Users/alectaggart/orc/fusion_decisions.py:285), [fusion_laya.py:170](/Users/alectaggart/orc/fusion_laya.py:170), [fusion_laya.py:196](/Users/alectaggart/orc/fusion_laya.py:196)).
- Active actions require an explicitly enabled kind, matching calibrated model identity, untruncated input, qualified bucket and confidence re-derived under current calibration ([fusion_decisions.py:419](/Users/alectaggart/orc/fusion_decisions.py:419)).
- Training freezes encoder and action head, updating decision heads with cross-entropy. This is supervised decision-head training, not coding LoRA or end-to-end reinforcement learning ([fusion_laya.py:237](/Users/alectaggart/orc/fusion_laya.py:237)).

Remaining epistemic boundaries:

1. **Council agreement is agreement, not independently observed truth.** Code enforces different worker names, not different underlying model families or training provenance. A fixture with different harnesses and identical model identity is accepted as unanimous. Expose model identity and correlated-source warnings in labeling records; validate approval quality against a separately adjudicated sample. Even genuinely different families can share mistakes.
2. **Evidence citation presence is not evidence verification.** Suggestions must cite known E identifiers, but the bundle largely contains original input and worker result/answer. Add coordinator test receipts, artifact snapshots and later outcomes; keep original-time facts separate from hindsight.
3. **Routing success is not the best-route label.** One successful action cannot show another route was worse. For route optimization, use matched tasks or carefully scoped randomized choices with logged candidate set, availability, propensity, budget and outcomes. Use abstention for unobserved alternatives.
4. **Metric denominators matter.** Laya evaluation accuracy is per labeled validation question. Garden agreement compares the model with retained approved labels across reviewed records; it is not held-out accuracy. Calibration qualifies with at least 20 train groups, 20 confident validation groups and ≥95% selective accuracy; accuracy itself remains question-weighted, allowing correlation/unequal group sizes ([fusion_learning.py:103](/Users/alectaggart/orc/fusion_learning.py:103), [fusion_laya.py:157](/Users/alectaggart/orc/fusion_laya.py:157), [fusion_decisions.py:507](/Users/alectaggart/orc/fusion_decisions.py:507)).
5. **Qualification is a minimum engineering gate, not strong risk evidence.** Even 20/20 independent successes yield only about an 84% lower bound under a two-sided 95% Wilson interval. Use task/group-level uncertainty, class-specific false-accept/false-reject rates, cost of error, coverage and drift; do not present `qualified` as universal reliability.
6. **Repeated candidate selection needs a final holdout.** Preserve lineage checks; add sealed task-family/repo/time-separated tests so repeated evaluation does not overfit the same validation set. Group by originating task family, not merely by newly generated run ID.

## A usable harness/SDK boundary

Build this on the existing outcome and manifest types, with migration tests; do not create another detached schema module.

`RunRequest` should identify workspace, task/spec version, parent run/attempt, base commit, evaluator bundle, allowed capabilities, budget/deadline, context snapshot and idempotency key. `RunHandle` must be durable before worker/model cold start. `RunReceipt` should reference actual harness/model/version, effective permissions, artifacts/diff, logs, tool/check observations, attempt graph, usage with coverage, terminal reason, acceptance and publication as separate states.

An executor interface needs capabilities, start, status/events, cancel and resume. “Supported” is per capability: structured output, edit, shell, network, scoped filesystem, MCP, resume, cancellation, usage capture and observation fidelity. Unsupported operations return an explicit unsupported/blocked result. Do not silently strengthen permissions or switch model/harness without recording the effective choice.

One execution owner invariant:

- If TENET asks ORC to execute a workflow, ORC owns its internal retries/worktree and returns child attempt receipts. TENET may retry submitting the same idempotent request, but must not start a second workflow simply because an RPC response was lost.
- If TENET owns a build loop, a worker adapter performs one bounded attempt. Wrapping a complete ORC workflow as an opaque one-attempt command without declaring nested budgets and publication policy is unsafe product semantics.
- Budgets include failed/retried/canceled attempts and labeling/evaluation costs. Cancellation propagates to descendants; reconnect/resume adopts an existing handle. Publication is one separately acknowledged action after acceptance.

Permission gap: ORC defaults to restricted execution and a scoped Codex workspace/Git profile. TENET autonomous Codex dispatch defaults to `--dangerously-bypass-approvals-and-sandbox`; Claude defaults to skipping permissions. “Autonomous” and “unrestricted” should be distinct capabilities in the adapter contract ([fusion_core.py:31](/Users/alectaggart/orc/fusion_core.py:31), [fusion_core.py:1095](/Users/alectaggart/orc/fusion_core.py:1095), [agent-runtimes.ts:134](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/agent-runtimes.ts:134), [agent-runtimes.ts:157](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/agent-runtimes.ts:157)). TENET's outcome gate also defaults to **shadow**, so type/fingerprint checks being implemented does not mean they are enforced ([agent-outcome.ts:115](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/agent-outcome.ts:115)).

MCP should expose the same application services as CLI/UI rather than constitute another execution stack. Preserve a small common task/context/evidence/run API; expose expert tools on demand. Contract tests should initialize the actual server, list tools, invoke a fixture run, reconnect/poll/cancel and validate every response.

Remaining local MCP issues: ORC `fusion_status` places a list in `structuredContent`; run start can synchronously wait 90 seconds for registration in its serial stdio request loop. TENET's command-based Codex registration ignores the supplied project scope and reports global configuration. Fix these before presenting installation as harness parity ([fusion_core.py:1593](/Users/alectaggart/orc/fusion_core.py:1593), [fusion_core.py:1643](/Users/alectaggart/orc/fusion_core.py:1643), [fusion_mcp.py:355](/Users/alectaggart/orc/fusion_mcp.py:355), [fusion_mcp.py:420](/Users/alectaggart/orc/fusion_mcp.py:420), [mcp-clients.ts:192](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/mcp-clients.ts:192)).

## Learning roadmap with measurable gates

| Phase | Deliverable | Evidence required to advance |
|---|---|---|
| 0: trustworthy run | One Oasis task through chosen executor, durable evidence, rejection of broken code, restart/cancel recovery | Behavioral checks fail before/fault injection and pass only on the fixed artifact; reconnect adopts same run; no unexplained orphan processes |
| 1: useful memory | Retrieve proven procedures and previous failures into a fresh task | Compared with same-model baseline: accepted outcomes and human minutes improve without harming unrelated tasks; every retrieved fact links to evidence |
| 2: observed decision dataset | Pending decisions joined to actual receipts and delayed outcomes | No current-outcome leakage; provenance survives all projections; synthetic rows separate; failed/refused/neutral outcomes retained; independent task groups |
| 3: bounded learned advice | Laya or another bounded head in shadow for one decision kind | Beats deterministic/majority/shuffled controls on held-out task groups; calibration, coverage, error costs and uncertainty reported; model-family/label-source breakdown |
| 4: limited active policy | One reversible low-impact action with fallback | Prospective canary improves accepted-task cost or human time at fixed quality; total failures/retries counted; drift and rollback tested |
| 5: narrow coding model | Train an explicitly licensed/adaptable local model for a repeated, verifiable subtask | Pinned training/serving pipeline reproduces; clean task-family holdout; tool-use and patch correctness tested; lower cost/latency at required acceptance rate |

Do not train “TENET builds anything” from journals. Journals help explain trajectories, select retrieval and identify missing capabilities. Training coding behavior needs executable trajectories, patches, pre-action observations, command results, evaluator identity, interventions and later regressions. Some fields are labels; some are context; some are only narrative. Keep those roles explicit.

LoRA is currently a research path, not an integrated product promise. README describes `local-distill`, `peter --dump-predictions` and serving an `--adapter`, but the current CLI does not register those options/command. The wrapper loads filtered pairs but launches training against the original data path, and omits alpha/dropout from its command. Pin a compatible MLX version and prove a tiny train → export → reload → infer → behavior-eval cycle before allocating a real corpus or claiming a local trained coding agent ([scripts/distill/README.md:22](/Users/alectaggart/CascadeProjects/tenet-cli/scripts/distill/README.md:22), [scripts/distill/lora_finetune.py:155](/Users/alectaggart/CascadeProjects/tenet-cli/scripts/distill/lora_finetune.py:155), [src/index.ts:2663](/Users/alectaggart/CascadeProjects/tenet-cli/src/index.ts:2663)). No LoRA run was performed in this review.

## Cleanup tied to product delivery

1. Quarantine target-leaking/synthetic projections from production training eligibility; preserve the original records for reproducible diagnosis. Do not delete history or call synthetic data inherently useless.
2. Replace simulated learner status with measured statuses (`not trained`, `shadow`, `candidate`, `qualified`, `active`, `stale`) backed by receipts.
3. Consolidate runtime selection and outcome normalization across Peter/build/API/local; rename functions such as `runClaudeCode` where they now select any harness. Keep compatibility wrappers while consumers migrate.
4. Merge learning manifests/provenance/promotion plumbing, while retaining separate model objectives. Avoid a single undifferentiated “reward” field for route ranking, class confidence, reviewer preference and task completion.
5. Make the same checkpoint input adapter serve training and inference. Remove stale docs/unsupported commands or label them experimental with executable probes.
6. Add adapter conformance tests using actual producer payloads and malformed/missing responses. Counts and mocked unit coverage alone do not establish product integration.
7. Deprecate duplicated implementation only after consumers migrate and a behavioral replacement passes. The goal is fewer independent sources of truth and more reliable user journeys, not an arbitrary line-count reduction.

## Experiments still needed

- Same task/base/model/budget, direct harness versus TENET context versus TENET+ORC: compare accepted behavior, human interventions, latency/cost and failure recovery. The effect of orchestration is presently unmeasured here.
- Model-family and human-adjudicated audit of auto-labels, including counterfactual routing questions and all-abstention cases.
- Native Codex/Claude/Pi integration conformance under actual installed versions: output capture, MCP discovery, permissions, cancellation and resumed sessions. No paid native session was launched during this review.
- Small end-to-end train/serve experiment after leakage/provenance/embedding defects are fixed; current isolated probes establish wiring defects, not model quality.
- Prospective useful-outcome measurement on Oasis plus unrelated tasks; old eval deltas must not silently become gold labels. The parent review separately probes evaluator adequacy.

## Validation and evidence

- [Evidence README, rerun instructions and hashed manifest](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-learning-2026-09-23/README.md).
- Six focused ORC acceptance/one-way classifier tests passed.
- 67 focused TENET runtime/policy-head/Codex-registration/spawnable-runtime tests across four files passed. The diagnostic failures above coexist with those passing tests.
- [Training projection fixture and exact outputs](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-learning-2026-09-23/training-probe.json).
- [Runtime output, embedding identity, v2 semantics, runtime inventory and MCP count fixture](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-learning-2026-09-23/runtime-probe.json), [rerun script](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-learning-2026-09-23/probe-runtime.cjs).
- [ORC acceptance and same-model council fixture](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-learning-2026-09-23/orc-probe.json), [rerun script](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-learning-2026-09-23/probe-orc.py).
- [Current local Laya summary](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-learning-2026-09-23/laya-summary.json).

Read-only source inspection establishes the listed paths; fixture execution establishes the bounded reproduced behavior. Neither demonstrates deployment state, broad harness parity, causal policy improvement or production readiness.
