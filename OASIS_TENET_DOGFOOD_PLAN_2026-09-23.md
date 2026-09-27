**Build Oasis through TENET; improve TENET from verified Oasis work**

Working recommendation and next-session handoff, September 23, 2026. This is a proposed operating method; no builds, model training, configuration changes or deployments were launched by writing it.

For the expanded state, ORC, learning, harness, platform and cleanup decisions, start with the [TENET vNext analysis](/Users/alectaggart/orc/TENET_VNEXT_ANALYSIS_2026-09-23.md). It refines the release gates and evidence requirements below using four delegated investigations.

**Decision**

Use Oasis as the main demanding external project. Deliver one small, working Oasis capability per cycle, capture the actual attempt through TENET, and turn demonstrated friction into a bounded TENET improvement. Retest the improvement on the originating task and on unseen work. Keep a smaller set of unrelated tasks to check whether improvements transfer.

TENET building itself is useful, but Oasis provides an independent reason for it to work. Maintain two definitions of success: Oasis must acquire verified behavior; TENET must reduce the effort, failures or cost required to deliver comparable behavior. Activity, journals and model-generated summaries do not satisfy either definition on their own.

**Observed starting point**

- [Oasis](/Users/alectaggart/Oasis) currently contains a [world/robotics blueprint](/Users/alectaggart/Oasis/research/original-blueprint.md), source research, and an economics model with tests. This directory was not a Git repository when inspected; no engine implementation was found there. Another session is actively adding research artifacts, which should be preserved.
- The blueprint combines procedural worlds, live behavior changes, persistent multiplayer state and robotics simulation. These are separate technical hypotheses. The economics workbook describes scenarios, not demonstrated engine performance.
- [Current TENET PRD](/Users/alectaggart/CascadeProjects/tenet-cli/PRD_TENET.md) emphasizes observing and connecting existing capabilities. Use that as a current starting point; do not reconstruct the state store from scratch based on the earlier audit.
- TENET had advanced to `b0ec9c4b` and ORC to `1f21ac9` during inspection. State-composition fixes and an ORC no-op-write rejection have landed since the original review. Reproduce remaining findings against current code before scheduling fixes.
- Read the [combined review](/Users/alectaggart/orc/ORC_TENET_REVIEW_2026-09-23.md) and [platform/state review](/Users/alectaggart/orc/TENET_PLATFORM_STATE_REVIEW_2026-09-23.md) as evidence from their pinned snapshots. They are not an assertion that every finding is still open.

**The first demonstrable outcome**

Provisional choice: a small live programmable world. The user was asked whether live-world or robotics-first should lead; change this slice if their answer chooses differently.

Start with a tiny simulation that can run headlessly, then add a minimal viewer. A fixed seed, input log and pinned executable produce a repeatable end state. Save/load preserves the required state. One behavior can be changed through a versioned module interface after compilation and acceptance checks. A deliberately broken/incompatible candidate must be rejected while the last working behavior remains usable. Choose the engine/module mechanism through a short implementation spike; the blueprint's DLL sketch is not yet a proven architecture.

The demonstration is: **request a behavior change → TENET supplies context and dispatches → ORC executes and verifies → Oasis exhibits the changed behavior → another harness/session can recover the evidence and continue.** Repeat with a bad candidate and an interrupted executor.

| Oasis task | Oasis acceptance | TENET capability exercised |
| --- | --- | --- |
| Deterministic headless world | Fixed seed/input/build reproduces state under a documented determinism/tolerance contract | Task specification, real build/test execution, evidence capture |
| Save/load and replay | Restart preserves required state; replay reaches the expected result | Durable journal/artifact identity, restart/resume and context recovery |
| One versioned behavior change | New behavior is observable; incompatible or failing candidate cannot replace the last good version | Dependency ordering, acceptance, independent review and artifact provenance |
| Minimal interactive viewer | A user can observe the change and inspect its test evidence | CLI/UI/MCP agree on workspace, run, result and freshness |
| Interrupted or failed attempt | No partial activation or false success; recovery is repeatable | Cancellation/recovery, accurate outcomes, durable state and harness handoff |

