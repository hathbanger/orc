# Fusion research and build notes

Updated 2026-09-18. This is a curated working set of high-signal sources for multi-model coding agents, model routing, and Claude/Codex delegation. “Fusion” here means a harness that combines agents at run time; it does not mean merging model weights.

## What the linked Artificial Analysis post says

The linked post is the Artificial Analysis thread on Cognition’s Devin Fusion:

- [Artificial Analysis thread](https://x.com/artificialanlys/status/2098504936984293447?s=46&t=6-cVizCt6wEDkRXUxmV5dA)
- [Artificial Analysis mirror with the thread text](https://es.twstalker.com/ArtificialAnlys/status/2098504936984293447)
- [Cognition: Fusion in Devin Desktop and CLI](https://cognition.com/blog/local-fusion)
- [Cognition: Devin Fusion technical breakdown](https://cognition.com/blog/devin-fusion)

The useful architectural claims are:

1. A frontier lead owns planning, ambiguity, delegation, and final review.
2. A cheaper sidekick runs bounded implementation, exploration, tests, or other mechanical work.
3. The two agents keep separate persistent contexts and exchange briefs, results, and feedback instead of copying entire transcripts.
4. Routing can change during a session when the current work becomes harder or easier. Cognition describes doing this at compaction boundaries to avoid throwing away a useful prompt cache.
5. The lead can take control back when the sidekick is out of depth.

The benchmark results are vendor and benchmark claims. Artificial Analysis reports a Claude Fable 5.1 plus SWE-2 sidekick configuration around 62 on its Coding Agent Index, and an Astra plus SWE-2 configuration that is cheaper and faster while scoring lower. Cognition’s own updated table reports task-level savings across several coding benchmarks. These numbers motivate the architecture; they are not a guarantee for a local workflow.

The most important failure case in Cognition’s examples is a task where the judgment itself is the deliverable. Delegating implementation on a mechanically specified task worked well; delegating a subtle product decision caused a large quality drop. Our harness therefore makes the lead explicit and returns structured evidence for review.

## Existing tools and protocols

| Source | Pattern | Use in this build |
| --- | --- | --- |
| [hathbanger/orc](https://github.com/hathbanger/orc) | Runs Claude Code against OpenRouter models, tracks model fit, quality, context, and spend | Keep as the provider and model launcher. Fusion is a separate orchestration layer beside it. |
| [dwgx/claude-codex-subagent](https://github.com/dwgx/claude-codex-subagent) | Claude is the orchestrator; `codex exec` is a fresh-context worker; uses personas, JSON output, resume, and bounded dispatch | The closest existing Claude-to-Codex implementation. Fusion generalizes the adapter in both directions and adds a run ledger plus workspace lock. |
| [OpenAI Codex CLI](https://github.com/openai/codex) | `codex exec --json` emits `thread.started`, `item.completed`, `turn.completed`, `turn.failed`, and `error` events | Fusion parses those events and stores the thread id for a later sidekick turn. |
| [Anthropic Claude Code CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage) | Print mode, JSON/stream-JSON output, session resume, MCP configuration | Fusion uses print mode for bounded Claude workers and MCP for the lead surface. |
| [Model Context Protocol](https://github.com/modelcontextprotocol/servers) | Standard way to expose tools and data to different agent clients | Fusion exposes `fusion_delegate` and `fusion_status` over stdio MCP, so Claude or Codex can be the lead. |
| [Agent Client Protocol](https://github.com/agentclientprotocol/rust-sdk) | Standardizes communication between editors and coding agents; supports client, agent, proxy, and conductor roles | A later UI/editor integration path. MCP is enough for the first local harness. |
| [OpenAI Agents SDK patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns) | Handoffs transfer ownership; agents-as-tools keep a manager in control; parallelization and judge loops are explicit patterns | Fusion follows the manager-as-lead pattern for coding work. |
| [UltraCode](https://github.com/diepquynh/ultracode) | Staged explore/spec/plan/implement/review workflow with cross-harness artifacts and model routing | Fusion adopts the bounded stage pipeline and artifact handoffs, with ORC model selection and per-stage budget caps. |

## Research that informs the design

| Source | Finding | What it means for coding agents |
| --- | --- | --- |
| [Mixture-of-Agents](https://arxiv.org/abs/2406.04692) and [reference implementation](https://github.com/togethercomputer/MoA) | Layered agents can improve answer quality by giving later agents earlier outputs | Useful for proposal and review workflows. It is expensive and transcript-heavy for code editing, so Fusion uses narrow briefs and a single writer. |
| [RouteLLM](https://github.com/lm-sys/RouteLLM) | A learned router can trade quality for cost between strong and weak models | A future policy layer can choose the lead/sidekick pair from receipts; an initial prompt-only router is too brittle for changing task difficulty. |
| [RouterBench](https://github.com/withmartian/routerbench) | Routing needs a common evaluation set with quality and cost data | Record task outcome, latency, tokens, and spend in `.fusion` so the routing policy can be evaluated on real work. |
| [LLMRouterBench](https://github.com/ynulihao/LLMRouterBench) | Unified evaluation finds model complementarity, recall failures, diminishing returns, and weak gains from careless ensemble growth | Curate a small roster of models and measure task-level outcomes instead of adding every available model. |
| [RouteMoA](https://github.com/Jize-W/RouteMoA) | Dynamic routing can reduce the dense cost and latency of a full mixture-of-agents graph | Supports using routing at task boundaries and compaction boundaries rather than calling every model on every turn. |
| [OpenHands](https://github.com/All-Hands-AI/OpenHands) | Open coding-agent infrastructure and evaluation make agent/tool behavior observable | Use its benchmarks and task traces as a future external eval target. |
| [agent-watch](https://github.com/soul-sol/agent-watch) | Process exit, terminal JSONL events, and stall detection should be treated separately | Fusion stores worker stdout/stderr and classifies timeout, worker error, and successful completion independently. |

## High-signal posts and discussions

- [Artificial Analysis: Devin Fusion benchmark thread](https://x.com/artificialanlys/status/2098504936984293447?s=46&t=6-cVizCt6wEDkRXUxmV5dA)
- [Cognition: Fusion announcement and benchmark results](https://cognition.com/blog/local-fusion)
- [Lawrence W. Zen: Claude as lead, Codex as subagent](https://x.com/LawrenceW_Zen/status/2035949835124351009)
- [A practical Claude-to-Codex subagent series](https://github.com/dwgx/claude-codex-subagent)
- [Codex multi-agents versus Claude Code agent teams](https://x.com/akihiro_genai/status/2026137417179365828)
- [Git worktrees for multiple Claude/Codex sessions](https://x.com/chenchengpro/status/2032411474703053012)
- [ACP session binding across Claude Code, Codex, and OpenCode](https://x.com/ichiaimarketer/status/2038146648627716195)
- [Claude Code Codex plugin patterns: review, adversarial review, rescue](https://x.com/reach_vb/status/2039251986357338257)
- [Codex discussion: queue/worker orchestration and isolated worktrees](https://github.com/openai/codex/discussions/3898)

## Decisions in this prototype

Fusion starts with Claude as the lead because it is already the user-facing orchestrator in the local workflow. Codex is the default sidekick because `codex exec --json` gives us a stable machine-readable worker boundary and a resumable thread id. Both directions are supported: `fusion lead --agent codex` wires the same MCP server into Codex, allowing Codex to delegate to Claude.

The Ultra pipeline can force every stage through Claude or Codex with
`--harness claude|codex`. Codex stages use separate read-only and
workspace-write routes, while Claude stages can use ORC's live free/best model
selectors. This keeps the harness boundary symmetric even though ORC is a
Claude-compatible OpenRouter launcher.

The handoff contract is deliberately small: task, role, workspace, success criteria, constraints, status, summary, changed paths, tests, blockers, and artifacts. The lead does not receive the sidekick’s full transcript by default. Raw stdout and stderr stay on disk for inspection and cost accounting.

Writes are serialized per workspace with `.fusion/workspace-writer.lock`. Parallel read-only work is safe; parallel writes require separate Git worktrees and a later merge or patch application step. This follows the same constraint surfaced in the Codex orchestration discussion and the worktree research above.

The next useful increment is an outcome-based router trained from the ledger: classify a task after initial exploration, choose the sidekick model from observed cost and repair rate, and promote work back to the lead when tests fail, the diff grows beyond scope, or the lead’s acceptance checks disagree with the worker’s report.

The repository now also has a bounded UltraCode-style pipeline. It keeps the
useful stage boundaries from [UltraCode](https://github.com/diepquynh/ultracode)
while making the expensive fan-out explicit: stage count is capped, each
stage gets a fresh context, handoffs live in `.fusion/ultra/`, and ORC routes
can select the current free or strongest tool-capable model with a per-call
budget. The pipeline is opt-in because a multi-stage workflow can spend more
tokens than a direct lead/sidekick run.

Each worker call also emits a metadata-only `fusion.trace.v1` span. The ledger
is intentionally local and provider-neutral: it can aggregate token usage and
latency now, and later attach ORC price receipts or provider billing data
without changing the orchestration contract. This is the data needed to tune
stage count, route choice, retry policy, and repair rate from real repository
work instead of benchmark guesses.

## How to try it

```sh
./install.sh
fusion doctor
fusion lead
fusion lead --agent codex
fusion delegate --agent codex --role implementation \
  --success 'tests pass' \
  'Implement the bounded change and report the files and tests.'
```

Use `.fusion.json` in a repository to pin commands, models, permission modes, timeouts, and the default lead. Do not put API keys in that file.
