**TENET vNext: platform, IDE, and harness-facing product analysis — September 23, 2026**

This is a source-grounded companion to the [platform/state review](/Users/alectaggart/orc/TENET_PLATFORM_STATE_REVIEW_2026-09-23.md). It covers the user journey and the platform/renderer boundaries; the state and ORC/learning analyses cover their respective internals. No product source, configuration, deployment, or production data was changed.

**Conclusion:** Preserve the local dashboard, terminal workspace engine, context service, and useful cloud capabilities. Consolidate their identities, commands, receipts, and read models. A new UI or a repository merger would leave the hardest failures intact. The next release should prove that one person can start work in one harness, inspect its evidence from another surface, survive a restart, and resume the same task without manually reconstructing reality.

Reviewed snapshots: `tenet-cli b0ec9c4b3e26fcc16c6fec697a31c41994f2e172`; `jfl-platform 2713e166c7b62603a746d2be6bf117febb7f3afc`; sibling `jfl-ide 38ca09611a0d8be8b5b32f73b3620db1feb7daf1`. The previous review established that platform main `2d9b88aa` had an identical `src` tree. This pass did not requery deployment or upstream HEAD. Existing dirty files in CLI/IDE were left alone.

**Corrections to the current product premise**

1. The 23-page local dashboard and the cloud platform dashboard are different applications. The local dashboard calls relative Context Hub endpoints through [apiFetch](/Users/alectaggart/CascadeProjects/tenet-cli/dashboard/src/api/client.ts:21); its topology endpoint is `/api/v1/topology`. The platform uses Postgres-backed workspace snapshots. Therefore [PRD_TENET W5](/Users/alectaggart/CascadeProjects/tenet-cli/PRD_TENET.md) overgeneralizes when it describes the dashboard as a cloud-only Postgres read path. The local browser and terminal views already supply a cloud-independent starting point.

2. Graph rendering already exists. [SystemGraph](/Users/alectaggart/CascadeProjects/tenet-cli/dashboard/src/pages/SystemGraph.tsx:130) draws real SVG nodes/edges; [Topology](/Users/alectaggart/CascadeProjects/tenet-cli/dashboard/src/pages/Topology.tsx:1348) uses a custom canvas/WebGL implementation; the IDE topology surface invokes `tenet services deps`. Absence of a graph library does not prove absence of a graph. These views describe service/event topology, not an authoritative task dependency graph with verified attempt outcomes. The missing work is the task/run data contract and its projection, plus a usable rendering of that contract.

3. `tenet ide` does not import the sibling `jfl-ide` package. It imports [its own workspace engine](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/ide.ts:8), a diverged implementation under `src/lib/workspace/`, with extra sidebar, kanban, and physical-world surfaces. The sibling is useful provenance but is not the production import path. Treating all local renderer code as already deduplicated would miss a real cleanup target.

4. The platform has several durable memory paths. The reproduced volatile `/api/tenet/mcp` memory bug does not mean every platform memory is volatile. The separate paths are detailed below; they are not interchangeable yet.

5. "Four renderers" is a planning grouping, not an exhaustive capability inventory. CLI text, local browser, terminal workspace, and cloud browser are sensible presentation roles. MCP is also a product interface for harnesses, and ORC has its own execution/review view. All must agree on task/run identity and meaning; they need not share an identical screen layout.

**Current user-journey matrix**

