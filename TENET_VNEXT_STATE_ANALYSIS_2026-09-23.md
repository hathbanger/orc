# TENET next version: state and substrate boundary

Reviewed September 23, 2026. Current CLI revision: `b0ec9c4b3e26fcc16c6fec697a31c41994f2e172`. Cloud-peer source snapshot: `dda2b89ff19d56a666356078e3c88f96e166468c`, archived in [platform evidence](/Users/alectaggart/orc/.fusion/evaluations/orc-tenet-review-2026-09-23/platform/README.md). This is a source review and isolated filesystem experiment, not a claim about current production deployment. No production code/configuration was changed and no cloud/model calls were made.

**Concurrent update, September 23 — CLI `4882046447c13661e17c78c8221a5124416daea4`:** reviewed only the diff from `b0ec9c4b` for `agent-outcome.ts`, `map-event-bus.ts` and `rubric-runner.ts`. Three previously identified append paths now use `createShadowDualStore(...).appendSync(...)`: classified outcomes enter `build-journal`; MAP events with the standard `/.tenet/map-events.jsonl` path enter `events` after redaction; rubric tuples with the recognized `.tenet/training-buffer.jsonl` path enter `training-buffer`. Outcome and rubric writers retain direct-write fallbacks; MAP custom paths retain direct writes. This repairs those standard-path producer bypasses. It does not route the separate `BuildJournal` writer, Context Hub journals, service events, Peter decisions or RL surface writer, and the MAP startup rewrite remains outside the append seam. The three-file diff does not repair the identity, coherence, migration, queue, acknowledgment, replication or schema contracts below. No new runtime test was performed for this update. The body and archived seven-probe/205-test evidence remain a historical `b0ec9c4b` snapshot; do not read those three historical bypass rows as the latest state. Source links follow the working checkout and may therefore show the newer lines.

**Recommendation:** preserve the useful object store, workspace registry, legacy adapters and outcome vocabulary. Make one real Oasis run durable, restartable and inspectable through the product before extracting packages or migrating every state type. The current substrate is useful code, but several independent persistence paths still have different authority, acknowledgment and identity semantics. Packaging them together would preserve that split.

## What is authoritative today

| Producer/surface | Actual write and read path | What reaches the new substrate |
|---|---|---|
| `TrainingBuffer.append` | Canonical `.tenet/training-buffer.jsonl`; reads still parse that file directly | Shared factory writes a workspace-scoped cache plus audit entry; optional cloud fan-out. See [writer](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/training-buffer.ts:241), [read](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/training-buffer.ts:287). |
| State diagnostics | Same factory as TrainingBuffer; list reads canonical JSONL, inspect tries shadow objects | The earlier wrong-directory diagnostic bug is fixed. [Factory](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/factory.ts:76), [CLI](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/state.ts:45). |
| Build journals and new classified outcomes | Direct append to `.tenet/build-journal.jsonl` | Bypasses the factory. [BuildJournal](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/build-journal.ts:38), [outcome writer](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/agent-outcome.ts:297). |
| Context Hub journal tool | Direct append to `.tenet/journal/<session>.jsonl` | Bypasses the factory. [Writer](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/context-hub.ts:1391). |
| Rubric evaluation tuples | Direct append to training-buffer JSONL | Bypasses TrainingBuffer/factory. [Writer](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/rubric-runner.ts:248). |
| Service events, Peter decisions, RL surface tuples | Direct JSONL writes, with both project and home scope | [Service writer](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/service-gtm.ts:673), [Peter](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/peter.ts:1109), [RL surface](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/rl-surface.ts:471). |
| MAP event bus | In-memory buffer, optional best-effort JSONL append, subscriber fan-out | Bypasses the factory. Persisted file is truncated/rebuilt at startup; it is a bounded event cache, not an immutable ledger. [Emit](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/map-event-bus.ts:109), [rewrite](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/map-event-bus.ts:259). |
| Memories | SQLite `.tenet/memory.db` remains canonical | Opt-in `TENET_MEMORY_STATE_LAYER_MIRROR=1` writes objects directly to LocalCache, outside audit/cloud stack. [Mirror](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/memory-state-mirror.ts:40). |
| Embedding cache | Opt-in cache objects plus tagged entries in the `memory` stream | Direct LocalCache; derived index, not authoritative memory. [Lookup/write](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/embedding-cache.ts:123). |
| Portfolio population | Reads legacy JSONL/journal/SQLite and puts objects into a shared pool | Uses kinds `training_tuple`, `journal_entry`, `service_event`, `memory`; does not create the equivalent stream memberships. [Populate](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/workspace/portfolio-populate.ts:66). |
| Workspace migration | Copies five legacy stream types to LocalCache | Excludes session journals, memory and home-scoped RL surface buffer; migration is not event-idempotent. [Migration](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/workspace/migrate.ts:32). |
| Refusal tracking | Direct `.tenet/refusals.jsonl`; latest row per ID resolves review | Useful append/supersede model, but not a StateStore stream and not the manifest refusal schema. [Writer/reader](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/refusal.ts:22). |

