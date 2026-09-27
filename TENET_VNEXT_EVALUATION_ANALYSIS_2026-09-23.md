**TENET vNext: evaluation, evidence and the Oasis experiment**

September 23, 2026. Root-agent investigation accompanying the state, learning/harness and platform investigations. Current inspected CLI: `b0ec9c4b`; ORC: `1f21ac9`. This analysis changes review artifacts only.

**The crucial result**

TENET's current hand-written EPIC 5 reference evaluator scored **2/13 before and 13/13 after cosmetic edits**, while the emitted JavaScript of every edited source file remained byte-identical. This reference cannot establish that the promised refusal/outcome behavior works. Treat it as a structural diagnostic until replaced or supplemented by executable behavioral checks.

The [reproducer](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-analysis-2026-09-23/evaluator-probe.cjs) copies the actual source files into a temporary fixture. It adds comments mentioning imports/calls/outcome words and changes the quote style of one type literal. It executes the original reference `evaluate()` body, omitting only its CLI entrypoint. TypeScript transpilation with comments removed verifies that none of these changes alters emitted runtime code. [Receipt](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-analysis-2026-09-23/evaluator-probe.json). Archived source and hashes are beside it; the [reproduction notes](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-analysis-2026-09-23/README.md) document a successful rerun using that snapshot.

**What the current mechanisms prove**

| Mechanism | What it establishes | What remains unproven |
| --- | --- | --- |
| A check is red on the starting revision | That implementation does not meet this check | That the check captures the user's intended behavior |
| Hand-written EPIC 5 reference | Source contains selected words/import-like patterns | A refused run is actually persisted, read, excluded from success and not retried |
| Generated/reference baseline parity | Two scalar scores differ by at most 0.25 | Equivalent semantics, mutation sensitivity or downstream acceptance quality |
| A generated eval has headroom | Some checks remain to be satisfied | That satisfying them will complete the task |
| File-touch epistemic map | Journal activity, active presence and documentation markers | Correct understanding, complete evidence or a calibrated probability |
| A positive score delta | Improvement according to that scorer | Useful work, independent acceptance, merge, settlement or model learning |

The reference [uses text patterns](/Users/alectaggart/CascadeProjects/tenet-cli/eval/build/epic-5-refusal-edges.reference.ts:57). Its “a-refused-node-does-not-retry” check requires the words `refused` and `retry` to occur; it does not execute a dispatch. Missing files can also pass its two regression guards. This is narrower than the comments' “ground truth” claim.

[compareEvals](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/eval-parity.ts:95) bases its verdict on the difference between scalar baselines. The saved counterexample passes it two 0.5 scores whose failing requirements are unrelated; it reports `parity`. Its own implementation says exact check equality is not intended, which is reasonable for a diagnostic. The current PRD's statement that eval adequacy has an exact computable answer should therefore be narrowed: a score difference is computable; adequacy against open-ended intent is not established by that difference.

The [parity CLI](/Users/alectaggart/CascadeProjects/tenet-cli/scripts/eval-parity.ts:86) returns 0 for `unmeasured` and `generated-overreaching`. Its active subprocess helper also throws on a nonzero eval exit before comparison, unlike the unused helper that preserves failing output. Give infrastructure errors, unmeasured results and actual comparisons distinct machine-readable states before using this command as a release gate. This last point is source inspection, not a separate subprocess reproduction.

The [epistemic mapper](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/epistemic-boundaries.ts:85) marks confidence high when a file is active or has five journal touches; a purpose header is enough for medium. Preserve that useful familiarity/activity signal, but label it accordingly. A real uncertainty record needs a claim, evidence, revision scope, consequence of being wrong, and the next discriminating experiment.

**A stronger acceptance contract**

For a bounded change, retain the task/requirement version and its acceptance contract outside the candidate's implicit control. Preserve regression checks that already pass; only new-capability checks need to fail initially. “Every check must fail before implementation” would incorrectly penalize preservation of existing behavior.

Each material criterion should link to an executable check or a clearly identified human/domain review. Run checks against the actual produced artifact and capture command, working directory, artifact hash, environment, exit/result, output and timeout. Do not treat imports, source spelling, line counts or a worker's statement as evidence of the behavior itself.

Before trusting a generated grader, test it against a small discriminating set:

| Candidate | Expected result |
| --- | --- |
| Starting implementation missing the behavior | New behavior check fails |
| Known valid implementation | Relevant checks pass |
| Cosmetic/source-marker-only change | Still fails |
| Partial implementation missing one required outcome | Corresponding criterion fails |
| Deliberate regression/no-op/test-skipping change | Fails or explicitly unverified |
| Checker crash, timeout, empty result, stale artifact | Unmeasured/error; cannot establish acceptance |

For refusal/outcome specifically, run a real bounded fixture through dispatch: emit the terminal refusal; observe the persisted outcome; read it through the actual report/projection; verify dependents and retries; verify it is counted separately from accepted success. Use the real producer's output rather than a handwritten lookalike envelope. Add a wrong-behavior mutant to establish the check detects the failure.

For an Oasis simulation, freeze build/environment/input log and state-equivalence rules. Verify save/load and rollback through execution. Domain claims such as physics fidelity and sim-to-real transfer need separate expert/empirical validation; a coding agent passing its software suite cannot establish them.

**Use the same evidence without collapsing its meanings**

The common record should connect intent, task/spec, run/attempt, selected executor, artifacts, check receipts, review, accepted outcome, later merge/settlement, and human intervention. Keep the individual records and typed links. Do not compress these stages into one `success` boolean or scalar reward.

Use the raw trace to diagnose; use verified outcomes to evaluate; use a reviewed/versioned eligibility rule to build training data. Keep pending and synthetic rows for diagnostics when useful, but preserve their provenance and exclude them from claims about measured task success. A failed attempt is valuable evidence; the desired behavior must be independently established before it becomes a supervised target.

Separate kinds of confidence in UI and storage: model action probability, estimated reward, label agreement, test coverage, observed task success rate, and data replication coverage. They have different denominators and cannot be substituted for one another.

**Oasis as a controlled development program**

Keep a stable TENET/ORC runner and a candidate checkout. Freeze a small set of task starting snapshots and their independent graders. For each task class compare the same harness/model and resource limits under minimal orchestration, stable TENET, and candidate TENET. Run enough repeated trials to see variability before promotion, especially when routing or learned decisions change.

Keep failed attempts and human-assisted completions in the denominator. Report independently accepted tasks, human active time, total elapsed time/cost including retries, false-success count, recovery/handoff success, and durable evidence coverage. If provider usage is incomplete, report the known portion and its coverage; do not report zero as actual cost.

Your [compute-economics strategy](/Users/alectaggart/idiot-index-strategy.md) provides a useful objective: reduce cost per intent satisfied while maintaining quality. Treat the cheapest verified configuration observed on a comparable task class as an empirical reference, not a proven universal cost floor. Recheck it after model/harness changes; cheap tokens with more failed attempts can increase total cost.

Use Oasis's first working slice to generate integration failures, then alternate a bounded TENET repair with another Oasis increment. Add a few unrelated task families. Once a task has influenced a fix, it is regression/development data; reserve other tasks and later milestones for held-out evaluation. Group retries, related patches and derived tasks together when splitting data to prevent leakage.

**Epistemic delegation as a product feature**

A useful delegation request is a bounded unknown, not a request for generic “more agents.” Its record should contain: the question, why it blocks a decision, current evidence, allowed inspection/experiments, time/resource ceiling, and what would resolve it. The returned result should distinguish observation, inference, recommendation and remaining unknowns, with source revision/artifact links.

Merge findings through review of the underlying evidence. Agent agreement alone does not establish independence or truth. Expire conclusions when their dependencies change, and resample important assumptions during new milestones. This is the part of the current review workflow that is worth making native in TENET.

**Validation and limits**

The new executable result is the source-loaded reference/cosmetic-change probe and the scalar-parity counterexample. No production code was modified; no real coding run, paid model call, deployment or training job was launched. This pass did not independently establish the entire reference evaluator's sensitivity/specificity or the real-world validity of Oasis physics.

[Anthropic's agent-evaluation guidance](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) distinguishes observed outcomes from transcripts and recommends separate capability/regression evaluation. [SWE-Gym](https://arxiv.org/abs/2412.21139) studies executable engineering tasks, trajectories and verifiers. These support the proposed experimental method; they do not prove TENET's particular implementation is effective.