| Surface | Context and task entry | Execution and evidence | Resume and learning | Authority/freshness today |
| --- | --- | --- | --- | --- |
| TENET CLI / local Context Hub | Context and memory commands; local board/GitHub issue paths; recipes; build dispatch | CLI build pipeline exists; Hub dispatch actually spawns `tenet build --dispatch`; journals/evals/session files exist | Runtime-specific session mechanisms; state diagnostics and training-buffer tools exist; no demonstrated universal cross-harness resume contract | Local files and several store compositions; local bearer auth; production backend wiring must match writers |
| TENET local MCP | Stdio server bridges harness tools into Hub; tools include `build_dispatch`, `recipe_run`, journals and context | `build_dispatch` description now honestly says background acceptance does not expose later eval rejection | A harness can retrieve context, but registration or a tool list does not establish durable task recovery | Project resolution + local Hub credentials; same server hardcodes some presence runtime labels as Claude |
| Local browser dashboard | Context/chat, board, services, journal, 23 pages, service topology | Real local endpoints; flows page reads execution history; some widgets infer or substitute states | Useful inspection surface, not yet one authoritative run detail and resume path across harnesses | Relative Hub API, token from dashboard URL/local storage; polling; no required cloud database |
| Shipped terminal IDE in CLI | tmux/cmux workspace engine, discovered local services/agents, shell and assistant panes | File reads/watchers for evals, sessions, training; polling for Hub events/flows | Session/round/training summaries; layout persistence does not equal agent-run recovery | Local-file floor is real; Hub flow response shape currently mismatches consumer; SSE disabled in this implementation |
| Sibling `jfl-ide` | Similar terminal surface engine, fewer surfaces | Similar file readers; separate Hub helper | Useful code to compare, not another backend to maintain independently by default | Separate helper requires explicit config, but its polling caller omits it; isolated probe confirmed no polling requests |
| Cloud platform browser | Accounts/memberships, workspaces, Work Brain, personal memory/import/search, integration UI | Build/eval dispatch records queued intent; snapshot histories and heuristic flow steps; live runs array is empty | Personal memory persists through separate DB APIs; universal executor recovery/cancel not established | Snapshot cache/Postgres, independent auth/identity domains, no demonstrated cloud-peer receipt projection |
| Cloud `/api/tenet/mcp` | Eight context/memory/journal/skill/recipe tool definitions | Custom HTTP tool caller works; standard lifecycle failed prior probes | Snapshot-backed `memory_add` is volatile; no run executor adapter | Not yet a standard MCP transport; workspace membership checks elsewhere do not cure missing protocol or persistence |
| ORC integration boundary | ORC owns a separate workflow/task interface | Intended reuse: harness execution, acceptance, attempts, review, recovery | Laya labels and candidate evaluations need their own receipt types | Needs an explicit TENET adapter; source of execution truth must be named per run |

Concrete source anchors: [Hub dispatch](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/context-hub.ts:1507), [current tool description](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/tool-schemas.ts:424), [shipped IDE imports](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/workspace/data-pipeline.ts:5), [cloud run API](/Users/alectaggart/CascadeProjects/jfl-platform/src/app/api/tenet/runs/route.ts:25).

**Which platform memory is which**

| Path | Stored representation | Acknowledgment and access behavior | Migration implication |
| --- | --- | --- | --- |
| `/api/tenet/mcp` `memory_add` | `WorkspaceData.memories` in global process Map | Reports success after `setWorkspaceData`; store explicitly excludes memories/links from DB persistence | Must route through a durable store; existing API intent is valuable, current implementation is not a durability contract |
| `/api/tenet/brain` POST/GET/export | `brain_memories` rows plus links | POST awaits SQL insert; reads primarily scoped to authenticated user; workspace membership checks exist; DB errors surface on write | Preserve useful personal-memory/import/export experience; define explicitly whether records are personal or shared workspace context |
| `/api/tenet/brain/import` | Per-note `brain_memories` + `brain_memory_links` | Awaits each insert, reports imported/total; failures can produce partial import | Reuse bounded records/import semantics, with explicit partial status and replay IDs |
| `/api/memory/sync` and `/api/memory/search` | `team_memories`, including optional vector embeddings | Sync awaits per-record upsert; search filters project; JWT + tier + `checkProjectAccess` | Existing durable implementation, but project UUID versus workspace slug mapping and conflict ownership need validation before adoption |
| `/api/journal/write` | Daily JSONL content inside `knowledge_files` | Awaits insert/update and returns failure on DB error; project access check; read/modify/write whole daily content | Preserve journal content and provenance; migrate to append records to avoid concurrent writers overwriting one another |
| `/api/memory/unified-search` | Fanout over brain, team, and workspace snapshots | Authenticated, but team/snapshot searches are not bounded to the principal's authorized workspaces | Do not make this the shared cross-harness retrieval endpoint before fixing authorization per source |