`getStateStore()` still constructs GitBackedStateStore, but a source search excluding tests found **no production callers**. It is a dormant competing entry point, not an observed active bypass. [Definition](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/index.ts:190).

The factory now centralizes the training/diagnostic composition. It does **not** include AsyncWriteQueue or PolicyFilteredStateStore. Its error fallback silently selects an unscoped `~/.tenet/state-layer` store; a degraded backend therefore changes identity/storage scope. Expose degradation and preserve explicit workspace scope instead. [Fallback](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/factory.ts:103).

## New experiments against current code

Actual imported TypeScript modules; all writes confined to fresh temporary fixtures. [Probe script](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-state-2026-09-23/probe.mts), [JSON receipt](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-state-2026-09-23/receipt.json). The script creates a fresh data subdirectory on each execution. [Reproduction instructions and source hashes](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-state-2026-09-23/README.md) are archived beside it; future reruns import the current source checkout, which is not archived. Existing focused suite: **205 tests passed in 11 state/migration test files** (`vitest run src/lib/state-store/__tests__ src/lib/workspace/__tests__/link-migrate.test.ts`). These experiments expose contracts that the passing suite does not establish.

1. **Object presence is reported as stream coherence.** Put the expected object into the shadow without appending to its stream. `verifyCoherence` reports primary 1, shadow hits 1, no missing hashes; shadow `list` returns zero. The implementation checks `shadow.get(hash)` only. This cannot establish membership, order, multiplicity, cursor continuity or cloud replication. [Checker](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/shadow-dual.ts:149).

2. **Repeating migration duplicates events.** One canonical row; migrate twice. The second result reports `copied=0, alreadyPresent=1`, while destination stream length becomes two. Object deduplication works; event deduplication does not. Migration unconditionally calls append after its presence check. [Loop](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/workspace/migrate.ts:63).

3. **Queue acknowledgment does not mean read visibility.** Awaiting `AsyncWriteQueue.append` returns a hash and queueDepth 1; immediate list is empty, then contains one row after drain. This conflicts with substituting the queue for a store whose append contract suggests read-your-write. [Queue append/read](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/write-queue.ts:130).

4. **Two queue instances sharing a directory can lose an acknowledged item on restart.** Construct A and B before either writes; each accepts one item using local sequence 1. The persisted file is overwritten and a fresh instance recovers only B. Existing code has no production callers, so this is a blocker to activating it, not an observed current production loss. [Sequence](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/write-queue.ts:104), [persist](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/write-queue.ts:262).

5. **Future schema versions are rejected by parse but silently downgraded by migrate.** A v2 refusal-like record is rejected by `parse`; `migrate` stamps version 1 and accepts it. Unknown kinds return an error with no retained opaque value. Unknown extra fields on a known valid shape are preserved. [Validation](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/manifest.ts:638), [migration](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/manifest.ts:1034).

6. **Changing portfolio object roots hides old stream content.** Append one event to a standalone workspace; open the same workspace with a portfolio root. The unchanged stream lists zero usable entries and the old hash returns null, because objects remain in the old root. Enrollment/reassignment needs copy-and-verify before switching references. [Root selection](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/local-cache.ts:78).

7. **Repeating put does not repair a corrupt existing blob.** Put an object, replace its file with empty content, put the same object again. The second put acknowledges the original hash, while get remains null. Hash verification on read is good; write acknowledgment currently does not prove the existing object is readable. [Put](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/local-state-layer.ts:109), [get](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/local-state-layer.ts:141).

## Acknowledgments and durability: separate the guarantees

