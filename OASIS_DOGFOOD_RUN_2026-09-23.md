# TENET builds Oasis: first recorded product cycle

The first Oasis increment was implemented by a real native Codex worker launched
through the TENET CLI and an authored ORC/Fusion workflow. It passed a frozen,
coordinator-owned acceptance suite. The same run is inspectable through the CLI
and MCP, and its request/result project into both local state stores with
independent hash verification.

Start with [the Oasis demo](/Users/alectaggart/Oasis/START_HERE.md).
The previous [vNext analysis](TENET_VNEXT_ANALYSIS_2026-09-23.md) remains the wider
architecture plan. This cycle establishes a working local slice of that plan.

## What Oasis gained

The research led to a persistent district with consequential NPC relationships.
The prototype implements a reusable Python/SQLite runtime, JSON interface,
interactive terminal demo, Harbor and Orchard content, versioned behavior,
per-player relationship state, ordered command history and verified replay.

In the exercised demo, rescuing Mara raises Alice's trust and changes price from
10 to 8. Borrowing 7 adds a surcharge and removes the credit offer. Repayment
restores the offer. Bob has separate facts. Activating behavior v2 changes
Alice's price to 6 without rewriting her relationship history; it survives a
fresh process. The runtime documents the distinction between an offer signal
and an enforced commerce transaction.

The existing research, economics model and workbook remained unchanged. The
frozen [specification](/Users/alectaggart/Oasis/specs/001-persistent-district.md)
was committed before implementation at
`7611b0b4f29499569f9ec111ca011d3beb4506f7`.

## What TENET and ORC gained

| Boundary | Implemented behavior |
| --- | --- |
| TENET step-recipe admission | Saves a request before dispatch and exposes a unique run handle. Reusing it refuses a second launch. |
| TENET observation | Saves the terminal executor result, returns failure exit codes, and preserves timeout/signal uncertainty. |
| MCP | Workspace-bound `recipe_status` reads the same saved record without starting an executor or Hub. |
| Shared state | `recipe sync` projects source receipts into primary/shadow stores and verifies each by reading its content hash. Reconciliation is repeatable and cloud is disabled for this path. |
| ORC acceptance | Coordinator check receipts retain command, timing, exit status, output files and hashes. Revalidation preserves older check directories. |
| Claude adapter | An actual failed review revealed that variadic `--allowedTools` consumed the prompt. An explicit option terminator fixes the launch boundary. |
| MCP permission metadata | A subsequent plan-mode denial exposed missing read-only annotations. The corrected tool metadata allowed the same native read-only harness to inspect the run, with permissions unchanged. |
| Worker handoffs | Multiline summaries and test lists now survive normalization; fenced examples do not override handoff labels. |

The original project-local receipts remain authoritative. State projection is
repairable indexing, not a competing execution owner. A recipe's successful
exit and a task's accepted behavior remain separate facts. The existing
platform/state composition is used locally; this cycle does not establish a
deployed platform/cloud integration or a finished dashboard.

See [recipe evidence API documentation](/Users/alectaggart/CascadeProjects/tenet-cli/docs/recipe-run-evidence.md).

## Recorded runs and evidence

The main run is `oasis-district-001`, with native Codex run
`20260923-191710-be72cb91`. It took 914 seconds including orchestration and checks.
The installed Codex configuration selected `gpt-6-astra`; ORC's native result
left the resolved model blank. Fusion recorded zero spend, which does not
establish the actual provider cost. These gaps are recorded as unknown in the
learning candidate.

Evidence lives at
[.fusion/evaluations/oasis-district-001](.fusion/evaluations/oasis-district-001).