Sources: [brain durable write](/Users/alectaggart/CascadeProjects/jfl-platform/src/app/api/tenet/brain/route.ts:117), [brain import](/Users/alectaggart/CascadeProjects/jfl-platform/src/app/api/tenet/brain/import/route.ts:113), [team memory sync](/Users/alectaggart/CascadeProjects/jfl-platform/src/app/api/memory/sync/route.ts:139), [journal write](/Users/alectaggart/CascadeProjects/jfl-platform/src/app/api/journal/write/route.ts:87), [snapshot persistence](/Users/alectaggart/CascadeProjects/jfl-platform/src/lib/tenet/data-store.ts:95).

"Durable implementation" above means source awaits a database operation; this pass did not prove deployed migrations, production data, concurrency, or an actual end-to-end adoption flow. In particular, [teamMemories.projectId](/Users/alectaggart/CascadeProjects/jfl-platform/src/lib/db/schema.ts:847) is a UUID foreign key to projects, while [checkProjectAccess](/Users/alectaggart/CascadeProjects/jfl-platform/src/lib/team-access.ts:18) compares its argument to `workspace_members.workspace_slug`. Do not assume those identities coincide. Also the team-memory upsert conflicts on global `id`, not `(project_id, id)`; validate ownership before accepting a supplied ID that already exists. These are source-level concerns, not claims about exploited deployment data.

**Current defects and what has already improved**

| Finding | Status and consequence |
| --- | --- |
| CLI state diagnostics previously used the wrong backend | Prior review recorded concurrent fixes through the shared factory. Do not reopen the fixed diagnostic bug as if untouched; broader backend migration and bypass writers are separate work |
| CLI build-dispatch tool implied the whole task succeeded | Current tool text explicitly distinguishes background dispatch from final eval; good improvement. Hub still returns only `ok/message`, without a durable run handle or background failure linkage |
| Cloud snapshot memory, auth/event scope, cloud MCP lifecycle, dispatch intent vs execution | Platform source unchanged from prior reproduced findings; remain release blockers for their respective promises |
| Unified memory search crosses source authorization boundaries | **New isolated reproduction:** authenticated user A received foreign team and snapshot sentinels with workspace omitted and explicitly foreign. Team SQL has no principal/project restriction; snapshot SQL accepts optional caller-supplied workspace. Brain source is user-filtered. This is a P1 boundary defect |
| Local topology replaces a real graph under six nodes with demo | Still present at [Topology.tsx:1351](/Users/alectaggart/CascadeProjects/tenet-cli/dashboard/src/pages/Topology.tsx:1351); mock badge exists but normal product navigation should show the real small workspace |
| Local loop counts positive eval as merge | Still present at [Loop.tsx:68](/Users/alectaggart/CascadeProjects/tenet-cli/dashboard/src/pages/Loop.tsx:68); success should require a merge receipt, not reward sign |
| Cloud workflows derive training completion from agent status | Still present at [flows/page.tsx:49](/Users/alectaggart/CascadeProjects/jfl-platform/src/app/dashboard/[slug]/flows/page.tsx:49); train/mining/merge states need their own evidence |
| Shipped IDE consumes the wrong flow envelope | [DataPipeline:241](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/workspace/data-pipeline.ts:241) expects `{flows}`; [Hub:2070](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/context-hub.ts:2070) emits an array. Flow summaries silently remain absent. Source-confirmed seam |
| Shipped IDE says SSE endpoint unavailable | [DataPipeline:68](/Users/alectaggart/CascadeProjects/tenet-cli/src/lib/workspace/data-pipeline.ts:68) disables SSE, while [Hub:1697](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/context-hub.ts:1697) implements it. Reconnect/cursor correctness still needs a producer-consumer experiment before enabling |
| Sibling IDE Hub polling never sends requests | [pollHubData:188](/Users/alectaggart/CascadeProjects/jfl-ide/src/data-pipeline.ts:188) omits the config required by [hubFetch:31](/Users/alectaggart/CascadeProjects/jfl-ide/src/hub-client.ts:31). Source-loaded probe: zero requests and empty live data, positive control with config: one request. This exact bug is scoped to the sibling; CLI uses a different Hub helper |
| Harness configuration is broader than harness presentation | Runtime/MCP registry includes Codex, while `ide config primary` accepts only Pi/Claude/auto and default workspace registry inserts Claude; MCP presence labels include hardcoded `claude-code-mcp`. Avoid reporting Codex work as Claude or inferring support from a binary registry alone |
| Installed/healthy is not necessarily connected | [doctor MCP-client check](/Users/alectaggart/CascadeProjects/tenet-cli/src/commands/doctor.ts:854) checks stale entries and can return green if module unavailable; it does not establish an actual initialize/list/call cycle |