| Current operation | Observed guarantee | Not established by the acknowledgment |
|---|---|---|
| GitBacked append | `appendFileSync` returned for the canonical JSONL | Explicit fsync/power-loss durability; semantic acceptance; replication |
| LocalStateLayer append | Object write and stream append returned | Atomicity across the two files; fsync; automatic corrupt-object repair |
| ShadowDual append | Primary succeeded; shadow failure can be swallowed | Shadow success, audit completeness, cloud durability |
| Audited append | Base append succeeded; audit is best effort | Required audit receipt persisted |
| AsyncWriteQueue append | Local queue file write returned | Underlying write, read visibility, multi-writer safety, replication |
| CloudPeer append/flush | Base append succeeded; flush waits for attempts to settle | Remote acceptance, retry, eventual delivery or pull convergence |
| MAP emit | In-memory event exists and fan-out attempted | Disk persistence or replayable durable history |

No fsync protocol is visible in these local implementations. That does not mean every process restart loses data; it means power-loss durability has not been established. `writeFileSync` is synchronous with respect to JavaScript execution, not a blanket stable-storage guarantee.

CloudPeer handles writes as nonblocking HTTP fan-out, reads only the local base, and has no persistent retry/outbox or pull path. A 5xx warns and drops that attempt; 401/404 disables later pushes until reenabled; flush resolves after failures too. Other 4xx responses are not surfaced as rejected persistence. [Client](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/cloud-peer.ts:104), [HTTP handling](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/cloud-peer.ts:205).

The archived cloud-peer server accepts stream and object writes in different tables. Appending a stream record does not materialize a corresponding content-addressed object. The shared append→hash→inspect story therefore requires a defined backend contract. The earlier archived probes also demonstrate missing workspace membership checks, unverified client hashes and duplicate retry appends. Those are source-level fixture findings; this review did not query production. [Server state](/Users/alectaggart/orc/.fusion/evaluations/orc-tenet-review-2026-09-23/platform/tenet-cloud-peer/src/state.ts:58), [prior receipts](/Users/alectaggart/orc/.fusion/evaluations/orc-tenet-review-2026-09-23/platform/cloud-peer-probe.json).

For vNext, return or expose a receipt with separate stages: **queued**, **locally committed**, **replicated**, **projected**. Define exactly what locally committed survives. Projection checkpoints must name the event/sequence they have consumed. A UI acknowledgment must use the achieved stage; a replication failure must remain pending/failed with its reason rather than disappearing behind a successful local append.

## Identity, event identity and cursors

**Workspace identity needs one resolver.** LocalCache considers explicit project IDs, a home path registry, parent portfolio registration, raw Git origin URL, then absolute path hash. Separately, `tenet ws mount` writes an active workspace marker and `TENET_WORKSPACE_ID`; the cache resolver does not consult either. `getActiveWorkspace` is used by workspace commands, not general state producers. Two users can therefore believe they selected the same workspace while writing elsewhere. [Cache resolver](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/local-cache.ts:131), [active resolver](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/workspace/registry.ts:163).

Raw remote URL hashing also changes identity across SSH/HTTPS spelling, organization rename or remote change. Bind a stable workspace ID explicitly; treat repository locations, slugs and remotes as aliases. Keep portfolio membership and object-sharing permission separate from identity. Existing shared pools expose all readable objects in the pool to local portfolio search, which is a convenience boundary, not per-workspace authorization. [Search](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/workspace/portfolio-search.ts:40).

**Content identity is not event identity.** Current append hashes `{kind: stream-entry:<stream>, data}`. Identical content gives the same hash but can legitimately occur twice. Conversely, a retry should represent the same event only if its event ID/idempotency key is reused. Give a run, an attempt, an observation, an evaluation and a human correction separate IDs, linked through their run/attempt/evidence references. Content hashes identify immutable payloads. Migration must preserve distinct historical occurrences and prevent import replay; hashing payload alone cannot do both.

TrainingBuffer's `tb_...` identifier derives from state/action, and cross-service aggregation deduplicates on that ID. Repeated attempts with matching state/action can therefore be collapsed in aggregate views; it must not become the universal execution-event identity. [Construction](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/training-buffer.ts:269), [aggregate dedup](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/training-buffer.ts:325).

**Current cursors are backend-local offsets, not durable cross-device checkpoints.** JSONL and cache logs use byte offsets; cloud uses sequence numbers. New watch consumers start at the beginning; there is no durable subscriber checkpoint in the StateStore interface. Byte positions are safe only while that exact stream remains append-only and the consumer stores the last entry cursor. The MAP startup rewrite breaks that premise. Session journals also differ: GitBacked resolves one session file while LocalStateLayer uses one aggregate journal stream. [Git resolver](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/git-backed.ts:240), [local resolver](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/local-state-layer.ts:328).