Do not require the whole MMO, procedural universe, runtime mutation and sim-to-real stack to succeed before calling the first cycle useful. Use the larger vision to choose later hard tasks. Treat claims such as universal sim-to-real transfer or 100× simulation as hypotheses requiring their own benchmarks.

**The repeated operating cycle**

1. **Choose a real Oasis task.** Pin the starting commit/environment, user-visible outcome, constraints, acceptance checks, timeout and resource budget. Identify what evidence would falsify success. When useful, confirm the acceptance test fails for the missing behavior or a deliberate bad implementation.
2. **Run through a supported TENET entry point.** Use the actual CLI/MCP/UI path the product promises. Record the selected harness/model, permissions, versions, attempt IDs and result. Do not silently bypass a broken product boundary with an ad hoc script and count it as TENET success.
3. **Verify independently.** Run the checks against the produced artifact and observe the product behavior. A separate review step can inspect quality; executable outcomes remain the authority for computable claims. Record human intervention, including when a person fixes the patch or acceptance criteria.
4. **Classify failures.** Distinguish Oasis implementation, task/spec ambiguity, missing domain knowledge, model capability, TENET integration, infrastructure, and evaluator defects. Keep the original failed trace and subsequent repair linked.
5. **Repair one relevant TENET defect.** Create an issue from the actual producer output, add a regression that reproduces it, make a bounded fix, and retry the originating task. Stop infrastructure work expanding beyond the next useful Oasis outcome. If repair blocks progress, use an explicit recorded manual escape and resume Oasis; that attempt is human-assisted.
6. **Promote what transfers.** Keep Oasis-specific fixes in Oasis. Generalize TENET changes only when another task or project supports the abstraction. Add the failure to regression coverage, then move on to a new Oasis capability.

An initial working allocation could be roughly two-thirds Oasis delivery and one-third TENET repairs/evaluation. This is a planning heuristic, not a measured optimum. Change it based on actual blocked time. If cycles repeatedly produce infrastructure changes without a working Oasis increment, narrow the next slice and stop adding abstractions.

**Keep the experiment interpretable**

Keep a known working TENET/ORC release or checkout as the runner and last-good fallback. Develop the candidate separately and pin the version used for each attempt. Stage candidate smoke tests before promotion; do not hot-replace the orchestrator underneath its own acceptance run.

Maintain a small benchmark set of frozen task snapshots. Compare:

- The same harness/model with minimal orchestration: baseline.
- The same harness/model with stable TENET: orchestration contribution.
- The same harness/model with candidate TENET: change under evaluation.

Use isolated starting states, equivalent task requirements, permissions and budgets. Repeat nondeterministic cases when deciding promotion. Count all retries and assisted work. Once a task has guided a fix, it becomes a regression case; reserve fresh tasks for transfer evaluation. Do not let the implementation candidate quietly weaken the reference grader.

The first handful of runs are diagnostic, not statistical proof. Keep a few unrelated tasks alongside Oasis so C++/simulation-specific improvements are not mistaken for general coding competence. Large models changing between runs are a confound: record upgrades and rerun a baseline.

**What to retain from each attempt**

Use the existing state-store/journal machinery and reconcile its schemas. Avoid creating a new independent logging subsystem. The required logical record is:

`task + initial state → observed tool actions/artifacts → executed checks → reviewed outcome → interventions + resource use`

Preserve task/spec version, workspace/run/node/attempt IDs, source and dependency versions, observable tool inputs/results, patch/artifact hashes, test commands/output, error/recovery sequence, human corrections, harness/model configuration, latency/cost coverage, acceptance and later settlement. Journals explain decisions and discoveries; build journals describe attempts; neither replaces the artifact and check receipt. Do not fabricate unavailable model reasoning or missing usage data.

Store large logs, recordings and simulation episodes as referenced artifacts with retention rules. Keep Oasis's high-frequency world/physics state in its engine data path; TENET should retain checkpoints, build evidence, episode metadata and references. Sharing provenance does not require routing every simulation tick through the engineering control plane.