The cloud unified-search reproduction loaded the actual TypeScript handler in an isolated VM, with authenticated user A, fixture SQL results, no environment keys, no network and no real database. It returned HTTP 200 with `FOREIGN_TEAM_SENTINEL` and `FOREIGN_SYNC_SENTINEL` for both requests. [searchTeam](/Users/alectaggart/CascadeProjects/jfl-platform/src/app/api/memory/unified-search/route.ts:117), [searchSync](/Users/alectaggart/CascadeProjects/jfl-platform/src/app/api/memory/unified-search/route.ts:168), and [POST fanout](/Users/alectaggart/CascadeProjects/jfl-platform/src/app/api/memory/unified-search/route.ts:213) show the missing authorization predicates. This demonstrates handler/query scope, not deployment exposure.

**One coherent user experience, with different host interfaces**

The central object should be a task with a versioned acceptance contract, containing runs, attempts, check receipts and artifacts. Context is a versioned input to that task. A memory is an attributed assertion with visibility and provenance; a journal is a narrative about activity; neither automatically establishes success. A label is a separate review decision attached to original evidence, and a trained candidate is a separate artifact with its own held-out evaluation.

Every surface should answer these same questions:

- Which workspace and repository revision am I working on, under which principal?
- What task is active, who owns execution, and what is blocking it?
- Which harness/model and effective permissions were used?
- What changed, what checks actually ran, and where is their output?
- What is saved locally, what is acknowledged remotely, and how stale is this view?
- What will resume or cancel do, and what still requires my decision?
- Which observations are merely collected, which labels are approved, and which model candidate is actually serving?

Keep each host's strengths. CLI gives concise text/JSON and stable IDs. MCP gives small structured results, resource/artifact links, and explicit unsupported capabilities. Terminal IDE gives the real interactive harness, logs, files, and attention notifications. Local browser gives run inspection, dependency views, diff/check evidence and recovery controls. Cloud browser gives fleet/workspace access, remote command intent and receipt projections. ORC can remain the detailed execution/review view while TENET links to it. Do not build another scheduler inside each UI.

The default home view should be an attention queue and current task: blocked checks, unanswered reviews, failed persistence, stale/disconnected executors. Put service topology, training diagnostics, and advanced traces one level deeper. Never replace an empty real workspace with example data; offer a separately labeled demonstration mode. Show cost as measured/partial/unknown with coverage. Never render missing evidence as zero cost, success, merged code or trained policy.

Artifacts need stable IDs and a location descriptor. On the originating machine, expose open file, copy absolute path, and reveal containing directory; support terminal hyperlinks where the host does. For cloud/other-machine viewers, expose an authorized artifact link or downloadable copy and its hash, not a local path that cannot exist there. Keep original run and check links on every journal summary and training example.

**Adoption sequence: installed → authenticated → MCP initialized → workspace resolved → verified task**

These are proposed release criteria using existing entry points where available, not claims that a new unified onboarding command already exists.