| Check | Observed result | Evidence |
| --- | --- | --- |
| Frozen external behavior contract | 15 tests passed after implementation; all failed on the initial empty implementation | [Initial acceptance](.fusion/evaluations/oasis-district-001/acceptance-initial/manifest.json) |
| Implementation suite | 22 tests passed in the native worker | [Worker handoff](.fusion/evaluations/oasis-district-001/acceptance-initial/workflow-manifest.json) |
| Independent state/replay/crash audit | 37 oracle-checked commands, 3 replay generations, 111 historical retries, forced pre-commit exits for all four mutations | [Reproducible audit](.fusion/evaluations/oasis-district-001/adversarial-runtime/README.md) |
| Negative controls | Always-success stub and constant-digest mutant both rejected | [Stub](.fusion/evaluations/oasis-district-001/false-success-control.json), [mutant](.fusion/evaluations/oasis-district-001/constant-digest-control.json) |
| Duplicate admission | Existing live handle refused before another dispatch | [Output](.fusion/evaluations/oasis-district-001/duplicate-launch.log) |
| Interrupted coordinator | Killed real CLI fixture remained unknown; duplicate handle refused | [Interruption evidence](.fusion/evaluations/oasis-district-001/interruption.json) |
| Accepted workflow resume | Acceptance reran; original worker ledger and worker-directory list unchanged | [Resume receipt](.fusion/evaluations/oasis-district-001/resume-result.json) |
| CLI/MCP observation | Real stdio MCP returned the persisted completed record; invalid traversal ID rejected | [MCP result](.fusion/evaluations/oasis-district-001/mcp-final.json) |
| Shared-state reconciliation | Request/result verified in both local stores; repeat preserved hashes; `state inspect` read both | [Projection](.fusion/evaluations/oasis-district-001/state-projection-final.json) |
| Native second-harness inspection | Claude's actual MCP request received the completed Codex run; response request/result equal the authoritative files | [Transport trace](.fusion/evaluations/oasis-district-001/claude-mcp-calls.jsonl), [coordinator verifier](.fusion/evaluations/oasis-district-001/verify-native-mcp.py) |

Initial and revalidated acceptance snapshots retain separate evaluator copies,
candidate hashes and oracle outputs, so later checks cannot overwrite their
evidence. The interruption fixture validates TENET plumbing, not takeover of a
killed model session. The crash audit establishes process-crash behavior, not
power-loss durability.

The fresh Claude source review found no blocker against the frozen Oasis
contract. Its first inference run still failed the workflow because the MCP
call was denied; ORC correctly rejected the worker's prose success claim.
The [original review and denial](.fusion/evaluations/oasis-district-001/review-permission-denied/answer.md)
remain preserved. A separate CLI resume invocation combined mutually exclusive
flags and was rejected before dispatch; its failed TENET record also remains.
Corrected configuration uses `--spec` alone, with the attempt limit in that
specification. These are recorded repairs, not a claim that every attempt worked.

The repaired review completed successfully on its third native attempt:
[accepted review snapshot](.fusion/evaluations/oasis-district-001/review-accepted/manifest.json).
Its coordinator gate checked the actual MCP exchange against both source
receipt files. The native Claude review workflow reported $0.9129282 in total
provider cost across its inference attempts. That is separate from the unknown
Codex implementation cost. This demonstrates fresh-harness reconstruction and
review, not transfer of a private provider session.

The combined TENET integration gate passed 23 tests across the five changed
receipt, interruption, projection, CLI and MCP suites. TypeScript compilation
passed and updated the locally linked `tenet` binary. ORC's launcher/parser and
existing compatibility suites passed 94 tests; the separate acceptance-receipt
suite passed 5 tests. The final multiline-handoff repair then passed 98 tests
across its new regressions and existing workflow, shipped-prompt and Grok
coverage; these gates overlap and should not be summed as unique tests.

Oasis is committed locally at `7653154` with a clean working tree. The TENET and
ORC integration changes remain reviewable working-tree changes in their existing
checkouts; unrelated telemetry/cache changes were not staged or committed.

## What this cycle says to build next

1. **One run view.** Show run handle, workspace, actual executor, acceptance,
   artifact links, uncertain liveness and state projection independently. The
   current recipe exec path buffers progress; a long task needs visible events
   while it runs. CLI/MCP now provide a concrete record for that view.
2. **Admission parity.** Apply the same receipt contract to other recipe entry
   points, including MCP execution and Pi. This pilot records CLI step recipes;
   advertising inspection everywhere does not make every executor equivalent.
3. **A harder Oasis increment.** Add a small district client and a persistent
   quest involving two players. Acceptance must cross reconnect, replay and
   behavior update through the public interface. Keep runtime state ownership
   explicit instead of duplicating it in UI state.
4. **Evidence before learned policy.** Preserve real failures and repairs,
   resolved model identity, meaningful cost and task-family splits. Collect fresh
   held-out tasks before qualifying Laya routing or training. Laya ran in shadow
   mode here; independent structural/behavior checks controlled acceptance.

[Engineering labels](.fusion/evaluations/oasis-district-001/learning-candidate.json)
are retained with provenance and unknowns. They are not ingested into training
or used to promote routing policy. NPC behavior and robotics learning remain
separate datasets and evaluation problems.

TENET's journal writer also recorded the build and verified it by reading the
journal back: [journal entry](.fusion/evaluations/oasis-district-001/journal-entry.json).