Concurrent appends calculate informational offsets before writing, so offsets cannot be unique event IDs. For vNext, make cursors scoped to workspace/stream/generation, persist the last consumed event position with its projection, and define at-least-once delivery plus event-ID deduplication. Do not compare local byte offsets to cloud sequence numbers. A real PostgreSQL test must also establish that a sequence checkpoint cannot skip a lower sequence that commits later; allocation order alone does not establish commit order.

## Reuse versus dormant components

Keep and tighten:

- Canonical JSON hashing, typed immutable object storage, read-time hash verification and the legacy JSONL adapter. Specify supported JSON values and hash format/version once across Python/TypeScript/cloud; current implementation describes a subset of canonical JSON, not a fully portable arbitrary-object encoder. [Hash](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/hash.ts:19).
- Workspace/portfolio registry and migration/population commands, after making identity binding and membership relocation safe.
- The new AgentOutcome vocabulary, recorded legacy-versus-classified divergence, and refusal review/correction concepts. These are useful source observations, not sufficient acceptance evidence on their own.
- File fingerprints and independent executable checks as evidence inputs. AgentOutcome currently can classify `completed` from changed files or the presence of any eval score; preserve the distinction between process completion, accepted task and merged result. [Classifier](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/agent-outcome.ts:238).
- Slot-pool dispatch selection with in-flight file conflict checks. It is a pure scheduling decision function, not a persistent command queue with claim/lease/recovery. [Selection](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/dispatch-queue.ts:128).

Do not advertise as connected until exercised:

- AsyncWriteQueue has tests but no production construction found; it needs the failure semantics above before insertion into the canonical path.
- Manifest types/validators have no production imports found outside tests. They are a design/library asset, not the actual schema enforced on live writes. The live refusal representation and manifest representation already differ.
- `buildPersonaPipeline` has no production caller found. PolicyFilteredStateStore reads bypass policy, defaults to allow-all, and quarantine without a target can fall through to ordinary persistence. It is not a general workspace authorization layer. [Pipeline](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/workspace/persona.ts:102), [policy behavior](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/state-store/policy-filter.ts:99).
- Memory mirroring and embedding caching are explicitly opt-in and have their own module singletons; they do not inherit the factory stack automatically.
- Cloud peer fan-out is an opt-in replication attempt, not multi-device convergence.

For schema evolution, retain closed enums for actions an executor understands, but do not let an old consumer discard an event it cannot interpret. Separate an extensible storage envelope from typed projections: preserve unknown payloads, type name and version verbatim, expose unsupported types as opaque records, and refuse to execute unknown commands. Known-event projectors can skip unsupported types while advancing a replayable checkpoint and reporting them. Never restamp an unknown future version as an older schema.

## Smallest safe migration and cleanup sequence

1. **Freeze one run contract and one workspace binding.** Choose the Oasis slice; pin the runner revision. Define task/run/attempt IDs, accepted-command ID, event IDs, artifact hashes, evaluator version and result distinctions. Persist the chosen workspace ID. Scope success to that run, not a claim that every TENET feature is migrated.

2. **Connect the real producers used by that run.** Send dispatch intent, process start/exit, artifacts, checks, human interventions and final acceptance through one receipt writer. Keep existing JSONL shapes as compatibility outputs while old readers still need them. Route diagnostics through the same explicit workspace and backend; report degradation. Do not make every observation depend on cloud availability.

3. **Make local commit plus pending replication coherent.** Use one controlled writer for the pilot and an explicit transaction boundary for event append, idempotency and outbox insertion. Existing SQLite infrastructure is a reasonable implementation candidate for this small metadata/outbox boundary, while retaining content-addressed blobs; validate it under the pilot's actual concurrent processes before selecting it. This does not require a new generic state framework or moving Oasis's simulation state into TENET.

4. **Repair and prove import/replay before backfilling real history.** Copy originals; inventory source files, row counts, corruption and content hashes. Import with stable source-occurrence IDs and a resumable ledger. Include all session journals needed for the pilot. Verify object integrity *and* stream membership/order/multiplicity. Run import twice and demand unchanged logical counts. Mark imported legacy data with evidence quality instead of inventing absent run/acceptance facts.