| Stage | Existing foothold | Required visible proof before advancing |
| --- | --- | --- |
| Installed | CLI binary, runtime registry, `tenet doctor`, client registration catalog | Executable/version/architecture resolved; supported capability list; unavailable harness features explicitly unavailable |
| Authenticated | Provider login, local Hub bearer, platform user/membership paths | Provider auth distinct from platform membership and local daemon auth; effective principal and workspace scope displayed without exposing secrets |
| MCP initialized | `tenet mcp register <client>` for local server; cloud endpoint after protocol repair | Real client initialization, tool discovery and one harmless call succeed; exact configured server version and client scope recorded |
| Workspace resolved | Existing init/config, Hub port discovery and state factory | Same canonical workspace ID, repo root/revision and scope in CLI, harness, IDE and browser; explicit mapping from old slugs/UUIDs; no silent CWD substitution |
| Executor ready | Runtime selection; explicit ORC adapter or named TENET execution owner | Reachable executor, permitted roots/network/tool capabilities, selected model, local state write/read health |
| First verified task | One bounded Oasis task with independent acceptance checks | Immediate run ID; actual change/check receipts; rejection visible; accepted state matches all surfaces; durable artifact retrievable after restart |
| Optional cloud connection | Existing snapshot sync/cloud peer/platform routes | Local receipt versus remote acknowledgment differentiated; offline operation preserved; replay and workspace access checks pass |
| Resume/handoff | Existing sessions plus new common task/run continuation contract | Fresh session retrieves the same accepted context and prior failures; intentional interrupted attempt resumes without duplicate work |

Registration is not initialization, initialization is not workspace binding, spawning a process is not completion, and an improved metric is not accepted behavior. These distinctions should be reflected as ordinary product status text, not as an infrastructure checklist dumped into every user interaction.

Current CLI command registration is [mcp register](/Users/alectaggart/CascadeProjects/tenet-cli/src/index.ts:552) and [context-hub](/Users/alectaggart/CascadeProjects/tenet-cli/src/index.ts:698). Some local dashboard/helper text still suggests `tenet hub open/start`, while this reviewed command table has no `hub` alias. Generate action/help links from the actual command registry so product recovery instructions are executable. Do not silently rewrite user client configuration as a substitute for a scoped installer preview/result.

**Consolidate and migrate without discarding the useful parts**

| Keep | Consolidate | Retire only after proof |
| --- | --- | --- |
| Shipped CLI workspace engine, tmux/cmux adapters, local file-based floor | One maintained terminal engine ownership; explicit runtime selection and Hub binding; migrate useful sibling differences | Independent sibling engine release path if no actual downstream users remain; preserve repository history and documented replacement |
| Local Preact dashboard and cloud Next application | Shared versioned read contracts and status derivation, not necessarily one frontend framework | Heuristic completion counters, hidden demo substitution, stale command snippets |
| Durable brain/team knowledge features and existing import/export | Explicit personal/workspace/project visibility, canonical IDs, provenance and per-source authorization | Volatile MCP memory implementation and duplicate independent search business rules after compatibility cutover |
| Existing state-store code and manifests | Shared typed read/write contracts at actual producer/consumer boundaries; replayable read projections | Whole-array snapshots as authority after receipts can reconstruct views; keep temporary versioned compatibility projection |
| Real local MCP tool surface | Shared tool definitions, transport adapters, client-specific config and protocol conformance tests | Custom cloud pseudo-MCP caller as advertised standard MCP; retain HTTP compatibility under an honest separate API if needed |
| ORC execution and review capabilities | One owner per run and a TENET adapter exposing identity, status, artifacts and controls | Duplicate orchestration paths only after their distinct capabilities/users are mapped |

Migration should be additive first: assign explicit legacy-source IDs, ingest without fabricating missing evidence, compare old/new read results, and keep rollback. Mark historical records lacking run/check links as legacy observations. They can inform retrieval; they must not silently become verified acceptance or training labels. A rename or repository move must preserve workspace identity, and deletion must have semantics across local caches and cloud replicas.

Avoid a giant new application shell as the first milestone. Build one task detail projection, use it in CLI/MCP/local browser, connect the terminal view, then expose the same authorized projection in cloud. Each old widget can be replaced only when its replacement reproduces real records and honestly reports missing data.

**Release gates and epistemic experiments**