The state layer must distinguish local commit, pending replication, cloud acknowledgment and UI projection freshness. Start local if necessary. Add cloud coverage only when authorization, durability, replay and duplicate handling pass. Eventually demonstrate the same task/result through CLI, MCP, local UI and platform, including failure and reconnect.

**Measure the product, not its activity**

| Metric | Why it matters |
| --- | --- |
| Independently accepted tasks / all attempted tasks | Measures useful completion, including failures in the denominator |
| Human active minutes per accepted task | Captures orchestration and recovery work the product should remove |
| Elapsed time and measured cost per accepted task | Includes failed attempts/retries; disclose incomplete provider usage |
| False-success count and rate | A green status without the promised result is a product defect |
| Recovery/harness-handoff success | Tests whether state helps work survive interruptions and session changes |
| Durable receipt/replication coverage | Separates retained local evidence, pending cloud writes and acknowledged writes |
| Fresh-task performance plus regression retention | Distinguishes useful generalization from memorizing a failure |

Keep correctness gates separate from convenience/cost metrics; do not trade a higher false-success rate for a prettier aggregate score. Use a short user walkthrough per milestone to catch confusing controls or missing evidence that automated checks miss.

**Training: three distinct data uses**

1. **Memory, recipes and retrieval first.** Verified procedures, recurring compiler errors, architecture decisions and recovery recipes can help future runs before any weight training. Measure whether retrieval changes task outcomes rather than assuming more context helps.
2. **Laya / TENET decision models.** Choose one bounded question with available evidence, such as identifying a failure class or proposing the next diagnostic. Keep deterministic checks deterministic. Blind labels to the model prediction, retain uncertainty/abstention, split by task lineage, and evaluate against simple baselines. A single successful run does not label its harness as the best possible choice; routing comparisons need comparative evidence. Start advisory and activate only after calibration and downstream checks.
3. **A trainable coding helper, if the data justifies it.** Collect successful, executable task trajectories with tool observations, plus reviewed failure-to-repair examples. Begin with a narrow useful role. Fix the existing export/train/evaluate/adapter-load/output-retention loop before claiming LoRA works. Compare base versus tuned models on held-out task families and real downstream outcomes under the same budget; preserve a rollback. Logs alone are not an improvement guarantee, and this plan does not assume the selected frontier model can be fine-tuned.

Oasis NPC behavior and robotics policies are a separate learning problem from TENET's engineering assistant. They need their own episodes, rewards, validation and deployment gates. Keep datasets and evaluation targets distinct while sharing storage/provenance facilities where useful.

Research supports this emphasis on executable tasks and outcomes: [SWE-Gym](https://arxiv.org/abs/2412.21139) studies training engineering agents and verifiers with real environments and tests. [Anthropic's evaluation guidance](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) distinguishes transcripts from actual environmental outcomes and separates capability evaluation from regression coverage. Neither establishes that this particular TENET/Oasis loop will improve without measuring it.

**Prompt for the new Codex session**

> Continue the TENET/Oasis dogfood program using this brief and the current repositories. Read applicable AGENTS.md/CLAUDE.md, the current TENET PRD, and the two review reports. Preserve concurrent work and recheck the findings against current HEADs. Inspect /Users/alectaggart/Oasis and confirm the first demonstrable milestone from the user's latest direction. Use existing TENET capabilities, making missing connections observable. Keep a stable runner separate from candidate changes. Establish a versioned Oasis workspace without moving or overwriting the existing research. Choose one small executable Oasis task, define independent acceptance and an attempt budget, and deliver it through the real TENET/ORC path. Capture the actual artifacts, failures, interventions and result. If TENET blocks that task, reproduce and repair the smallest relevant defect, then resume the same Oasis task. End the cycle with a demonstrable Oasis increment, a truthful TENET outcome, and a concise record of what improved and what remains. Do not begin with a broad rewrite or model training campaign.

Agree on an attempt budget when starting the first execution cycle. This session inspected the current work and wrote this handoff; the proposed Oasis/TENET build cycle has not been launched.