5. **Add cloud replay only after local recovery works.** Persistent outbox, bounded retries, server idempotency on workspace+event ID, validated hashes, workspace authorization and a pull/checkpoint contract. Make repeated delivery harmless. Feed platform projections from accepted event receipts and expose projection freshness. Keep snapshot sync as an explicitly dated compatibility projection until replaced.

6. **Cut over one stream/consumer at a time.** Compare old and new projections at the same checkpoint. Flip a named canonical mode; keep a rollback pointer and legacy export. Distinguish old historical gaps from new producer bypasses. Remove dual-write only after crash/replay and recovery pass. Never flip a global canonical switch based on the current coherence percentage.

7. **Clean up after reachability is proven.** Remove unused alternate factories/imports, stale phase-status documentation, duplicate schema shapes and misleading backend names. Make the command help truthful about supported stages. Add a narrow guard against new direct writes to the migrated authoritative streams; do not ban caches, logs or app-specific files. Extract a shared contract package only when CLI, ORC adapter, peer and platform exercise it in the same run.

The pilot's acceptance demonstration should include: dispatch from a supported product surface; one isolated executor claims the attempt; evidence survives killing and restarting the coordinating process; retry does not execute or count the accepted command twice; a fresh session resumes using stored receipts; a broken candidate remains rejected; UI/MCP/CLI agree on the same IDs and final status. Then repeat on a different Oasis task and an unrelated small project.

## Choosing the local commit implementation

**No backend choice is proven by the current probes.** Use the same small conformance test against two candidates and select the simpler implementation that passes. Do this before a package extraction or a large migration.

| Candidate | What can be retained | Required new correctness work |
|---|---|---|
| Existing filesystem store behind one per-workspace writer | Blob format, hash behavior, legacy compatibility, straightforward inspectability | A committed journal record must contain the event and pending replication intent together. Define atomic publication, flush/fsync semantics, incomplete-write recovery, deduplication, checkpoint publication and compaction. All authoritative producers must use the writer. |
| Transactional SQLite metadata/outbox with existing blobs retained | Existing SQLite dependency, blob format, legacy compatibility via projections | Transactionally append event, event-ID uniqueness, outbox and claim/checkpoint changes. Define blob-before-reference publication and missing-blob handling, commit/durability settings, process contention and migration. Test the actual filesystem/environment. |

SQLite metadata/outbox is the stronger **candidate** when real CLI, harness and daemon processes must write concurrently; the filesystem option can be a smaller pilot if a single writer is operationally enforceable and its recovery journal is small. Neither option justifies replacing all data stores. Keep search indexes, embeddings and platform views as rebuildable projections.

The shared spike must pass: duplicate append returns the same event receipt; same payload with different event IDs preserves two occurrences; a commit cannot leave a locally accepted event without pending replication; kill/restart at each publication boundary neither loses acknowledged events nor doubles accepted commands; concurrent writers cannot overwrite each other's queue state; unknown events survive; a checkpoint cannot advance beyond missing data; and restart reconstructs the same run view. Measure tail latency and contention using representative Oasis receipt volume, not simulation-tick throughput.

Execution ownership and delivery ordering are separate. Use one active local executor claim per attempt with an expiry/heartbeat and generation/fencing rule that rejects stale owners after takeover. A remote replication cursor only says how far a particular source stream was consumed; it is not a global clock, a cross-device total order, or proof that a worker owns execution. Multi-device concurrent editing/dispatch can be deferred until this smaller ownership contract is demonstrated.

## Remaining epistemic boundaries

These need targeted experiments before stronger claims:

- OS crash/power-loss durability and object/stream ordering: current fixtures test logical behavior, not fsync or sudden machine failure.
- Multi-process local writes and cloud commit ordering under real PostgreSQL transactions; current cloud receipts use an in-memory DB fixture.
- Workspace moves, worktrees, remote URL changes, project-ID overrides, two mounted projects in one daemon, portfolio join/leave and object ownership after relocation.
- Process termination after executor claim, after artifact write, after event commit, after cloud acceptance and before local outbox acknowledgment. Establish recovery at each boundary.
- Mixed-version consumers with unknown events, delayed corrections and projection rebuilds. Stored unknown data must round-trip without becoming executable.
- Whether the first run's acceptance checks detect plausible but incorrect code. State integrity preserves evidence; it does not make a weak evaluator correct.

The next architecture decision should be driven by these failures and one completed user journey. There is enough substrate here to reuse; the missing work is a small number of precise contracts and end-to-end ownership, not another layer of names around the existing disconnected paths.