| Gate / uncertainty | Smallest decisive experiment | Pass condition |
| --- | --- | --- |
| Honest first run | Drive one actual generated task through each production producer/consumer, including real failed acceptance | One stable run ID, no success on missing implementation or failed checks, same blocker everywhere |
| Harness onboarding | Fresh disposable workspace, actual supported client process and server initialize/list/read/write cycle | Context lands in intended workspace with correct principal/runtime label; unsupported functions reported; no global/project scope drift |
| Durable memory | Save through each supported UI/MCP entry point, restart service, read from fresh process | Every acknowledged durable write survives; partial/pending operations are explicit |
| Cross-workspace retrieval | Two principals and two workspaces across brain/team/snapshot searches, SSE and cloud peer | Foreign records never appear even with supplied IDs/workspace names or omitted filters |
| Canonical identity | Existing project UUID, workspace slug, local state ID and renamed/moved checkout | All authorized references resolve intentionally; no accidental new tenant or split history |
| Local offline floor | Stop platform/cloud connectivity while Hub and workspace files remain | Context, task evidence, local run inspection and continuation work; cloud lag is visible |
| View freshness/reconnect | Start IDE before Hub, interrupt stream, rotate token, restart Hub, reconnect | Recover automatically or expose actionable failure; no silent permanent stale state; cursor/replay behavior measured |
| Flow producer/consumer | Use the actual Hub response with the shipped IDE/local browser consumers | Same flow definitions, approval state and execution outcomes rendered; no hand-authored alternate envelope |
| Cross-harness continuation | Stop one harness mid-task and open another with the same task handle | Continues from durable task state; previous failures and constraints remain; unsupported provider-session resume is distinguished from task-level continuation |
| Cancellation and worker ownership | Cancel during tool work, kill executor, retry command delivery | Single owner/lease, acknowledged cancel versus requested cancel distinct, side effects accounted for, no duplicate accepted attempt |
| Concurrent journal/memory writes | Two source handlers write simultaneously against disposable Postgres | No lost acknowledged journal entries; conflicting memory IDs cannot overwrite another project; empty/deleted state stays deleted |
| Training truth in UI | Agent completes without training; candidate trains without promotion; candidate is promoted | UI keeps collection, label approval, training, evaluation and serving version distinct |
| Artifact portability | Open receipt from original machine, another machine and cloud browser | Correct hash/content with authorization; local paths only offered where meaningful |
| Engine consolidation | Compare shipped workspace engine and sibling against the same surface fixtures and backend capabilities | Preserve CLI additions and proven behavior; no second hidden owner of state transitions |

The Oasis loop should pay for this work through visible user value: each task creates an accepted Oasis capability and exposes one concrete TENET friction point. Track manual rescue minutes and missing-evidence incidents in addition to task success/cost. Do not count a cleaner dashboard or additional journal volume as proof that difficult tasks became easier.

**Validation and limits**

This pass performed read-only source tracing, compared production import paths, and ran two isolated handler/helper probes without external calls: sibling IDE polling/config behavior and platform unified memory-search scope. Sibling IDE `tsc --noEmit --incremental false` passed; that did not catch its polling seam. Prior platform/ORC/CLI reproductions remain in the companion report. No new paid harness runs, browser sessions, tmux/cmux sessions, authenticated live APIs or deployment checks were performed. Shipped-IDE flow-envelope and other source-level findings require the producer-consumer release probes above before being described as a complete live product test.

The [new evidence archive and reproduction notes](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-platform-2026-09-23/README.md) contain [IDE probe source](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-platform-2026-09-23/ide-polling-probe.cjs), [IDE result](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-platform-2026-09-23/ide-polling-probe.json), [unified-search probe source](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-platform-2026-09-23/unified-search-probe.cjs), [search result](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-platform-2026-09-23/unified-search-probe.json), [typecheck log](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-platform-2026-09-23/ide-typecheck.log), and [source revisions/hashes](/Users/alectaggart/orc/.fusion/evaluations/tenet-vnext-platform-2026-09-23/source-manifest.json). The archive preserves the distinction between the sibling IDE polling defect and the shipped IDE's different producer/consumer contract.
