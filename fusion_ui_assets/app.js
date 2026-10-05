/* ORC control room: no hosted assets, no bundler, no separate frontend process. */
"use strict";
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
// Pure decision logic lives in logic.js so it can be tested without a browser.
const {
  esc,
  safeGithubURL,
  withoutDerived,
  active,
  canApproveLabels,
  commandPreview,
  matchesWorkflowFilter,
  tokenFromURL,
  storedCredential,
} = ORCLogic;
const pretty = (value) => JSON.stringify(value, null, 2);
const sigil = (name, extra = "") => ORCBrand.icon(name, extra);
const badge = (value) =>
  `<span class="status ${esc(value)}">${esc((value || "unknown").replaceAll("_", " "))}</span>`;
const date = (ms) =>
  ms
    ? new Date(ms).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "Unknown start";
const age = (ms) => ORCLogic.age(ms);
const number = (n) =>
  new Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(n || 0);
const state = {
  workspace: null,
  workspaces: [],
  view: "overview",
  id: null,
  node: null,
  tab: "report",
  overview: null,
  config: null,
  report: null,
  scout: null,
  truffleSelection: {},
  forest: {tab:"woodland", patch:"all", grade:"all", query:""},
  decisions: [],
  decision: null,
  labTab: "overview",
  labelDrafts: {},
  learning: null,
  trainingLoop: null,
  learningRound: null,
  garden: null,
  labelRuns: [],
  gardenFilter: "all",
  search: "",
  filter: "all",
  epoch: 0,
  modal: null,
  polling: false,
  signature: "",
  authRequired: false,
};
const credentialKey = "fusion-token";
function rememberCredential(value) {
  // Share only credentials accepted by this server; the key is origin-scoped.
  for (const storage of [sessionStorage, localStorage]) {
    try { if (storedCredential(storage) !== value) storage.setItem(credentialKey, value); } catch {}
  }
}
let token = new URLSearchParams(location.hash.slice(1)).get("token");
if (token) {
  history.replaceState(null, "", "#overview");
}
token ||= storedCredential(localStorage) || storedCredential(sessionStorage);

async function api(path, body, workspace = state.workspace, retryCredential = true) {
  const url = new URL("/api/" + path, location.origin);
  if (workspace) url.searchParams.set("w", workspace);
  const requestToken = token;
  const response = await fetch(url, {
    method: body ? "POST" : "GET",
    headers: {
      "X-Fusion-Token": requestToken,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify({ ...body, workspace }) : undefined,
  });
  const data = await response.json();
  if (response.status === 401) {
    state.authRequired = true;
    const shared = storedCredential(localStorage);
    // Reads can recover after another tab connects. Never replay a mutation.
    if (!body && retryCredential && shared && shared !== requestToken) {
      token = shared;
      return api(path, body, workspace, false);
    }
  }
  if (!response.ok)
    throw new Error(data.error || `Request failed (${response.status})`);
  state.authRequired = false;
  if (token === requestToken) rememberCredential(requestToken);
  return data;
}
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("visible");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("visible"), 3800);
}
function error(message) {
  $("#error-banner").textContent = message || "";
  $("#error-banner").hidden = !message;
  if (message && state.authRequired) {
    const reconnect = document.createElement("button");
    reconnect.className = "button small";
    reconnect.dataset.action = "reconnect";
    reconnect.textContent = "Reconnect";
    $("#error-banner").append(" ", reconnect);
  }
}
function openReconnect() {
  modal("Reconnect to your control room", "A server restart can expire this tab’s connection. Your saved work stays on disk.",
    `<form id="reconnect-form"><div class="field"><label for="reconnect-url">Current control-room URL</label><input id="reconnect-url" name="url" type="url" autocomplete="off" spellcheck="false" placeholder="http://127.0.0.1:8765/#token=…" required><small>Paste the full URL printed by orc fusion ui. Opening that URL in another tab in this browser also reconnects this one.</small></div><button type="submit" class="button primary">Connect</button></form>`, "reconnect");
}
async function reconnectWithURL(value) {
  const candidate = tokenFromURL(value, location.origin);
  if (!candidate)
    throw new Error("Use the full URL for this local control room, including #token=…");
  // Validate before replacing a working credential or notifying any other tab.
  const response = await fetch("/api/bootstrap", {headers: {"X-Fusion-Token": candidate}});
  if (!response.ok) throw new Error("That connection URL has expired. Use the latest URL printed by orc fusion ui.");
  token = candidate;
  state.authRequired = false;
  rememberCredential(candidate);
  if (new URLSearchParams(location.hash.slice(1)).has("token"))
    history.replaceState(null, "", routeHash());
  await connectWorkspace(await response.json());
}
function intro(eyebrow, title, description, action = "") {
  return `<div class="page-intro"><div><div class="eyebrow">${sigil(ORCBrand.chapters[state.view])}${eyebrow}</div><h1>${title}</h1><p>${description}</p></div>${action || `<div class="date">${new Date().toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })}</div>`}</div>`;
}
function empty(title, description, button = "") {
  return `<div class="empty"><div class="empty-symbol">${sigil(ORCBrand.chapters[state.view] || "compass")}</div><h2>${title}</h2><p>${description}</p>${button}</div>`;
}
function button(text, action, extra = "", type = "") {
  return `<button class="button ${type}" data-action="${action}" ${extra}>${text}</button>`;
}
function markdown(text) {
  return DOMPurify.sanitize(
    marked.parse(text || "", { gfm: true, breaks: false }),
    {
      ALLOWED_TAGS: [
        "p",
        "br",
        "hr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "strong",
        "em",
        "del",
        "blockquote",
        "pre",
        "code",
        "ul",
        "ol",
        "li",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "a",
        "input",
      ],
      ALLOWED_ATTR: [
        "href",
        "title",
        "class",
        "type",
        "checked",
        "disabled",
        "start",
        "align",
      ],
      ALLOW_DATA_ATTR: false,
    },
  );
}
function enhanceMarkdown(root = $("#content")) {
  $$(".markdown pre", root).forEach((pre) => {
    const b = document.createElement("button");
    b.className = "code-copy";
    b.textContent = "Copy";
    b.addEventListener("click", () =>
      copy($("code", pre)?.textContent || pre.textContent.replace(/Copy$/, "")),
    );
    pre.append(b);
  });
  $$(".markdown a", root).forEach((link) => {
    const href = link.getAttribute("href") || "";
    if (/^https?:\/\//i.test(href)) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    } else {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        let path = href.replace(/:\d+(?:-\d+)?$/, "");
        try {
          path = decodeURIComponent(path);
        } catch {}
        if (path && !path.startsWith("#")) openFile(path);
      });
    }
  });
}
async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied to clipboard");
  } catch {
    toast("Clipboard unavailable. Select and copy the text.");
  }
}
function scrollPanes(root) {
  return $$(".console,.activity-feed", root).map((el, index) => ({
    el,
    key: el.dataset.scrollKey || `${el.className}:${index}`,
  }));
}
function activityItems(el) {
  const items = $$(".activity-item", el);
  return el.hasAttribute("data-newest-first") ? items.reverse() : items;
}
const activityKey = (el) => el.dataset.activityKey || el.textContent;
function animateNewActivity(el, previous) {
  const items = activityItems(el);
  const keys = items.map(activityKey);
  // Match the retained tail, including repeated messages and capped log windows.
  let overlap = Math.min(previous.length, keys.length);
  while (
    overlap > 0 &&
    !previous.slice(-overlap).every((key, index) => key === keys[index])
  )
    overlap--;
  items.slice(overlap).forEach((item, index) =>
    item.animate(
      [
        { opacity: 0, transform: "translateY(4px)" },
        { opacity: 1, transform: "translateY(0)" },
      ],
      {
        duration: 220,
        delay: Math.min(index, 4) * 25,
        easing: "ease-out",
        fill: "backwards",
      },
    ),
  );
}
function replaceContent(root, html, context) {
  const previousLive = new Set(root.dataset.scrollContext === context
    ? $$('.label-live-item', root).map(el => el.dataset.liveKey) : []);
  const disclosures = new Map(root.dataset.scrollContext === context
    ? $$('details[data-disclosure-key]', root).map(el => [el.dataset.disclosureKey, el.open]) : []);
  const scrolls = new Map(
    root.dataset.scrollContext === context
      ? scrollPanes(root).map(({ el, key }) => [
          key,
          {
            top: el.scrollTop,
            left: el.scrollLeft,
            atBottom: el.scrollHeight - el.clientHeight - el.scrollTop <= 4,
            items: activityItems(el).map(activityKey),
          },
        ])
      : [],
  );
  root.innerHTML = html;
  root.dataset.scrollContext = context;
  $$('details[data-disclosure-key]', root).forEach(el => {
    if (disclosures.has(el.dataset.disclosureKey)) el.open = disclosures.get(el.dataset.disclosureKey);
  });
  enhanceMarkdown(root);
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!reducedMotion) $$('.label-live-item', root).forEach(el => {
    if (!previousLive.has(el.dataset.liveKey)) el.animate(
      [{opacity:0, transform:'translateY(5px)'}, {opacity:1, transform:'translateY(0)'}],
      {duration:280, easing:'cubic-bezier(.2,.7,.3,1)'});
  });
  scrollPanes(root).forEach(({ el, key }) => {
    const previous = scrolls.get(key);
    if (previous && !reducedMotion) animateNewActivity(el, previous.items);
    el.scrollTo({
      top: previous?.top || 0,
      left: previous?.left || 0,
      behavior: "instant",
    });
    // Scrolling up pauses following. Returning to the bottom resumes it.
    if (el.hasAttribute("data-follow-tail") && (!previous || previous.atBottom))
      el.scrollTo({
        top: el.scrollHeight,
        behavior: previous && !reducedMotion ? "smooth" : "instant",
      });
  });
}
function mount(html) {
  replaceContent(
    $("#content"),
    html,
    JSON.stringify([
      state.workspace,
      state.view,
      state.id,
      state.node,
      state.tab,
      state.view === "decisions" ? state.labTab + ":" + state.decision : null,
    ]),
  );
}
function runRows(runs) {
  return runs
    .map(
      (run) =>
        `<div class="run-row" role="button" tabindex="0" data-action="run" data-id="${esc(run.id)}"><div><div class="run-title">${esc(run.task)}</div><div class="run-meta"><span class="mono">${esc(run.id)}</span><span>${run.read_only ? "Investigation" : "Implementation"}</span><span>${esc(run.agents?.join(" + "))}</span><span>${date(run.started_at_ms)}</span></div></div><div class="run-side">${badge(run.status)}<div class="mini-progress" title="${run.completed}/${run.total} stages accepted">${Array.from({ length: Math.min(12, run.total) }, (_, i) => `<i class="${i < run.completed ? "done" : ""}"></i>`).join("")}</div></div></div>`,
    )
    .join("");
}
function jobRows(jobs) {
  return jobs
    .map(
      (job) =>
        `<div class="job-row" role="button" tabindex="0" data-action="job" data-id="${esc(job.id)}"><div><h3>${esc(job.action)} <span class="muted">·</span> ${esc(job.title).slice(0, 130)}</h3><p>${date(job.started_at_ms)} ${active(job.status) ? "· " + age(job.started_at_ms) + " elapsed" : ""}</p></div>${badge(job.status)}</div>`,
    )
    .join("");
}
function overview() {
  const data = state.overview,
    runs = data.workflows,
    usage = data.usage,
    workerGroups = usage.by_route.filter((g) => g.agent !== "gate");
  const running = runs.filter((r) => r.status === "running").length,
    finished = runs.filter((r) => r.status === "success").length;
  const tokens =
    (usage.total.input_tokens || 0) + (usage.total.output_tokens || 0);
  const configured = state.config,
    qualified = configured?.qualified_buckets || 0;
  mount(
    ORCBrand.hero() +
      `<div class="stats"><div class="stat"><div class="stat-label">Active workflows <span>↗</span></div><div class="stat-number">${running}<span class="unit">running</span></div><div class="stat-detail">${runs.length} saved workflows in this workspace</div></div><div class="stat"><div class="stat-label">Completed <span>✓</span></div><div class="stat-number">${finished}</div><div class="stat-detail">All required stages accepted</div></div><div class="stat"><div class="stat-label">Worker calls <span>⌘</span></div><div class="stat-number">${number(workerGroups.reduce((n, g) => n + g.calls - (g.cache_hit || 0), 0))}</div><div class="stat-detail">${number(tokens)} reported input + output tokens</div></div><div class="stat"><div class="stat-label">Reported spend <span>＄</span></div><div class="stat-number">${data.cost.reported_calls ? "$" + Number(usage.total.cost_usd || usage.total.cost || 0).toFixed(2) : "—"}</div><div class="stat-detail">${data.cost.reported_calls} / ${data.cost.calls} calls reported a cost</div></div></div>` +
      `<div class="grid-main"><div><div class="section-bar"><h2>Recent workflows <small>${runs.length}</small></h2><button class="subtle" data-view="workflows">View all →</button></div>${runs.length ? `<div class="panel">${runRows(runs.slice(0, 6))}</div>` : empty("Your next idea starts here.", "Launch a discovery run to map the code and surface useful improvements.", button("Start discovery", "template", 'data-template="audit"', "primary"))}
    ${data.jobs.length ? `<div class="section-bar"><h2>Launch activity</h2><small>Jobs keep running when you close this tab</small></div><div class="panel">${jobRows(data.jobs.slice(0, 5))}</div>` : ""}
    ${data.builds
      .filter((b) => b.status === "running")
      .map(
        (b) =>
          `<div class="info-box"><h3>Preparing a workflow · ${esc(b.phase)}</h3><p>${esc(b.message)} · ${age(b.started_at_ms)}</p></div>`,
      )
      .join("")}</div>
    <aside><div class="section-bar"><h2>Start with a direction</h2></div><div class="panel">${[
      ["truffle", "truffle", "Send in the Truffle pig", "Find tractable open issues. Queue the fixes worth doing."],
      [
        "audit",
        "compass",
        "Find the next improvement",
        "Evidence, ranked findings, and a concrete plan.",
      ],
      [
        "feature",
        "forge",
        "Build something useful",
        "Explore, plan, implement, independently review.",
      ],
      [
        "debug",
        "bug",
        "Hunt down a bug",
        "Reproduce the failure, fix it, prove it.",
      ],
    ]
      .map(
        ([key, icon, title, desc]) =>
          `<div class="template" role="button" tabindex="0" data-action="template" data-template="${key}"><div class="template-top"><span class="template-icon">${sigil(icon)}</span><span class="template-arrow">↗</span></div><h3>${title}</h3><p>${desc}</p></div>`,
      )
      .join(
        "",
      )}</div><div class="info-box"><h3 class="guild-section-emblem">${sigil("rune")} Laya is ${esc(configured?.mode || "unavailable")}</h3><p>${configured?.mode === "shadow" ? "Local decisions are recorded as advice. Your workflow rules remain in control." : configured?.mode === "active" ? `${qualified} qualified calibration buckets. Only qualified, permitted actions can apply.` : "Classification is disabled. Existing orchestration still works."}</p><div class="worker-strip">${(configured?.workers || []).map((w) => `<span class="worker-chip"><b>${w.available ? "●" : "○"}</b>${esc(w.agent)}</span>`).join("")}</div></div></aside></div>`,
  );
}
function workflows() {
  const runs = state.overview.workflows.filter((run) =>
    matchesWorkflowFilter(run, state.filter, state.search),
  );
  mount(
    intro(
      "PERSISTED WORKFLOWS",
      "The work, end to end.",
      "Terminal and browser runs share the same history. Open any run to inspect its plan, activity, and results.",
    ) +
      `<div class="filter-bar"><input id="run-search" placeholder="Search tasks, run IDs, or agents…" aria-label="Search workflows" value="${esc(state.search)}"><select id="run-filter" aria-label="Filter status">${["all", "running", "success", "failed", "blocked", "interrupted", "paused_quota", "paused_budget"].map((s) => `<option value="${s}" ${s === state.filter ? "selected" : ""}>${s === "all" ? "All statuses" : s.replaceAll("_", " ")}</option>`).join("")}</select><small>${runs.length} runs</small></div><div id="run-results">${runs.length ? `<div class="panel">${runRows(runs)}</div>` : empty("No matching workflows.", "Try another filter or start a new run.")}</div>`,
  );
}
function openHunt() {
  modal("Send in the Truffle pig", "Scour this workspace’s GitHub issues for fixes grounded in actual code.",
    `<form id="truffle-hunt-form"><div class="form-grid"><div class="field"><label for="hunt-count">Issues to find</label><input id="hunt-count" name="count" type="number" min="1" max="20" value="5" required><small>A target, not a promise. Weak candidates stay out.</small></div><div class="field"><label for="hunt-pool">Issues to inspect</label><input id="hunt-pool" name="scan_limit" type="number" min="1" max="200" value="40" required><small>Controls the scope of this hunt.</small></div><div class="field"><label for="hunt-worker">Scout worker</label><select id="hunt-worker" name="agent"><option value="auto">Auto · available worker</option>${["codex", "claude", "agy", "grok"].map(a => `<option>${a}</option>`).join("")}</select></div><div class="field"><label for="hunt-remote">Repository remote</label><input id="hunt-remote" name="remote" value="${esc(state.config?.publish?.remote || "origin")}" required></div></div><div class="field"><label for="hunt-search">GitHub search filter · optional</label><input id="hunt-search" name="search" maxlength="500" placeholder='label:bug sort:updated-desc'><small>Uses the repository attached to this remote. Requires an authenticated gh CLI.</small></div><label class="check-field"><input name="include_assigned" type="checkbox"> Include issues already assigned to someone</label><div class="launch-note">Investigation only. The scout checks source, tests and feasibility, and explains its skips. You choose which fixes enter Fusion. Uses your configured coding worker account.</div><div class="dialog-footer"><button class="button" type="button" data-action="truffle-open">Past hunts</button><button type="submit" class="button primary">Find the truffles →</button></div></form>`, "truffle-hunt");
}
function truffleSelected(hunt) {
  const key = state.workspace + ":" + hunt.id;
  return state.truffleSelection[key] ||= new Set(hunt.selected || (hunt.kind === "survey" ? [] : hunt.candidates.filter(c => !c.workflow_id && c.status !== "skipped").map(c => c.number)));
}
function truffleView() {
  if (!state.scout || state.scout.kind === "survey") return woodlandView(state.scout);
  const h = state.id ? state.scout : null;
  const hunts = state.overview.hunts || [];
  if (!h) {
    mount(intro("ISSUE SCOUT", "Truffle pig.", "A good nose for small fixes. Source evidence, a regression plan, and a clear path into Fusion.", button("New hunt", "truffle-hunt", "", "primary")) + ORCBrand.truffleLore() +
      (hunts.length ? `<div class="panel">${hunts.map(r => `<button class="truffle-history" data-action="truffle-open" data-id="${esc(r.id)}"><span><strong>${esc(r.repo || "Reading repository…")}</strong><small>${esc(r.found)} / ${esc(r.target)} candidates · ${esc(r.scanned ?? "—")} issues inspected · ${date(r.started_at_ms)}</small><span>${esc(r.message)}</span></span>${badge(r.status)}<span>→</span></button>`).join("")}</div>` : empty("Let it sniff out the next fix.", "Choose how many issues you want. The scout will return fewer if the evidence does not support the target.", button("Start a hunt", "truffle-hunt", "", "primary"))));
    return;
  }
  const selected = truffleSelected(h), busy = ["scouting", "running", "waiting"].includes(h.status);
  mount(`<button class="back" data-view="truffle">← All hunts</button>` + intro("TRUFFLE PIG · " + (h.repo || "GITHUB"), h.status === "complete" ? "Back from the woods." : "The shortlist.", h.message || "Investigating open issues…", button("New hunt", "truffle-hunt")) + ORCBrand.truffleLore(h.status) +
    `<div class="pill-row">${badge(h.status)}<span class="mono">${esc(h.id)}</span><small>${date(h.started_at_ms)}${h.head ? " · checkout " + esc(h.head.slice(0, 8)) + (h.dirty ? " with local changes" : "") : ""}</small></div><div class="truffle-stats"><div><strong>${h.candidates.length}<small> / ${h.target}</small></strong><span>Source-backed candidates</span></div><div><strong>${h.scanned ?? "—"}</strong><span>Open issues inspected</span></div><div><strong>${h.skipped.length}</strong><span>Skipped with a reason</span></div><div><strong>${h.candidates.filter(c => c.workflow_status === "success").length}</strong><span>Fixes accepted</span></div></div>
    <div class="truffle-toolbar"><p class="help-copy">Source quotes are checked against files. Feasibility is the scout’s assessment; implementation and independent review still have to prove the fix.</p>${button(h.selected?.length && h.status !== "complete" ? "Continue queue →" : "Queue selected fixes →", "truffle-queue", busy || !h.candidates.length ? "disabled" : "", "primary")}</div>
    <div class="truffle-candidates">${h.candidates.map((c, i) => `<article class="panel truffle-candidate"><div class="panel-body"><div class="truffle-candidate-heading"><label class="check-field"><input type="checkbox" data-truffle-number="${c.number}" ${selected.has(c.number) ? "checked" : ""} ${busy || c.status === "skipped" ? "disabled" : ""} aria-label="Select issue ${c.number}"><span class="eyebrow">PICK ${i + 1} · #${c.number}</span></label>${badge(c.workflow_status || c.status)}</div><h2><a href="${esc(safeGithubURL(c.url))}" target="_blank" rel="noopener noreferrer">${esc(c.title)} ↗</a></h2><p>${esc(c.reason)}</p><div class="pill-row"><span class="chip">${esc(c.effort)} effort</span><span class="chip">${esc(c.risk)} regression risk</span><span class="chip">${c.evidence.length} source citations</span></div><p class="help-copy">${esc(c.reproduction)}</p>${c.queue_note ? `<p class="launch-note">${esc(c.queue_note)}</p>` : ""}${c.workflow_id ? button("Open workflow →", "run", `data-id="${esc(c.workflow_id)}"`, "primary small") : ""}${c.publication?.status === "published" && safeGithubURL(c.publication.url) !== "#" ? `<a class="button small truffle-pr" href="${esc(safeGithubURL(c.publication.url))}" target="_blank" rel="noopener noreferrer">${sigil("flag")} View pull request ↗</a>` : ""}<details data-disclosure-key="truffle-evidence-${c.number}"><summary>Evidence & implementation brief</summary>${c.evidence.map(e => `<div class="truffle-source"><button class="subtle" data-action="file" data-path="${esc(e.path)}">${esc(e.path)}:${e.line}–${e.end_line || e.line}</button><pre class="console">${esc(e.quote)}</pre></div>`).join("")}${[["Implementation", c.plan], ["Verification commands", c.verification], ["Acceptance criteria", c.acceptance]].map(([title, rows]) => `<h3>${title}</h3><ul>${rows.map(r => `<li>${esc(r)}</li>`).join("")}</ul>`).join("")}</details></div></article>`).join("")}</div>
    ${!h.candidates.length && !busy ? empty("No convincing truffles in this pool.", "Review the skip reasons, broaden the search, or try another worker.") : ""}
    ${h.skipped.length ? `<details class="panel truffle-skips" data-disclosure-key="truffle-skips"><summary>Why ${h.skipped.length} issues stayed out</summary>${h.skipped.map(s => `<p><strong>#${s.number}</strong> ${esc(s.reason)}</p>`).join("")}</details>` : ""}`);
}
function openTruffleQueue() {
  const h = state.scout, numbers = [...truffleSelected(h)];
  if (!numbers.length) return toast("Select at least one issue first.");
  const defaults = {...state.config.publish, mode: state.config.publish?.mode === "auto" ? "auto" : "manual"};
  modal("Queue the fixes", `${numbers.length} selected · one isolated worktree and reviewed workflow per issue`,
    `<form id="truffle-queue-form"><p class="help-copy">${numbers.map(n => "#" + n).join(" · ")}</p>${publishFields("truffle", defaults)}<div class="field"><label for="truffle-attempts">Maximum attempts per stage</label><input id="truffle-attempts" name="attempts" type="number" min="1" max="5" value="2" required></div><div class="launch-note">Explore → plan → implement → independent review, one issue at a time. Closed issues and linked PRs are rechecked before launch. The queue pauses on a changed issue, failed review, quota or publication error; accepted work stays saved. Manual mode lets you inspect the diff before publishing. Automatic mode commits, pushes, and opens each accepted PR.</div><label class="check-field"><input name="allow_write" type="checkbox" required> Allow the selected fixes to edit their isolated worktrees</label><button type="submit" class="button primary">Start issue queue →</button></form>`, "truffle-queue");
  $('#truffle-publish-mode option[value="off"]').remove();
}
function recommendationCards(output) {
  return (output?.findings || [])
    .map(
      (f) =>
        `<div class="finding-card"><h3><span class="number">${f.number.toString().padStart(2, "0")}</span>${esc(f.title)}</h3><div class="actions">${button("Read finding", "finding", `data-number="${f.number}"`, "small ghost")}${state.report.status === "success" && output.status === "success" ? button("Implement this →", "implement", `data-number="${f.number}" data-node="${esc(output.node_id)}"`, "small primary") : ""}</div></div>`,
    )
    .join("");
}
function publishFields(prefix, value = state.config?.publish || {}, modes = true) {
  return `<div class="form-grid">${modes ? `<div class="field"><label for="${prefix}-publish-mode">PR publication</label><select id="${prefix}-publish-mode">${[["off", "Off · current workspace"], ["manual", "Manual · isolated worktree"], ["auto", "Automatic after review"]].map(([v, label]) => `<option value="${v}" ${value.mode === v ? "selected" : ""}>${label}</option>`).join("")}</select></div>` : ""}<div class="field"><label for="${prefix}-publish-base">Target branch</label><input id="${prefix}-publish-base" list="${prefix}-branches" value="${esc(value.base || "staging")}" required><datalist id="${prefix}-branches">${(state.config?.publish?.branches || []).map(b => `<option value="${esc(b)}">`).join("")}</datalist></div><div class="field"><label for="${prefix}-publish-remote">Remote</label><input id="${prefix}-publish-remote" value="${esc(value.remote || "origin")}" required></div><div class="field"><label for="${prefix}-publish-draft">PR type</label><select id="${prefix}-publish-draft"><option value="draft" ${value.draft !== false ? "selected" : ""}>Draft</option><option value="ready" ${value.draft === false ? "selected" : ""}>Ready for review</option></select></div></div>`;
}
function publishValues(prefix) {
  return { mode: $(`#${prefix}-publish-mode`)?.value || "manual", base: $(`#${prefix}-publish-base`).value,
    remote: $(`#${prefix}-publish-remote`).value, draft: $(`#${prefix}-publish-draft`).value === "draft" };
}
function publicationCard(report) {
  const p = report.publication || {}, g = report.git || {};
  if (!p.status && !g.branch) return "";
  const checks = p.pr?.statusCheckRollup || [];
  return `<section class="panel publication-panel"><div class="panel-header"><h3>Pull request</h3>${badge(p.status || "pending")}</div><div class="panel-body"><div class="facts"><span>Target</span><span>${esc(p.base || g.base)}</span><span>Branch</span><span class="mono">${esc(p.branch || g.branch)}</span>${p.commit ? `<span>Commit</span><span class="mono">${esc(p.commit.slice(0, 12))}</span>` : ""}</div>${p.error ? `<p class="blocker">${esc(p.error)}</p>` : ""}${p.url ? `<p><a href="${esc(safeGithubURL(p.url))}" target="_blank" rel="noopener noreferrer">Open PR #${esc(p.number)} ↗</a> · ${esc(p.pr?.state || "Published")}</p>${button("Refresh PR checks", "pr-refresh", "", "small")}<p class="help-copy">${p.checked_at_ms ? "Last checked " + date(p.checked_at_ms) : "CI status has not been fetched yet."}</p>${checks.map(c => `<p class="help-copy">${esc(c.name || c.context)} · ${esc(c.conclusion || c.state || c.status)}</p>`).join("")}` : `<p class="help-copy">${g.mode === "auto" ? "Publishes automatically after the implementation and review are accepted." : "Completed stages stay saved while you publish or retry."}</p>`}${g.workspace ? `<details><summary>Run worktree</summary><p class="mono">${esc(g.workspace)}</p></details>` : ""}</div></section>`;
}
function openPublish() {
  const report = state.report;
  const value = { ...state.config.publish, ...report.git, ...report.publication };
  modal("Open a pull request", "Choose the target, then inspect the exact diff and PR description.",
    `<form id="publish-preview-form" data-run="${esc(report.workflow_id)}">${publishFields("pr", value, false)}<p class="help-copy">The preview fetches the target branch and prepares a diff. Publication commits to a feature branch, pushes it, and opens the PR.</p><button class="button primary" type="submit">Preview PR →</button></form>`, "publish");
}
function showPublishPreview(p) {
  modal("Review pull request", `${p.repo} · ${p.branch} → ${p.base}`,
    `<form id="publish-form" data-run="${esc(p.workflow_id)}" data-snapshot="${esc(p.snapshot_id)}"><div class="field"><label for="pr-title">Title</label><input id="pr-title" name="title" value="${esc(p.title)}" maxlength="256" required></div><div class="field"><label for="pr-body">Description · Markdown</label><textarea id="pr-body" name="body" class="editor" required>${esc(p.body)}</textarea></div><label class="check-field"><input type="checkbox" name="draft" ${p.draft ? "checked" : ""}> Create as draft</label><details open><summary>Changes · ${p.files.length} files</summary><pre class="console publish-diff">${esc(p.diff)}</pre></details>${p.legacy ? '<label class="check-field"><input type="checkbox" name="accept_legacy_diff" required> This older run has no saved Git review snapshot. I checked this diff and confirm it is the change to publish.</label>' : '<p class="help-copy">This diff matches the saved tree from the accepted review.</p>'}<div class="dialog-footer"><small>Creates a commit, pushes the feature branch, and opens the PR against ${esc(p.base)}.</small><button class="button primary" type="submit">${p.commit ? "Retry publication →" : "Publish PR →"}</button></div></form>`, "publish");
}
function activityTimeline(node) {
  const entries = node?.activity_entries?.length ? node.activity_entries
    : (node?.messages || []).filter(m => !/^(worker session connected|worker turn completed)/.test(m))
      .map((text, i) => ({id: `legacy-${i}`, kind: /^(running a command|command finished)/.test(text) ? "tool" : "message", text, name: text}));
  const groups = [];
  for (const entry of entries) {
    if (["command", "tool"].includes(entry.kind)) {
      if (groups.at(-1)?.kind === "commands") groups.at(-1).items.push(entry);
      else groups.push({kind: "commands", id: entry.id, items: [entry]});
    } else groups.push(entry);
  }
  return groups.map(entry => {
    const key = `${state.node}-${node?.attempts || 0}-${entry.id}`;
    if (entry.kind === "commands") {
      const running = entry.items.some(c => c.status === "running") && node?.status === "running";
      const nonzero = entry.items.filter(c => c.failed).length;
      const count = entry.items.length;
      const label = running ? "Running" : nonzero ? `${nonzero} nonzero ${nonzero === 1 ? "exit" : "exits"}` : "Finished";
      return `<details class="activity-item command-group" data-activity-key="${esc(key)}" data-disclosure-key="${esc(key)}"><summary><span class="activity-symbol ${running ? "is-live" : nonzero ? "has-error" : ""}" aria-hidden="true">${running ? "●" : nonzero ? "!" : "✓"}</span><span class="command-group-copy"><span><strong>${count} ${entry.items.every(c => c.kind === "command") ? (count === 1 ? "command" : "commands") : (count === 1 ? "tool call" : "tool calls")}</strong><small>${label}</small></span><code>${esc(commandPreview(entry.items.at(-1)))}</code></span><span class="disclosure-chevron" aria-hidden="true">⌄</span></summary><div class="command-list">${entry.items.map(c => `<details class="command-detail" data-disclosure-key="${esc(key + '-' + c.id)}"><summary><span class="command-exit ${c.failed ? "has-error" : ""}">${c.exit_code != null ? `exit ${esc(c.exit_code)}` : c.status === "running" ? (node?.status === "running" ? "running" : "no exit") : "tool"}</span><code>${esc(commandPreview(c))}</code></summary>${c.command ? `<pre class="console">${esc(c.command)}</pre>` : ""}${c.output ? `<pre class="console command-output">${esc(c.output)}</pre>` : '<p class="help-copy">No command output captured.</p>'}</details>`).join("")}</div></details>`;
    }
    if (entry.kind === "message")
      return `<article class="activity-item worker-update" data-activity-key="${esc(key)}"><div class="update-label"><span aria-hidden="true">✦</span> ${esc(node.agent || "Worker")} <span>update</span></div><div class="markdown">${markdown(entry.text)}</div></article>`;
    return `<div class="activity-item activity-note ${entry.kind === "error" ? "blocker" : ""}" data-activity-key="${esc(key)}">${esc(entry.text)}</div>`;
  }).join("") || `<div class="activity-empty"><span aria-hidden="true">◌</span><h3>${node?.status === "running" ? "Waiting for the first update" : "No activity captured"}</h3><p>${node?.status === "running" ? (node.output_format === "plain" ? "Public text updates will appear here. This run does not emit individual tool receipts." : "Worker updates and commands will appear here as they arrive.") : "Open Deliverable for the saved result, or Run details for logs."}</p></div>`;
}
function workerActivity(node, report) {
  const running = node?.status === "running", worker = node?.agent || "Worker", a = node?.activity;
  const duration = a?.started_at_ms ? Math.max(0, Math.floor(((running ? Date.now() : a.updated_at_ms) - a.started_at_ms) / 1000)) : null;
  const elapsed = duration == null || !Number.isFinite(duration) ? "" : duration < 60 ? `${duration}s` : `${Math.floor(duration / 60)}m ${duration % 60}s`;
  const eventNames = {"node.started":"Started", "node.retrying":"Retrying", "node.succeeded":"Completed", "node.failed":"Stopped", "node.reused":"Reused", "workflow.created":"Run created", "workflow.started":"Run started", "workflow.resumed":"Run resumed", "workflow.finished":"Run finished"};
  return `<section class="worker-activity"><div class="activity-toolbar"><div><div class="eyebrow">${running ? '<span class="live-dot"></span> LIVE ACTIVITY' : "WORKER ACTIVITY"}</div><h2>${esc(state.node)} <span>with ${esc(worker)}</span></h2><p>${badge(node?.status || "pending")}${elapsed ? `<span>${elapsed} ${running ? "elapsed" : "total"}</span>` : ""}<span>Attempt ${node?.attempts || 0}</span>${node?.last_output_at_ms ? `<span>Last output ${age(node.last_output_at_ms)} ago</span>` : ""}</p></div>${button("Latest ↓", "activity-latest", 'aria-label="Jump to latest activity"', "small ghost")}</div><div class="activity-feed worker-timeline" data-scroll-key="worker-${esc(state.node)}-${node?.attempts || 0}" data-follow-tail tabindex="0" aria-label="Worker activity">${activityTimeline(node)}</div><p class="activity-follow-hint">${node?.output_format === "plain" ? "This run streams text updates; individual tool receipts were not recorded. " : ""}Scroll back to pause following. Jump to latest to catch up.</p><details class="activity-diagnostics" data-disclosure-key="run-details-${esc(state.node)}-${node?.attempts || 0}"><summary>Run details <span>Logs &amp; stage history</span></summary><div class="diagnostic-content">${a ? `<div class="diagnostic-stats"><span>Process <b>${esc(a.pid)}</b></span><span>stdout <b>${number(a.stdout_bytes)}B</b></span><span>stderr <b>${number(a.stderr_bytes)}B</b></span><span>Refresh <b>2s</b></span></div>` : ""}${node?.stderr ? `<h3>Worker stderr</h3><pre class="console">${esc(node.stderr)}</pre>` : ""}<h3>Stage history</h3><div class="activity-feed stage-history" data-scroll-key="workflow-events" data-newest-first>${report.events.slice(-40).reverse().map(e => `<div class="activity-item" data-activity-key="${esc(pretty(e))}"><time>${date(e.ts)}</time><span>${esc(eventNames[e.type] || e.type.replaceAll(/[._]/g, " "))}${e.node_id ? ` · ${esc(e.node_id)}` : ""}${e.status ? ` · ${esc(e.status)}` : ""}</span></div>`).join("")}</div></div></details></section>`;
}
function workflow() {
  const report = state.report,
    nodes = report.live_nodes;
  const task = report.task || "Untitled workflow";
  const normalized = task.replace(/\s+/g, " ").trim();
  const sentence = normalized.match(/^.{1,180}?[.!?](?:\s|$)/)?.[0].trim();
  const title =
    sentence ||
    (normalized.length > 160
      ? normalized.slice(0, 157).replace(/\s+\S*$/, "") + "…"
      : normalized);
  const request =
    task !== title
      ? `<details class="request-detail"><summary>Full request</summary><p>${esc(task)}</p></details>`
      : "";
  if (!nodes.some((n) => n.id === state.node))
    state.node =
      nodes.find((n) => n.status === "running")?.id ||
      report.primary_nodes[0] ||
      nodes[0]?.id;
  const node = nodes.find((n) => n.id === state.node),
    output = report.outputs.find((o) => o.node_id === state.node);
  const decisions =
    report.waves.flatMap((w) => w.nodes).find((n) => n.id === state.node)
      ?.decisions || {};
  const jobs = state.overview.jobs.filter(
      (j) => j.workflow_id === report.workflow_id,
    ),
    job = jobs.find((j) => active(j.status));
  let body = "";
  if (state.tab === "report")
    body = output
      ? `<div class="article-head"><div><h2>${esc(state.node)} · deliverable</h2><small>${esc(output.agent)} · ${output.source === "summary" ? "Saved summary" : "Full worker answer"}</small></div>${button("Copy Markdown", "copy-report", "", "small ghost")}</div>${output.warning ? `<div class="blocker">${esc(output.warning)}</div>` : ""}<article class="markdown">${markdown(output.text)}</article>`
      : empty(
          node?.status === "running"
            ? "The investigation is in progress."
            : "This stage has no deliverable yet.",
          "The complete answer will appear here as soon as the worker finishes. Open Activity to follow its progress.",
          button("Watch activity", "tab", 'data-tab="activity"'),
        );
  if (state.tab === "activity") body = workerActivity(node, report);
  if (state.tab === "evidence")
    body = `<h2>Verification &amp; handoff</h2>${(node?.result?.blockers || []).map((b) => `<div class="blocker">${esc(b)}</div>`).join("")}<h3>Reported checks</h3><pre class="console">${esc((node?.result?.tests || []).join("\n") || "No verification reported yet.")}</pre>${node?.result?.command_evidence?.length ? `<h3>Command observations</h3><p class="help-copy">These commands returned a nonzero exit during the turn. They may include searches with no matches, corrected lookups, or tests run before a fix. Unresolved issues belong in the final handoff above.</p><pre class="console">${esc(node.result.command_evidence.join("\n"))}</pre>` : ""}<h3>Changed files</h3><pre class="console">${esc((node?.result?.changed || []).join("\n") || "No changed files reported.")}</pre><p class="help-copy">These are worker receipts. Inspect the actual checks and diff before treating a claim as verified.</p>`;
  if (state.tab === "spec")
    body = `<div class="article-head"><h2>Workflow definition</h2>${button("Use as a new workflow", "custom-from-run", "", "small")}</div><pre class="console">${esc(pretty(report.spec))}</pre>`;
  if (node?.quota)
    body =
      `<div class="quota-notice"><h3>${esc(node.quota.agent)} · ${esc(node.quota.route)} route reached its provider limit</h3><p>${esc(node.quota.message)}</p><p>This is the worker provider’s quota, separate from the Fusion budget. An installed CLI can still depend on a remote account with its own limits.</p>${button("Retry with another worker →", "resume", "", "small")}<small>Accepted stages stay cached. Review runs in a separate worker session.</small></div>` +
      body;
  if (node?.permission_failure)
    body =
      `<div class="quota-notice"><h3>${esc(node.permission_failure.agent)} could not run a required tool</h3><p>${state.config?.execution_mode === "yolo" && node?.result?.execution_mode !== "yolo" ? "This attempt used restricted permissions. YOLO is now configured; retry to launch with full runtime access." : esc(node.permission_failure.message)}</p><p>The worker stopped at a permission gate. Retry after configuring that worker, or choose Auto to use another permitted worker. Accepted stages stay saved.</p>${button("Retry this stage →", "resume", "", "small")}</div>` +
      body;
  if (node?.coordinator_failure) {
    const notice = `<div class="quota-notice"><h3>${esc(node.coordinator_failure.message)}</h3><p>${esc(node.coordinator_failure.detail)}</p><p>Fusion could not capture the Git state required for review. Retrying a different worker cannot repair this coordinator failure. After correcting it, resume this stage; accepted stages remain saved.</p>${button("Resume this stage →", "resume", "", "small")}</div>`;
    body = notice + (state.tab === "report" && node.coordinator_failure.phase === "snapshot_before_review" ? '<p class="help-copy">No reviewer was launched, so there is no worker deliverable for this attempt.</p>' : body);
  }
  mount(`<button class="back" data-view="workflows">← All workflows</button><div class="detail-heading"><div class="eyebrow">${report.read_only ? "DISCOVERY & INTELLIGENCE" : "IMPLEMENTATION WORKFLOW"}</div><h1>${esc(title)}</h1>${request}<div class="run-meta">${badge(report.status)}<span class="mono">${esc(report.workflow_id)}</span><span>${date(report.started_at_ms)}</span><span>${report.read_only ? "Investigation task" : "Implementation task"}</span></div><div class="actions">${button("Export report ↓", "export-report", "", "small")}${report.status === "success" && !report.read_only && !report.publication?.url ? button(report.publication?.status === "failed" ? "Retry publication" : "Open PR", "publish", "", "small primary") : ""}${["paused_quota", "paused_budget", "interrupted", "failed"].includes(report.status) ? button("Resume workflow", "resume", "", "small primary") : ""}${job ? button("Stop run", "cancel", `data-id="${esc(job.id)}"`, "small danger") : ""}</div></div>
    <div class="pipeline" aria-label="Workflow stages">${nodes.map((n, i) => `<button class="stage ${n.id === state.node ? "selected" : ""}" data-action="stage" data-node="${esc(n.id)}"><span class="stage-index">${String(i + 1).padStart(2, "0")}</span>${badge(n.status)}<h3>${sigil(({explore:"compass",plan:"scroll",implement:"forge",review:"shield"})[n.id] || "flag", "stage-sigil")}${esc(n.id)}</h3><small>${esc(n.agent)} · attempt ${n.attempts}</small>${n.needs?.length ? `<div><small>after ${esc(n.needs.join(", "))}</small></div>` : ""}</button>`).join("")}</div>
    <div class="detail-grid"><div class="panel"><div class="panel-body"><div class="tabs">${[
      ["report", "Deliverable"],
      ["activity", "Activity"],
      ["evidence", "Evidence"],
      ["spec", "Workflow JSON"],
    ]
      .map(
        ([key, label]) =>
          `<button class="tab ${state.tab === key ? "active" : ""}" data-action="tab" data-tab="${key}">${label}</button>`,
      )
      .join(
        "",
      )}</div>${body}</div></div><aside class="detail-aside">${publicationCard(report)}<div class="panel"><div class="panel-header"><h3>Run at a glance</h3></div><div class="panel-body"><div class="facts"><span>Stages accepted</span><span>${nodes.filter((n) => n.status === "success").length} / ${nodes.length}</span><span>Reported cost</span><span>${report.cost.reported_calls ? "$" + Number(report.spent_usd).toFixed(4) : "Not reported"}</span><span>Cost coverage</span><span>${report.cost.reported_calls} / ${report.cost.calls} calls</span><span>Recorded-spend budget</span><span>${report.budget_usd ? "$" + report.budget_usd : "No limit"}</span></div>${!job && report.status === "running" ? '<p class="help-copy" style="margin:18px 0 0">Started outside this UI. Observe here; interrupt it from its original terminal.</p>' : ""}</div></div>
    ${
      Object.keys(decisions).length
        ? `<div class="panel"><div class="panel-header"><h3 class="guild-section-emblem">${sigil("rune")} Laya decisions</h3></div>${Object.entries(
            decisions,
          )
            .map(
              ([kind, d]) =>
                `<div class="decision-mini"><div class="mini-title"><span>${esc(kind)}</span><small>${d.applied ? "Applied" : "Advisory"}</small></div>${Object.entries(
                  d.recommendations || {},
                )
                  .map(
                    ([k, v]) =>
                      `<p>${esc(k)} → ${esc(v.value)} · ${(v.probability * 100).toFixed(1)}%</p><div class="probability"><i style="width:${Math.max(0, Math.min(100, v.probability * 100))}%"></i></div>`,
                  )
                  .join(
                    "",
                  )}<p>Policy action: ${esc(d.actual || d.status || "not recorded")}</p></div>`,
            )
            .join("")}</div>`
        : ""
    }
    ${output?.findings?.length ? `<div><div class="section-bar"><h2>Next moves <small>${output.findings.length}</small></h2></div>${recommendationCards(output)}</div>` : ""}${report.blockers.length ? `<div class="panel"><div class="panel-header"><h3>Open blockers</h3></div><div class="panel-body">${report.blockers.map((b) => `<div class="blocker">${esc(b.node_id)}: ${esc(b.blocker)}</div>`).join("")}</div>` : ""}</aside></div>`);
}
function decisionPredictions(record) {
  return Object.entries(record.recommendations || {})
    .map(([k, v]) => `${k} → ${v.value} (${(v.probability * 100).toFixed(1)}%)`)
    .join(" · ");
}
function labelDraft(record) {
  const key = state.workspace + ":" + record.id;
  const approved = record.labels?.at(-1);
  const draft = (state.labelDrafts[key] ||= {
    answers: { ...(approved?.answers || {}) },
    evidence: approved?.evidence || "",
    suggestion_id: approved?.suggestion_id || null,
    seen: approved?.suggestion_id || null,
    worker: state.garden?.agent || "auto",
    labeling_mode: state.garden?.labeling_mode || "single",
    council_agents: state.garden?.council_agents || [],
    approval_mode: state.garden?.approval_mode || "human", council_rule: state.garden?.council_rule || "unanimous",
    dirty: false,
    optionsDirty: false,
  });
  if (!draft.optionsDirty) Object.assign(draft, {
    worker: state.garden?.agent || "auto", labeling_mode: state.garden?.labeling_mode || "single",
    council_agents: state.garden?.council_agents || [], approval_mode: state.garden?.approval_mode || "human", council_rule: state.garden?.council_rule || "unanimous",
  });
  if (approved && !draft.dirty && draft.approved_at !== approved.time_ms) {
    draft.answers = {...record.reviewed_answers};
    draft.evidence = approved.evidence || "";
    draft.suggestion_id = draft.seen = approved.suggestion_id || null;
    draft.approved_at = approved.time_ms;
  }
  const suggestion = record.suggestions?.at(-1);
  if (suggestion && suggestion.suggestion_id !== draft.seen && !draft.dirty &&
      (!approved || suggestion.time_ms > approved.time_ms)) {
    draft.suggestion_id = draft.seen = suggestion.suggestion_id;
    draft.answers = Object.fromEntries(Object.entries(suggestion.answers).map(([k, v]) => [k, v.value]));
    draft.evidence = Object.entries(suggestion.answers).map(([k, v]) =>
      `${k} = ${v.value}: ${v.reason} [${v.evidence.join(", ")}]`).join("\n\n");
  }
  return draft;
}
function labelingControls(prefix, options) {
  const council = options.labeling_mode === "council";
  const workers = state.config?.workers || [];
  const members = options.council_agents?.length ? options.council_agents : workers.filter(w => w.available).slice(0, 3).map(w => w.agent);
  return `<div class="labeling-controls" data-labeling="${prefix}"><div class="form-grid"><div class="field"><label for="${prefix}-mode">Drafting method</label><select id="${prefix}-mode" name="labeling_mode"><option value="single" ${!council ? "selected" : ""}>Single worker</option><option value="council" ${council ? "selected" : ""}>Agent council</option></select></div><div class="field single-worker" ${council ? "hidden" : ""}><label for="${prefix}-worker">Labeling worker</label><select id="${prefix}-worker" name="agent"><option value="auto">Auto · available worker</option>${workers.map(w => `<option value="${esc(w.agent)}" ${options.worker === w.agent ? "selected" : ""} ${!w.available ? "disabled" : ""}>${esc(w.agent)}</option>`).join("")}</select></div></div><fieldset class="council-members" ${!council ? "hidden" : ""}><legend>Council members · choose at least two</legend><div>${workers.map(w => `<label class="check-field"><input type="checkbox" name="council_agents" value="${esc(w.agent)}" ${members.includes(w.agent) ? "checked" : ""} ${!w.available ? "disabled" : ""}> ${esc(w.agent)}${!w.available ? " · unavailable" : ""}</label>`).join("")}</div><p class="help-copy">Each member independently reads the same evidence. One worker call per member, run in sequence. Disagreements and abstentions remain visible.</p><div class="field"><label for="${prefix}-rule">Council agreement</label><select id="${prefix}-rule"><option value="unanimous" ${options.council_rule !== "available" ? "selected" : ""}>Every selected member must agree</option><option value="available" ${options.council_rule === "available" ? "selected" : ""}>Available members agree · minimum two</option></select><small>Available-member agreement skips quota limits, timeouts, missing runtimes and permission failures. At least two workers must answer with evidence; all participating members must agree. Abstentions and invalid assessments still block that answer.</small></div></fieldset><div class="field council-approval"><label for="${prefix}-approval">Label approval</label><select id="${prefix}-approval"><option value="human" ${options.approval_mode !== "council" ? "selected" : ""}>I approve the drafts</option><option value="council" ${options.approval_mode === "council" ? "selected" : ""}>Council approves unanimous answers</option></select><small>Choose council approval to assess and save unanimous answers automatically. This uses at least two workers. Human reviews are preserved; disputed or unsupported answers remain pending.</small></div></div>`;
}
function labelingValues(prefix) {
  return {agent: $(`#${prefix}-worker`).value, labeling_mode: $(`#${prefix}-mode`).value,
    council_rule: $(`#${prefix}-rule`).value,
    approval_mode: $(`#${prefix}-mode`).value === "council" ? $(`#${prefix}-approval`).value : "human",
    council_agents: [...document.querySelectorAll(`[data-labeling="${prefix}"] [name=council_agents]:checked:not(:disabled)`)].map(el => el.value)};
}
function councilAssessment(suggestion) {
  const c = suggestion.council;
  if (!c) return "";
  return `<section class="council-assessment"><h4>Council assessment · ${c.members.length} members</h4><p class="help-copy">${c.rule === "available" ? "Available-member agreement · at least two independent assessments. Operationally unavailable accounts are excluded from voting." : "Every selected member must agree."} Agreement measures consistency; evidence determines whether a label is supported.</p><div class="council-outcomes">${Object.entries(c.questions).map(([key,q])=>`<span class="pill">${esc(key)} · ${esc(q.state)} · ${q.votes}/${q.members} answered</span>`).join("")}</div>${c.members.map(m=>`<details><summary>${esc(m.requested_agent)} · ${m.status === "success" ? `${Object.keys(m.answers).length} answers` : "assessment failed"}</summary><p class="help-copy">Worker: ${esc(m.agent || m.requested_agent)} · Model: ${esc(m.model || "not reported")} · Run: ${esc(m.run_id || "not started")}</p>${m.error ? `<p class="blocker">${esc(m.error)}</p>` : ""}${Object.entries(m.answers || {}).map(([k,v])=>`<p><strong>${esc(k)} → ${esc(v.value)}</strong><br>${esc(v.reason)} <small>[${esc(v.evidence.join(", "))}]</small></p>`).join("")}${Object.entries(m.abstentions || {}).map(([k,v])=>`<p class="help-copy"><strong>${esc(k)} · abstained</strong><br>${esc(v)}</p>`).join("")}</details>`).join("")}</section>`;
}
function savedLabelStatus(record) {
  if (!record) return "";
  if (record.excluded) return "Example excluded from training. No approval required.";
  if (record.garden_state === "approved") return "Approved labels saved. No further approval is required.";
  return "";
}
function labelRunCard(run) {
  const members = run.members || [], done = members.filter(m => ["success", "error", "interrupted", "unavailable"].includes(m.status)).length;
  const approved = Object.keys(run.approval?.answers || {}).length;
  const running = run.status === "running";
  const record = state.decisions.find(r => r.id === run.decision_id);
  const latestApproval = record?.labels?.at(-1);
  const sameCouncilApproval = latestApproval?.source === "council_approved_suggestion" && latestApproval.suggestion_id === run.suggestion_id;
  const saved = !running && !sameCouncilApproval && savedLabelStatus(record);
  const title = saved && run.phase !== "approved" ? "Labels already reviewed" : running ? (run.labeling_mode === "council" ? "Council in session" : "Reading the evidence")
    : run.phase === "approved" ? "Council approval complete" : run.phase === "partial" ? "Approved what the council could agree on"
    : run.status === "failed" || run.status === "interrupted" ? "Assessment needs attention" : "Ready for your review";
  const questions = [...new Set([...Object.keys(run.questions || {}), ...members.flatMap(m => [...Object.keys(m.answers || {}), ...Object.keys(m.abstentions || {})])])];
  return `<section class="council-live ${running ? "is-live" : ""}" aria-label="Live labeling run"><div class="council-live-head"><div><div class="eyebrow">${running ? '<span class="live-orbit" aria-hidden="true"></span> LIVE LABELING' : "LABELING RESULT"}</div><h3>${title}</h3><p class="help-copy">${esc(run.decision_id)} · ${running ? age(run.started_at_ms) + " elapsed" : date(run.finished_at_ms)} · ${run.approval_mode === "council" ? (run.council_rule === "available" ? "Auto-approve · available members agree (minimum two)" : "Auto-approve · every selected member must agree") : "Human approval"}</p></div><span class="pill">${done}/${members.length} finished · ${members.filter(m => m.status === "success").length} assessments</span></div><div class="council-progress" role="progressbar" aria-label="Members completed" aria-valuemin="0" aria-valuemax="${members.length}" aria-valuenow="${done}"><i style="width:${members.length ? done / members.length * 100 : 0}%"></i></div>
    <div class="live-member-grid">${members.map(m => `<article class="live-member label-live-item ${esc(m.status)}" data-live-key="${esc(run.id + ':' + m.requested_agent + ':' + m.status)}"><div class="live-member-heading"><span class="member-avatar">${sigil(({codex:"scroll",claude:"spark",agy:"compass",grok:"forge"})[m.requested_agent] || "council")}</span><div><strong>${esc(m.requested_agent)}</strong><small>${m.status === "running" ? "Assessing independently…" : m.status === "pending" ? "Up next" : m.status === "success" ? "Assessment saved" : esc(m.status)}</small></div><span class="member-status" aria-hidden="true">${m.status === "success" ? "✓" : m.status === "error" ? "!" : m.status === "running" ? "◌" : "·"}</span></div>
    ${m.messages?.length ? `<p class="live-worker-update">${esc(m.messages.at(-1))}</p>` : m.status === "running" ? '<p class="help-copy">Waiting for the next public worker update.</p>' : ""}${m.error ? `<p class="blocker">${esc(m.error)}</p>` : ""}${Object.keys(m.answers || {}).length || Object.keys(m.abstentions || {}).length ? `<details data-disclosure-key="${esc(run.id + ':' + m.requested_agent)}"><summary>${Object.keys(m.answers || {}).length} ${Object.keys(m.answers || {}).length === 1 ? "answer" : "answers"} · ${Object.keys(m.abstentions || {}).length} abstentions</summary>${Object.entries(m.answers || {}).map(([key,v])=>`<p><strong>${esc(key)} → ${esc(v.value)}</strong><br>${esc(v.reason)} <small>[${esc((v.evidence || []).join(", "))}]</small></p>`).join("")}${Object.entries(m.abstentions || {}).map(([key,v])=>`<p class="help-copy"><strong>${esc(key)} · abstained</strong><br>${esc(v)}</p>`).join("")}</details>` : ""}${m.model ? `<small class="mono">${esc(m.model)}</small>` : ""}</article>`).join("")}</div>
    ${questions.length ? `<div class="live-votes" aria-label="Council votes">${questions.map(key=>`<div class="live-vote label-live-item" data-live-key="${esc(run.id + ':' + key + ':' + members.map(m=>m.answers?.[key]?.value || m.abstentions?.[key] || m.status).join('|'))}"><strong>${esc(key)}</strong><div>${members.map(m=>`<span class="vote-chip ${m.answers?.[key] ? "answered" : ""}">${esc(m.requested_agent)} <b>${esc(m.answers?.[key]?.value || (m.abstentions?.[key] ? "abstained" : m.status === "error" ? "failed" : m.status === "unavailable" ? "unavailable" : "…"))}</b></span>`).join("")}</div></div>`).join("")}</div>` : ""}
    ${run.approval ? `<div class="council-result label-live-item ${approved ? "has-approval" : ""}" data-live-key="${esc(run.id + ':approval:' + run.phase)}"><strong>${saved ? "Review complete" : approved ? `${approved} ${approved === 1 ? "answer" : "answers"} approved automatically` : "No automatic approval"}</strong><span>${esc(saved || run.approval.reason)}</span>${approved ? '<small>Saved with council provenance. You can inspect, correct or exclude these labels.</small>' : ""}</div>` : ""}${run.error ? `<p class="blocker">${esc(run.error)}</p>` : ""}
  </section>`;
}
function gardenLive() {
  const runs = state.labelRuns || [], current = runs.find(r => r.status === "running") || runs[0];
  if (!current) return "";
  return `<div class="garden-live">${labelRunCard(current)}<div class="live-run-actions">${button("Inspect this decision →", "quality-decision", `data-id="${esc(current.decision_id)}"`, "small")}${state.garden?.active_job ? button("Stop current run", "cancel", `data-id="${esc(state.garden.active_job.id)}"`, "small ghost") : ""}</div>${runs.length > 1 ? `<details class="label-run-history" data-disclosure-key="label-run-history"><summary>Recent labeling runs · ${runs.length - 1}</summary>${runs.filter(r=>r.id !== current.id).slice(0,7).map(r=>`<button class="label-run-row label-live-item" data-live-key="${esc(r.id + ':' + r.phase)}" data-action="quality-decision" data-id="${esc(r.decision_id)}"><span><strong>${esc(r.decision_id)}</strong><small>${date(r.started_at_ms)} · ${r.members.map(m=>esc(m.requested_agent)).join(' + ')}</small></span><span>${esc(r.phase.replaceAll('_',' '))}${Object.keys(r.approval?.answers || {}).length ? ' · ' + Object.keys(r.approval.answers).length + ' approved' : ''}</span><span>→</span></button>`).join("")}</details>` : ""}</div>`;
}
function labelEditor(record) {
  const draft = labelDraft(record);
  const suggestion = record.suggestions?.find(s => s.suggestion_id === draft.suggestion_id);
  const latest = record.suggestions?.at(-1);
  const automatic = record.labels?.find(e => e.source === "council_approved_suggestion" && e.suggestion_id === suggestion?.suggestion_id);
  const approval = record.label_run?.approval;
  const saved = savedLabelStatus(record);
  const reviewed = record.labels?.some(e => e.verified && e.suggestion_id === suggestion?.suggestion_id);
  const job = record.suggestion_job;
  const busy = job && active(job.status);
  const options = q => q.type === "noul" ? ["false", "true"] : q.type === "score" ? Object.keys(q.criteria).map((_, i) => String(i)) : Object.keys(q.criteria || {});
  return `<section class="review-form"><div class="article-head"><div><h3>Teach from verified evidence</h3><p class="help-copy">Choose human review or council approval. Inspect the votes, evidence and saved answers here.</p></div></div>
    <div id="label-live-region" data-scroll-context="label-live:${esc(state.workspace)}:${esc(record.id)}">${record.label_run ? labelRunCard(record.label_run) : ""}</div>${saved ? `<div class="label-saved-status" role="status">${sigil("shield")}<span>${esc(saved)} You can make corrections below.</span></div>` : ""}${labelingControls("label", draft)}<div class="label-tools">${button(busy ? "Drafting labels…" : draft.approval_mode === "council" ? "Run council & approve" : latest ? "Regenerate labels" : "Suggest labels", "suggest-labels", busy ? "disabled" : "", "primary")}</div>
    <p class="help-copy">Uses your configured worker accounts. Laya’s prediction and prior labels are withheld; unsupported answers stay unlabeled.</p>
    ${job ? `<div class="label-job" role="status">${badge(job.status)}<span>${busy ? "Reading evidence and drafting labels · " + age(job.started_at_ms) : job.status === "success" ? (latest && !Object.keys(latest.answers).length ? "Assessment saved; no labels suggested." : saved || approval?.reason || "Draft saved. Review below before approving.") : "No new draft was saved. Open activity for the error, then retry with another worker."}</span>${button("View activity", "job", `data-id="${esc(job.id)}"`, "small")}${busy ? button("Stop", "cancel", `data-id="${esc(job.id)}"`, "small") : ""}</div>` : ""}
    ${latest && latest.suggestion_id !== draft.suggestion_id ? `<div class="launch-note">A new draft is ready. Your edits have been preserved. ${button("Use latest draft", "use-label-draft", "", "small")}</div>` : ""}
    ${suggestion && !Object.keys(suggestion.answers).length ? `<div class="launch-note">No labels were suggested. Check the reasons below. You can fill in any answer supported by the original input and add your evidence, or leave it unlabeled. Only approved, supported answers enter training exports.</div>` : ""}
    ${suggestion ? `<div class="label-reasoning"><div class="article-head"><h3>Suggested assessment</h3><span class="pill">${automatic ? "Council approved" : reviewed ? "Reviewed" : "Draft"} · ${esc(suggestion.agent)}</span></div>${Object.entries(suggestion.answers).map(([key, item]) => `<p><strong>${esc(key)} → ${esc(item.value)}</strong><br>${esc(item.reason)} <small>[${esc(item.evidence.join(", "))}]</small></p>`).join("")}${Object.entries(suggestion.abstentions).map(([key, reason]) => `<p class="help-copy"><strong>${esc(key)} · needs evidence</strong><br>${esc(reason)}</p>`).join("")}${councilAssessment(suggestion)}<details><summary>Evidence used · ${suggestion.sources.length} sources</summary>${suggestion.sources.map(s => `<h4>${esc(s.id)} · ${esc(s.title)}</h4>${s.timing ? `<p class="help-copy">${esc(s.timing)}</p>` : ""}<pre class="console label-source">${esc(typeof s.text === "string" ? s.text : pretty(s.text))}${s.truncated ? "\n[Excerpt truncated]" : ""}</pre>`).join("")}</details></div>` : ""}
    <form id="label-form" data-decision="${esc(record.id)}"><div class="form-grid">${Object.entries(record.questions || {}).map(([key, q], i) => `<div class="field"><label for="label-answer-${i}">${esc(key)}</label><select id="label-answer-${i}" name="${esc(key)}"><option value="">Leave unlabeled</option>${options(q).map(v => `<option value="${esc(v)}" ${draft.answers[key] === v ? "selected" : ""}>${esc(v)}</option>`).join("")}</select><small>${esc(q.instructions || "")}</small></div>`).join("")}</div><div class="field"><label for="label-evidence">Verification evidence</label><textarea id="label-evidence" placeholder="What did you check? Which reproduction, diff, or test proves the answer?" required>${esc(draft.evidence)}</textarea></div><p class="help-copy">${saved ? "These labels are already saved. Editing and saving below records a human correction." : "Select at least one answer and add verification evidence to approve. You can leave other questions unlabeled; only selected answers enter training."}</p><button class="button primary" type="submit" ${canApproveLabels(draft) && (!saved || draft.dirty) ? "" : "disabled"}>${saved ? "Save label changes" : draft.suggestion_id ? "Approve labels" : "Save reviewed labels"}</button>${record.labels?.length ? `<p class="help-copy" style="margin-top:15px">${record.labels.length} review(s) saved for this decision.</p>` : ""}</form></section>`;
}
function captureLabelEdits() {
  const form = $("#label-form");
  const record = state.decisions.find(r => r.id === form?.dataset.decision);
  if (!record) return;
  const draft = labelDraft(record);
  draft.answers = Object.fromEntries(new FormData(form));
  draft.evidence = $("#label-evidence").value;
  draft.dirty = true;
  form.querySelector('button[type="submit"]').disabled = !canApproveLabels(draft);
  if (savedLabelStatus(record)) form.querySelector('button[type="submit"]').textContent = "Save label changes";
}
const percent = value => typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "Not measured";
function qualityDashboard(l) {
  const q = l.quality;
  if (!q) return "";
  const reviews = (q.draft_reviews.edited || 0) + (q.draft_reviews.unchanged || 0);
  const councilQuestions = (q.council.agreed || 0) + (q.council.disputed || 0) + (q.council.insufficient || 0);
  const issueLinks = ids => ids.map(id => button(esc(id), "quality-decision", `data-id="${esc(id)}"`, "small ghost mono")).join(" ");
  return `<section class="panel quality-panel"><div class="panel-header"><div><div class="eyebrow">TRAINING DATA HEALTH</div><h3>What’s in your lessons?</h3></div><span class="pill">${q.examples} retained examples</span></div><div class="panel-body"><div class="quality-metrics">${[
    [q.unique_inputs, "Distinct inputs", `${q.duplicate_examples} repeated examples`],
    [q.groups.train + q.groups.validation, "Independent groups", `${q.groups.train} training · ${q.groups.validation} validation`],
    [`${q.evidence_recorded}/${q.answers}`, "Evidence recorded", "Approval evidence; source and approver are tracked below"],
    [reviews ? percent((q.draft_reviews.edited || 0) / reviews) : "Not measured", "Drafts edited at approval", `${reviews} retained draft reviews · ${q.revised_answers} answer revisions`],
  ].map(([v,title,copy])=>`<div><strong>${esc(v)}</strong><h4>${title}</h4><p class="help-copy">${esc(copy)}</p></div>`).join("")}</div>
  ${q.conflicts.length || q.cross_split_duplicates.length ? `<div class="quality-warning"><strong>${q.conflicts.length} conflicting input sets · ${q.cross_split_duplicates.length} sets shared across training and validation</strong><p>Review conflicting answers and exclude redundant examples. Shared inputs can inflate validation scores. Re-export after correcting the data.</p></div>` : `<p class="help-copy">${q.examples ? "No conflicting labels or identical inputs shared across the two splits detected." : "Approve examples to start measuring data quality."} These checks cannot establish label correctness or detect every form of leakage.</p>`}
  <div class="learning-columns"><div><h4>Where the approved answers came from</h4>${Object.entries(q.sources).map(([name,count])=>`<div class="quality-source"><span>${name === "council_auto" ? "Council approved automatically" : name === "human" ? "Human-authored" : name === "council" ? "Council draft + human approval" : esc(name) + " draft + human approval"}</span><strong>${count}</strong></div>`).join("") || '<p class="help-copy">No retained approvals yet.</p>'}</div><div><h4>Council agreement</h4><p>${councilQuestions ? `${percent((q.council.agreed || 0) / councilQuestions)} agreed under the selected rule · ${q.council.disputed || 0} disputed · ${q.council.insufficient || 0} without full support` : "No council assessments yet."}</p><p class="help-copy">${q.council.drafts || 0} latest drafts · ${q.council.failed_members || 0} failed assessments · ${q.council.unavailable_members || 0} unavailable members. Agreement measures consistency, not correctness.</p></div></div>
  <details class="quality-details"><summary>Label balance · spot missing classes and skew</summary>${q.balance.map(b=>`<div class="quality-balance"><strong>${esc(b.question)}</strong><div class="balance-segments" aria-label="Label counts">${Object.entries(b.counts).filter(([,n])=>n).map(([name,n],i)=>`<span style="flex:${n};opacity:${1-i*.12}" title="${esc(name)}: ${n}"></span>`).join("")}</div><p class="help-copy">${Object.entries(b.counts).map(([name,n])=>`${esc(name)}: ${n}`).join(" · ")} · largest class ${percent(b.dominant_share)}</p>${b.missing_labels.length ? `<small>Not represented: ${b.missing_labels.map(esc).join(", ")}</small>` : ""}</div>`).join("")}<p class="help-copy">Imbalance may reflect real usage. Check underrepresented outcomes before treating overall accuracy as representative.</p></details>
  ${q.duplicate_sets.length ? `<details class="quality-details"><summary>Inspect duplicate and conflicting examples · ${q.duplicate_sets.length} sets</summary>${q.duplicate_sets.slice(0,20).map(ids=>`<div class="quality-issue"><strong>${q.conflicts.some(c=>c.ids[0]===ids[0]) ? "Conflicting answers" : "Identical input"}${q.cross_split_duplicates.some(c=>c[0]===ids[0]) ? " · in both splits" : ""}</strong><div>${issueLinks(ids.slice(0,8))}</div></div>`).join("")}${q.duplicate_sets.length > 20 ? '<p class="help-copy">Showing the first 20 sets.</p>' : ""}</details>` : ""}</div></section>`;
}
function impactDashboard(l) {
  const comparisons = l.comparisons || [];
  return `<section class="panel impact-panel"><div class="panel-header"><div><div class="eyebrow">MEASURED RESULTS</div><h3>Are the lessons helping?</h3></div><span class="pill">${comparisons.filter(c=>c.delta !== null).length} matched comparisons</span></div><div class="panel-body"><p class="help-copy">Export approved labels, evaluate the source model, train a candidate, then evaluate that candidate on the same export. Each row below pairs exact model identities and the same held-out examples. New weights take effect only when you choose that checkpoint in Settings.</p>
    ${comparisons.length ? comparisons.slice(0,8).map(c=>`<article class="impact-comparison"><div class="article-head"><div><strong>${esc(c.candidate_id)}</strong><small>${date(c.time_ms)} · benchmark ${esc(c.benchmark?.slice(0,12) || "unknown")}</small></div><div class="impact-delta ${c.delta !== null && c.delta < 0 ? "regressed" : ""}">${c.delta === null ? "Awaiting evidence" : `${c.delta >= 0 ? "+" : ""}${(c.delta*100).toFixed(1)} pp`}</div></div><div class="impact-bars">${[["Source", c.baseline_accuracy],["Candidate", c.accuracy]].map(([name,value])=>`<div><span>${name}</span><div class="coverage-track"><i style="width:${typeof value === "number" ? value*100 : 0}%"></i></div><strong>${percent(value)}</strong></div>`).join("")}</div><p class="help-copy">${c.train_answers ?? "Unrecorded"} training answers · ${c.validation_questions ?? "?"} held-out questions in ${c.validation_groups ?? "?"} groups.<br>Shuffled-state control: ${percent(c.control_accuracy)} · training-majority baseline: ${percent(c.majority_accuracy)}.</p>${c.notes.map(note=>`<p class="quality-warning">${esc(note)}</p>`).join("")}${c.holdout_status !== "checked" ? `<p class="help-copy">${c.holdout_status === "contaminated" ? "Known overlap: do not interpret the accuracy as evidence of generalization." : "This run has no complete training-overlap audit. Re-evaluate with a candidate trained by this version to track lineage."}</p>` : '<p class="help-copy">No exact input or group overlap detected against recorded local training lineage. Sample size and repeated benchmark use still matter.</p>'}${Object.keys(c.by_kind).length ? `<details><summary>Results by decision type</summary>${Object.entries(c.by_kind).map(([kind,v])=>`<div class="quality-source"><span>${esc(kind)} · ${v.questions} questions</span><strong>${percent(c.baseline_by_kind[kind]?.accuracy)} → ${percent(v.accuracy)}</strong></div>`).join("")}</details>` : ""}</article>`).join("") : `<div class="impact-empty"><strong>No measured improvement yet.</strong><p>${l.labeled_questions} approved answers are available. ${l.candidates.length ? "Evaluate your candidate and its source model to see the difference here." : "They become model changes after you train a candidate. Paired evaluations will show gains and regressions here over time."}</p></div>`}
  </div></section>`;
}
function learningDashboard() {
  const l = state.learning;
  if (!l) return "";
  const model = l.model;
  let running = 0;
  const growth = l.growth.map(day => ({...day, total: running += day.labels})).slice(-30);
  return `<section class="learning-dashboard" aria-label="Laya learning progress">
    <div class="learning-hero panel"><div><div class="eyebrow guild-section-emblem">${sigil("rune")} YOUR LAYA · THE LOREKEEPER</div><h2>${l.candidates.length ? "Lessons becoming a model." : "Every verified lesson counts."}</h2><p>${l.candidates.length ? "Follow your candidates from training through evaluation. Your configured model is shown separately." : "You’re building Laya’s training data. Approved labels become new weights when you train a candidate."}</p><div class="learning-actions">${button("Review labels", "lab-tab", 'data-tab="review"', "primary")}${button("Watch the garden", "lab-tab", 'data-tab="garden"')}</div></div>
    <div class="learning-growth"><div><strong>${number(l.labeled_questions)}</strong><span>approved answers retained</span></div><div class="growth-chart" role="img" aria-label="Cumulative retained labels by first review date">${growth.length ? growth.map(day => `<div style="height:${Math.max(5, day.total / Math.max(1, running) * 100)}%" title="${esc(date(day.day_ms))}: ${day.total} retained answers"><i></i></div>`).join("") : '<p>Your first approval starts the chart.</p>'}</div><small>Current retained labels · excludes removed examples</small></div></div>
    <div class="learning-pipeline">${[
      [l.reviewed_decisions, "Reviewed examples", `${l.labeled_questions} answers across ${Object.keys(l.kinds).length} decision types`],
      [l.exports.length, "Dataset exports", `${l.groups.train} training / ${l.groups.validation} held-out groups`],
      [l.candidates.length, "Trained candidates", "Separate checkpoints; never auto-activated"],
      [l.evaluations.length, "Completed evaluations", "Held-out questions and shuffled-state control"],
    ].map(([n,title,copy],i)=>`<div><span class="eyebrow">0${i+1}</span><strong>${number(n)}</strong><h3>${title}</h3><p>${copy}</p></div>`).join("")}</div>
    <div class="learning-columns"><section class="panel"><div class="panel-header"><h3>Model in use</h3><span class="pill">${esc(model.mode)}</span></div><div class="panel-body"><h2>${model.path ? "Custom checkpoint" : "Bundled Laya checkpoint"}</h2><p class="help-copy">${model.path ? esc(model.path) : "The router selects the bundled checkpoint. Label approvals alone do not modify it."}</p>${model.training.method ? `<p>${esc(model.training.method)} · ${esc(model.training.steps)} training steps</p>` : ""}<p class="help-copy">${model.qualified_buckets} saved qualified calibration buckets (model-specific)${model.mode === "shadow" ? " · recommendations remain advisory" : model.mode === "off" ? " · inference is disabled" : " · automatic actions still require qualification"}.</p><details><summary>Last observed model identity</summary><p class="mono">${esc(model.last_observed_identity || "No successful inference recorded")}</p><small>${model.last_observed_at_ms ? date(model.last_observed_at_ms) + " · historical observation; may predate configuration changes" : "Run a probe to observe the configured model."}</small></details></div></section>
    <section class="panel"><div class="panel-header"><h3>Teaching coverage</h3><span class="pill">${l.labeled_questions} / ${l.eligible_questions} answers</span></div><div class="panel-body">${Object.entries(l.kinds).map(([kind,v])=>`<div class="coverage-row"><div><strong>${esc(kind)}</strong><small>${v.labeled} / ${v.questions}</small></div><div class="coverage-track"><i style="width:${v.questions ? v.labeled / v.questions * 100 : 0}%"></i></div></div>`).join("") || '<p class="help-copy">New decisions will appear here as you use Fusion.</p>'}<p class="help-copy">${l.can_train ? "Training and held-out groups are present. More independent workflows improve the evidence." : "Training needs approved examples in both training and held-out workflow groups. Keep reviewing independent runs; repeats of one workflow stay together."}</p><details><summary>Label balance &amp; historical agreement</summary>${Object.entries(l.distribution).map(([key,labels])=>`<p><strong>${esc(key)}</strong><br>${Object.entries(labels).map(([label,n])=>`${esc(label)}: ${n}`).join(" · ")}</p>`).join("")}<p>${percent(l.agreement.rate)} agreement on ${l.agreement.compared} reviewed answers. This compares recorded predictions with approved human or council labels; it is not a held-out score or proof of improvement.</p></details></div></section></div>
  </section>`;
}
function trainingDashboard() {
  const l = state.learning;
  if (!l) return "";
  const model = l.model, latestExport = l.exports[0];
  return `<section class="training-dashboard">${trainingQuest()}<details class="manual-learning" data-disclosure-key="manual-learning"><summary>Manual training tools &amp; saved candidates</summary><div class="panel learning-training"><div class="eyebrow guild-section-emblem">${sigil("forge")} TRAIN A CANDIDATE</div><h2>Turn approved lessons into new weights.</h2><p>Export your labels, train a separate candidate, then compare it with the current model in Results.</p><div class="learning-actions">${button("Export approved labels", "learning-step", 'data-step="export"', "primary")}${button("Train candidate", "learning-step", `data-step="train" ${!l.can_train || !latestExport ? 'disabled title="Export labels from both train and validation groups first"' : ""}`)}${button("Evaluate configured model", "learning-step", `data-step="evaluate" ${!latestExport ? "disabled" : ""}`)}</div><p class="help-copy">${l.labeled_questions} approved answers · ${l.groups.train} training / ${l.groups.validation} held-out groups. Your active checkpoint stays unchanged until you select a new one in Settings.</p></div>
    <section class="panel lab-exports"><div class="panel-header"><h3>Dataset exports</h3><span class="pill">${l.exports.length} saved</span></div><div class="panel-body">${l.exports.length ? l.exports.slice(0,8).map(e=>`<div class="evaluation-row"><div><strong>${esc(e.id)}</strong><small>${date(e.started_at_ms)}</small></div>${button("View export", "job", `data-id="${esc(e.id)}"`, "small")}</div>`).join("") : '<p class="help-copy">Your first export creates a saved dataset from the approved labels.</p>'}</div></section>
    ${l.candidates.length ? `<section class="panel candidate-panel"><div class="panel-header"><h3>Your trained candidates</h3><span class="pill">${l.candidates.length} saved</span></div><div class="candidate-grid">${l.candidates.slice(0,6).map(c=>{
      const evaluation = l.evaluations.find(e=>e.result.model_identities?.length === 1 && e.result.model_identities[0] === c.training.model_identity);
      const matched = l.comparisons?.find(item=>item.candidate_id === c.id && item.evaluation_id === evaluation?.id);
      const score = evaluation?.result;
      const delta = matched?.delta == null ? null : matched.delta * 100;
      return `<article class="candidate-card"><div class="article-head"><strong>${esc(c.id)}</strong><span class="pill">${model.path === c.path ? "Configured" : "Candidate"}</span></div><p class="help-copy">${date(c.started_at_ms)} · ${esc(c.training.steps ?? "?")} steps · ${esc(c.training.train_groups ?? "?")} training groups</p><div class="candidate-score"><strong>${percent(score?.accuracy)}</strong><span>held-out accuracy${score ? ` · ${esc(score.validation_questions)} questions` : ""}</span></div><p class="help-copy">Shuffled-state control: ${percent(score?.control_accuracy)}${delta !== null ? `<br>${delta >= 0 ? "+" : ""}${delta.toFixed(1)} percentage points vs source model on the same held-out benchmark.` : "<br>Evaluate the source model on the same dataset to measure improvement."}</p><div class="actions">${button("Evaluate candidate", "learning-step", `data-step="evaluate" data-model="${esc(c.path)}" data-dataset="${esc(c.dataset)}"`, "small")}${button("Training activity", "job", `data-id="${esc(c.id)}"`, "small")}</div><details><summary>Training lineage</summary><pre class="console">${esc(pretty(c.training))}</pre><p class="mono">${esc(c.path)}</p></details></article>`;
    }).join("")}</div></section>` : ""}
    ${l.evaluations.length || l.jobs.length ? `<details class="learning-history"><summary>Learning history · ${l.jobs.length} recent jobs</summary>${l.evaluations.slice(0,6).map(e=>`<div class="evaluation-row"><div><strong>${e.result.model_path ? "Custom checkpoint" : "Configured / bundled model"}</strong><small>${esc(e.id)} · ${esc(e.result.validation_questions ?? 0)} held-out questions</small></div><div>${percent(e.result.accuracy)}<small>Control: ${percent(e.result.control_accuracy)}</small></div>${button("View activity", "job", `data-id="${esc(e.id)}"`, "small")}</div>`).join("")}${jobRows(l.jobs)}</details>` : ""}
  </details></section>`;
}
function gardenPanel() {
  const g = state.garden;
  if (!g) return "";
  const ready = state.decisions.filter(r => r.garden_state === "needs_review").length;
  const automatic = g.approval_mode === "council";
  const title = ready ? `${ready} ${ready === 1 ? "draft is" : "drafts are"} ready to review.`
    : g.state === "drafting" ? "Your next draft is in progress."
    : !g.enabled ? "Automatic drafting is paused." : g.queued ? "Your next drafts are queued." : "Ready for new decisions.";
  return `<section class="panel garden-panel"><div class="garden-main"><div class="eyebrow guild-section-emblem">${sigil("seed")} DATA GARDEN · NO DAILY CAP</div><h2>${title}</h2><p>${ready ? (automatic ? "Supported council answers are approved automatically. These remaining drafts need attention: inspect their votes, unavailable workers and evidence." : "Open Review to inspect the pending labels and evidence. Choose council approval in Garden settings to automate supported answers.") : automatic ? "The council assesses new decisions and approves unanimous answers automatically. Disagreements stay in your review queue." : "Automatically draft labels as decisions arrive. You edit and approve the useful ones."}</p>
    <div class="garden-usage"><span>${g.used_today} automatic drafts started today (UTC)</span><span>${g.queued} queued · one draft at a time</span><span>${automatic ? (g.council_rule === "available" ? "Auto-approve · available members agree (minimum two)" : "Auto-approve · every selected member") : "Human approval"}</span><span>${g.labeling_mode === "council" ? `Council · ${esc(g.council_agents.join(" + "))}` : `Single worker · ${esc(g.agent)}`}</span></div>
    ${g.error ? `<p class="blocker">${esc(g.error)}</p>` : ""}${g.latest_job?.status === "failed" ? '<p class="help-copy">The last draft failed. Review its activity and retry explicitly; garden does not repeatedly call a failing worker for the same decision.</p>' : ""}</div>
    <div class="garden-controls"><span class="pill">${esc(g.state.replaceAll("_", " "))}</span>${ready ? button(`Review ${ready} ${ready === 1 ? "draft" : "drafts"} →`, "garden-review", "", "primary") : ""}${button(g.enabled ? "Garden settings" : "Enable auto-drafts", "garden-settings", "", ready ? "" : "primary")}${g.enabled ? button("Pause garden", "garden-pause", "", "small ghost") : ""}${g.active_job ? button("View current draft", "job", `data-id="${esc(g.active_job.id)}"`, "small") : g.latest_job ? button("Latest activity", "job", `data-id="${esc(g.latest_job.id)}"`, "small ghost") : ""}</div></section>`;
}
function gardenToolbar() {
  const choices = [["all","All decisions"],["needs_review","Ready to review"],["needs_draft","Needs a draft"],["needs_evidence","Needs evidence"],["drafting","Drafting"],["approved","Approved"],["needs_attention","Failed drafts"],["excluded","Excluded"]];
  return `<div class="garden-toolbar" role="group" aria-label="Filter learning queue">${choices.map(([id,label])=>button(`${label} <small>${id === "all" ? state.decisions.length : state.decisions.filter(r=>r.garden_state===id).length}</small>`, "garden-filter", `data-filter="${id}" aria-pressed="${state.gardenFilter === id}"`, "small")).join("")}</div>`;
}
function openGarden() {
  const g = state.garden;
  modal("Tend your data garden", "Continuous labeling. Your choice of approver.", `<form id="garden-form"><label class="check-field"><input type="checkbox" name="enabled" ${g.enabled ? "checked" : ""}> Automatically label new decisions</label>${labelingControls("garden", {...g, worker: g.agent})}<label class="check-field"><input type="checkbox" name="include_existing"> Include existing eligible decisions and drafts in this setup</label><div class="launch-note">No daily cap. Uses your configured worker accounts and their provider quotas. One assessment runs at a time, with one attempt per decision under this council approval setup. Human-reviewed examples are preserved. Pause also withdraws pending garden approvals. Only approved answers enter training exports. The queue runs while the control-room server is running, even with the browser closed.</div><button class="button primary" type="submit">Save garden settings</button></form>`, "garden");
}

function decisions() {
  const focusedTab = document.activeElement?.closest('[role="tab"][data-action="lab-tab"]')?.id;
  const tabScroll = $(".lab-tabs")?.scrollLeft || 0;
  const selected = labTabs.find(([id]) => id === state.labTab) || labTabs[0];
  let body = "";
  if (state.labTab === "overview") body = trainingQuest(true) + learningDashboard();
  else if (state.labTab === "review") body = decisionReview();
  else if (state.labTab === "garden") body = gardenPanel() + `<div id="garden-live-region">${gardenLive() || empty("The garden is quiet.", "Enable automatic drafts to follow new labeling runs here. You can inspect decisions in Review at any time.")}</div>`;
  else if (state.labTab === "training") body = trainingDashboard();
  else if (state.labTab === "quality" && state.learning) body = qualityDashboard(state.learning);
  else if (state.labTab === "results" && state.learning) body = trainingQuest() + impactDashboard(state.learning);
  const ready = state.decisions.filter(r => r.garden_state === "needs_review").length;
  mount(intro("LOCAL INTELLIGENCE", "Laya lab.", selected[2], `<div class="actions">${button("Learning tools", "learning")}${button("New probe", "probe", "", "primary")}</div>`) +
    `<div class="lab-tabs" role="tablist" aria-label="Laya lab sections">${labTabs.map(([id, label]) => `<button type="button" id="lab-tab-${id}" role="tab" aria-selected="${state.labTab === id}" aria-controls="lab-panel" tabindex="${state.labTab === id ? 0 : -1}" data-action="lab-tab" data-tab="${id}">${label}${id === "review" && ready ? `<small aria-label="${ready} ready to review">${ready}</small>` : ""}${id === "garden" && state.garden?.active_job ? '<i class="lab-live-dot" aria-label="Labeling in progress"></i>' : ""}</button>`).join("")}</div>` +
    `<section id="lab-panel" class="lab-panel" role="tabpanel" aria-labelledby="lab-tab-${state.labTab}" tabindex="0" data-lab-section="${state.labTab}">${body}</section>`);
  $(".lab-tabs").scrollLeft = tabScroll;
  if (focusedTab) document.getElementById(focusedTab)?.focus({preventScroll:true});
}

function decisionReview() {
  const records = state.decisions.filter(r => state.gardenFilter === "all" || r.garden_state === state.gardenFilter);
  const missing = state.decision && !state.decisions.some(r => r.id === state.decision);
  const record = missing ? null : records.find(r => r.id === state.decision) || records[0];
  if (!missing) state.decision = record?.id || null;
  writeLabURL("replace");
  return gardenToolbar() + `<div class="lab-layout"><div class="panel decision-list">${records.length ? records.map((r) => `<div class="decision-row ${r.id === state.decision ? "selected" : ""}" role="button" tabindex="0" data-action="decision" data-id="${esc(r.id)}"><div class="decision-kind">${sigil("rune")} ${esc(r.kind)} ${badge(r.status)}</div><div class="prediction">${esc(decisionPredictions(r) || r.error || "No prediction")}</div><div class="run-meta"><span>${esc(r.mode)}</span><span>${number(r.duration_ms)}ms</span>${r.reviewed_answers && Object.keys(r.reviewed_answers).length ? "<span>✓ reviewed</span>" : ""}<span>${esc((r.garden_state || "").replaceAll("_", " "))}</span></div></div>`).join("") : '<div class="panel-body"><p>No decisions recorded yet. Run a local probe or start a workflow.</p></div>'}</div>
    <div class="panel"><div class="panel-body">${
      record
        ? `<div class="article-head"><div><h2>${esc(record.kind)} decision</h2><small>${esc(record.id)}</small></div><span class="pill">${esc(record.mode)}</span></div>${record.error ? `<div class="blocker">${esc(record.error)}</div>` : ""}${record.truncated ? '<div class="blocker">Input was truncated. This decision cannot qualify for automatic action or reviewed labels.</div>' : ""}${Object.entries(
            record.recommendations || {},
          )
            .map(
              ([key, value]) =>
                `<div class="prob-row"><header><span>${esc(key)} → <strong>${esc(value.value)}</strong></span><span>${(value.probability * 100).toFixed(1)}%</span></header><div class="probability"><i style="width:${Math.max(0, Math.min(100, value.probability * 100))}%"></i></div><small>${esc(record.questions?.[key]?.instructions)}</small></div>`,
            )
            .join(
              "",
            )}<h3 style="margin-top:25px">What actually happened</h3>${record.applications?.length ? record.applications.map((a) => `<p class="help-copy"><strong>${esc(a.actual)}</strong> · ${a.applied ? "Model recommendation applied" : "Policy decision; recommendation advisory"}<br>${esc(a.reason || "")}</p>`).join("") : '<p class="help-copy">This probe has no workflow action attached.</p>'}<details><summary>Input &amp; model evidence</summary><pre class="console">${esc(pretty({ state: record.state, prediction: record.prediction, model: record.model_identity, context: record.context }))}</pre></details>${
            (record.status === "ok" || record.status === "unscored") && !record.truncated
              ? `<div class="garden-record-tools">${record.excluded ? "Excluded from future exports and automatic drafts. Existing datasets and trained models are unchanged." : "Keep useful examples; exclude noisy or unsuitable inputs from future exports."}${button(record.excluded ? "Restore example" : "Exclude example", "exclude-label", `data-id="${esc(record.id)}" data-excluded="${!record.excluded}"`, "small")}</div>` + (record.excluded ? "" : labelEditor(record))
              : ""
          }`
        : empty(
            missing ? "Decision unavailable." : state.gardenFilter === "all" ? "A small model. An inspectable decision." : "No decisions in this queue.",
            missing ? "This decision is not in the loaded records for this workspace. Select another decision from the list." : state.gardenFilter === "all" ? "Probe intake, review, recovery, or acceptance without launching a coding agent." : "Choose another filter, or let the garden prepare new drafts.",
            button("Try a probe", "probe", "", "primary"),
          )
    }</div></div></div>`;
}
function appearanceControls() {
  const current = ORCAppearance.get();
  return `<div class="appearance-controls"><div class="appearance-mode" role="group" aria-label="Color mode">${[
    ["light", "☀", "Light"],
    ["dark", "☾", "Dark"],
    ["system", "◐", "System"],
  ]
    .map(
      ([id, icon, label]) =>
        `<button type="button" data-action="appearance-mode" data-mode="${id}" aria-pressed="${current.mode === id}"><span aria-hidden="true">${icon}</span> ${label}</button>`,
    )
    .join(
      "",
    )}</div><div class="theme-grid" role="group" aria-label="Color theme">${ORCAppearance.themes.map((t) => `<button type="button" class="theme-option" data-action="appearance-theme" data-theme="${t.id}" aria-pressed="${current.theme === t.id}" style="--swatch-dark:${t.dark};--swatch-light:${t.light};--swatch-bg:hsl(${t.hue} ${t.saturation}% 12%)"><span class="theme-preview" aria-hidden="true">${sigil("orc")}</span><span>${t.name}</span><span class="theme-check" aria-hidden="true">✓</span></button>`).join("")}</div><p class="help-copy">Saved automatically in this browser. System follows your device’s appearance.</p></div>`;
}
function syncAppearanceControls() {
  const current = ORCAppearance.get();
  $$('[data-action="appearance-mode"]').forEach((el) =>
    el.setAttribute("aria-pressed", String(el.dataset.mode === current.mode)),
  );
  $$('[data-action="appearance-theme"]').forEach((el) =>
    el.setAttribute("aria-pressed", String(el.dataset.theme === current.theme)),
  );
  $("#appearance-name").textContent =
    ORCAppearance.themes.find((t) => t.id === current.theme).name +
    " · " +
    current.mode;
}
document.addEventListener("orc-appearance-change", syncAppearanceControls);
syncAppearanceControls();

function settings() {
  const c = state.config;
  mount(
    intro(
      "WORKSPACE CONFIGURATION",
      "Make it work your way.",
      "Tune workers, routes, model selection, and Laya. Changes apply to future launches in this workspace.",
    ) +
      `<section class="panel appearance-panel"><div class="panel-header"><h2>Appearance</h2><span class="pill">This browser</span></div><div class="panel-body">${appearanceControls()}</div></section>` +
      `<div class="info-box" style="margin:0 0 24px"><h3>Settings source · ${esc(c.source)}</h3><p>Save creates or updates this workspace’s config. Other keys stay intact; masked secrets keep their existing values.${Object.keys(c.environment).length ? ` Environment overrides: ${esc(pretty(c.environment))}` : ""}</p></div><div class="settings-grid"><section class="panel"><div class="panel-header"><h2>Fusion &amp; Laya</h2><span class="pill">${c.qualified_buckets} qualified buckets</span></div><div class="panel-body"><div class="form-grid"><div class="field full"><label for="setting-execution">Runtime access</label><select id="setting-execution">${["yolo", "restricted"].map((m) => `<option value="${m}" ${m === c.execution_mode ? "selected" : ""}>${m === "yolo" ? "YOLO · full access, no approval prompts" : "Restricted · per-worker permissions"}</option>`).join("")}</select><small>YOLO applies to all workers and routes, including reviews. Review-only tasks remain instructions to avoid edits; runtime access is unrestricted.</small></div><div class="field"><label>Laya mode</label><select id="setting-mode">${["off", "shadow", "active"].map((m) => `<option ${m === c.effective.decisions.mode ? "selected" : ""}>${m}</option>`).join("")}</select></div><div class="field"><label>Preferred worker</label><select id="setting-worker">${["codex", "claude", "agy", "grok"].map((m) => `<option ${m === c.effective.sidekick ? "selected" : ""}>${m}</option>`).join("")}</select></div></div><p class="help-copy">Active mode still requires qualified calibration, allowed decision kinds, and every workflow permission check.</p><h3>GitHub publishing</h3>${publishFields("settings", c.publish)}<p class="help-copy">Manual and automatic modes start new implementation runs in a clean worktree from the target branch. Automatic mode authorizes a feature-branch push and PR creation after successful review.</p><div class="field"><label for="fusion-config">Project .fusion.json</label><textarea id="fusion-config" class="editor" spellcheck="false">${esc(pretty(c.local))}</textarea><small>Full control over routes, workers, timeouts, telemetry, and calibration paths.</small></div><div class="setting-footer">${button("Save Fusion settings", "save-settings", 'data-target="fusion"', "primary")}${button("View resolved settings", "effective", "", "ghost")}</div></div></section><section class="panel"><div class="panel-header"><h2>ORC model preferences</h2></div><div class="panel-body"><p class="help-copy">Choose a project model, small model, permission mode, or saved profile. Project preferences leave your global ORC defaults intact.</p><div class="field"><label for="orc-config">Project .orc.json</label><textarea id="orc-config" class="editor" spellcheck="false" placeholder='{"model":"provider/model-id"}'>${esc(pretty(c.orc))}</textarea><small>Browse the ORC models tab for available IDs and tool-fit results.</small></div>${button("Save ORC settings", "save-settings", 'data-target="orc"', "primary")}<hr><h3>Local runtime</h3><p class="help-copy">Install or repair Laya’s optional Python runtime and cached English checkpoint. Normal inference uses cached weights.</p>${button("Set up Laya runtime", "setup")}<hr><h3>Workers on PATH</h3><div class="worker-strip">${c.workers.map((w) => `<span class="worker-chip" title="${esc(w.reason || "")}"><b>${w.available ? "●" : "○"}</b>${esc(w.agent)} · ${!w.available ? "missing" : w.automatic_ready === false ? "headless setup needed" : "installed"}</span>`).join("")}</div>${c.workers
        .filter((w) => w.available && w.automatic_ready === false)
        .map(
          (w) => `<p class="help-copy">${esc(w.agent)}: ${esc(w.reason)}</p>`,
        )
        .join("")}</div></section></div>`,
  );
}
function models() {
  mount(
    intro(
      "OPENROUTER × CLAUDE",
      "Find the right worker.",
      "Inspect your resolved ORC model, tool-capable models, quality rankings, and saved profiles.",
    ) +
      `<div class="model-toolbar">${[
        ["status", "Resolved model"],
        ["models", "Tool-capable models"],
        ["free", "Free models"],
        ["quality", "Quality rankings"],
        ["profiles", "Profiles"],
      ]
        .map(([cmd, label]) =>
          button(
            label,
            "orc",
            `data-command="${cmd}"`,
            cmd === "status" ? "primary" : "",
          ),
        )
        .join(
          "",
        )}</div><div class="panel"><div class="panel-header"><h3 id="orc-title">Resolved model</h3><small>Read through your installed ORC CLI</small></div><div class="panel-body"><pre id="orc-output" class="model-output">Loading…</pre></div></div><div class="info-box"><h3>Tool support ≠ verified tool fit</h3><p>FIT means ORC verified a tool round trip. UNTESTED is not a failure. Manage project model IDs and profiles in Settings.</p></div>`,
  );
  loadOrc("status");
}
async function loadOrc(command) {
  $("#orc-output").textContent = "Reading ORC…";
  try {
    const data = await api("orc?command=" + command);
    if (state.view !== "models") return;
    $("#orc-title").textContent = {
      status: "Resolved model",
      models: "Tool-capable models",
      free: "Free models",
      quality: "Quality rankings",
      profiles: "Saved profiles",
    }[command];
    let text = data.text;
    try {
      text = pretty(JSON.parse(text));
    } catch {}
    $("#orc-output").textContent = text + (data.error ? "\n" + data.error : "");
    $$("[data-action=orc]").forEach((b) =>
      b.classList.toggle("primary", b.dataset.command === command),
    );
  } catch (e) {
    if ($("#orc-output")) $("#orc-output").textContent = e.message;
  }
}
function modal(title, description, body, kind = null) {
  state.modal = kind;
  $("#dialog-content").innerHTML =
    `<div class="dialog-header"><div><h2 id="dialog-title">${esc(title)}</h2><p>${esc(description)}</p></div><button class="icon-button" data-action="close" aria-label="Close dialog">×</button></div><div class="dialog-body"><div id="dialog-error" class="dialog-error" hidden></div>${body}</div>`;
  if (!$("#dialog").open) $("#dialog").showModal();
  enhanceMarkdown($("#dialog"));
}
function modalError(message) {
  const el = $("#dialog-error");
  if (el) {
    el.textContent = message;
    el.hidden = false;
  } else error(message);
}
function closeModal() {
  state.modal = null;
  $("#dialog").close();
}
const templates = {
  audit: {
    kind: "discovery",
    text: "Find the five highest-value bugs and improvements in this repository. Read repository instructions and preserve existing changes. Cite exact files and evidence. Rank by impact, effort, and regression risk. Use numbered bold headings (1. **Title**) for each finding. For the top three, provide reproduction steps, a scoped implementation plan, acceptance criteria, and exact verification commands. Return recommendations only. Use BLOCKERS: none when investigation is complete; list unmeasured risks as caveats.",
  },
  feature: { kind: "build", text: "" },
  debug: { kind: "debug", text: "" },
};
function openLaunch(options = {}) {
  const kind = options.kind || "discovery";
  modal(
    options.from_workflow ? "Implement a finding" : "Start a new run",
    "Give Fusion an outcome. It handles the stages and saves the evidence.",
    `<form id="launch-form"><input type="hidden" name="from_workflow" value="${esc(options.from_workflow || "")}"><input type="hidden" name="from_node" value="${esc(options.from_node || "")}"><input type="hidden" name="finding" value="${esc(options.finding || "")}">${options.from_workflow ? `<div class="launch-note">Recommendation ${esc(options.finding)} · ${esc(options.title || "")}<br>The worker will revalidate the finding against your current checkout.</div>` : ""}<div class="form-grid"><div class="field"><label for="launch-kind">Workflow</label><select id="launch-kind" name="kind">${[
      ["discovery", "Discover & plan"],
      ["build", "Build a feature"],
      ["debug", "Reproduce & fix"],
      ["review", "Independent review"],
      ["sweep", "Sweep in parallel"],
      ["delegate", "Single worker"],
      ["workflow", "Custom workflow JSON"],
    ]
      .map(
        ([v, t]) =>
          `<option value="${v}" ${kind === v ? "selected" : ""}>${t}</option>`,
      )
      .join(
        "",
      )}</select></div><div class="field"><label for="launch-mode">Laya mode for this run</label><select id="launch-mode" name="mode">${["shadow", "off", "active"].map((m) => `<option ${m === (state.config?.mode || "shadow") ? "selected" : ""}>${m}</option>`).join("")}</select></div></div><div class="field" id="prompt-field"><label for="launch-text">${options.from_workflow ? "Additional constraints" : "What should happen?"}</label><textarea id="launch-text" name="text" rows="6" placeholder="Describe a feature, a bug, an investigation, or paste a GitHub issue URL…">${esc(options.text || "")}</textarea></div><div id="across-field" class="field" hidden><label for="launch-across">Dimensions to sweep, comma separated</label><input id="launch-across" name="across" placeholder="auth, payments, data integrity, performance"><p class="hint">One read-only worker per dimension, in parallel, then one that reads all of them and writes a ranked account.</p></div><div id="custom-field" class="field" hidden><label for="launch-spec">fusion.workflow.v1 JSON</label><textarea id="launch-spec" class="editor" name="spec" spellcheck="false">${esc(pretty(options.spec || { schema: "fusion.workflow.v1", task: "Inspect this repository", nodes: [{ id: "explore", agent: "auto", write: false, task: "Inspect the repository and report useful findings. Do not delegate further." }] }))}</textarea></div><div class="form-grid" id="worker-fields" hidden><div class="field"><label>Worker</label><select name="agent">${["auto", "codex", "claude", "agy", "grok"].map((a) => `<option>${a}</option>`).join("")}</select></div><div class="field"><label>Role</label><select name="role">${["review", "discovery", "planning", "implementation"].map((a) => `<option>${a}</option>`).join("")}</select></div><div class="field full"><label>Route</label><select name="route"><option value="">Automatic / native</option>${Object.keys(
      state.config?.effective?.routes || {},
    )
      .map((r) => `<option>${esc(r)}</option>`)
      .join(
        "",
      )}</select></div></div><div id="build-fields"><div class="form-grid"><div class="field"><label for="launch-attempts">Maximum attempts per stage</label><input id="launch-attempts" name="attempts" type="number" min="1" max="5" value="${kind === "discovery" ? 1 : 2}"></div><div class="field"><label for="launch-budget">Reported-spend budget · USD</label><input id="launch-budget" name="budget" type="number" min="0" step="0.5" value="0"><small>0 = no limit. Not a hard cap when providers omit costs.</small></div></div><label class="check-field"><input type="checkbox" name="prepare" id="prepare-only"> Prepare brief and workflow only</label></div><label class="check-field" id="write-field"><input type="checkbox" name="allow_write" id="allow-write"> Allow this run to edit workspace files</label><section id="launch-publish-fields"><h3>Pull request</h3>${publishFields("launch")}<p class="help-copy">Manual and automatic publishing use a separate worktree from the selected target. Automatic mode commits and pushes accepted changes and creates the PR.</p></section><div class="launch-note" id="launch-preview"></div><div class="dialog-footer"><small>${state.config?.execution_mode === "yolo" ? "YOLO · full runtime access. " : "Restricted runtime. "}Runs use your configured agent accounts. Closing this tab leaves them running.</small><button class="button primary" type="submit" id="launch-submit">Start run →</button></div></form>`,
    "launch",
  );
  $("#launch-form").addEventListener("change", updateLaunch);
  updateLaunch();
}
function updateLaunch() {
  const k = $("#launch-kind").value,
    prep = $("#prepare-only").checked && !["delegate", "workflow"].includes(k);
  $("#custom-field").hidden = k !== "workflow";
  $("#across-field").hidden = k !== "sweep";
  $("#prompt-field").hidden = k === "workflow";
  $("#worker-fields").hidden = k !== "delegate";
  $("#build-fields").hidden = ["delegate", "workflow"].includes(k);
  const writes =
    ["build", "debug", "delegate", "workflow"].includes(k) && !prep;
  $("#write-field").hidden = !writes;
  $("#launch-publish-fields").hidden = !["build", "debug"].includes(k);
  if (!writes) $("#allow-write").checked = false;
  $("#launch-submit").textContent =
    prep && !["delegate", "workflow"].includes(k)
      ? "Prepare workflow →"
      : "Start run →";
  $("#launch-preview").textContent =
    k === "discovery"
      ? "Explore → plan. Reads the repository and produces recommendations."
      : k === "sweep"
        ? "Explore → one read-only worker per dimension, in parallel → one synthesis that reads them all. Never writes."
      : k === "review"
        ? "Explore → independent review. Unresolved findings block acceptance."
        : k === "delegate"
          ? "One fresh worker session with the selected route and role."
          : k === "workflow"
            ? "Runs the exact nodes, dependencies, permissions, and checks in this definition."
            : prep
              ? "Creates an inspectable brief and workflow. No coding workers start."
              : "Explore → plan → implement → independent review. Edits require the checkbox above.";
}
async function launch(body) {
  const job = await api("launch", body);
  closeModal();
  await refresh(true);
  openJob(job.id);
  toast("Launch started. Progress is saved locally.");
}
async function openJob(id) {
  try {
    const job = await api("job?id=" + encodeURIComponent(id));
    modal(
      job.title || job.action,
      "Launch activity · " + id,
      `<div id="job-content"></div>`,
      { job: id },
    );
    renderJob(job);
  } catch (e) {
    error(e.message);
  }
}
function renderJob(job) {
  if (!$("#job-content")) return;
  const result = job.result || {};
  const controls = `<div class="actions" style="margin:16px 0">${job.action?.startsWith("truffle-") ? button("Open hunt →", "truffle-open", `data-id="${esc(result.id || "")}"`, "primary") : ""}${job.workflow_id ? button("Open workflow →", "job-workflow", `data-id="${esc(job.workflow_id)}"`, "primary") : ""}${active(job.status) ? button(job.status === "stopping" ? "Stopping…" : "Stop job", "cancel", `data-id="${esc(job.id)}" ${job.status === "stopping" ? "disabled" : ""}`, "danger") : ""}${result.workflow && !result.workflow_id ? button("Inspect prepared workflow", "prepared", `data-path="${esc(result.workflow)}"`) : ""}</div>`;
  replaceContent(
    $("#job-content"),
    `<div class="pill-row">${badge(job.status)}<small>${active(job.status) ? age(job.started_at_ms) + " elapsed" : date(job.finished_at_ms)}</small></div>${controls}${job.label_run ? labelRunCard(job.label_run) : ""}${job.error ? `<div class="blocker">${esc(job.error)}</div>` : ""}<pre class="console job-console" data-scroll-key="job-console" data-follow-tail>${esc(job.console || (active(job.status) ? "Starting the local coordinator…" : "Job finished. See its result below."))}</pre>${job.output ? `<details ${!active(job.status) && !job.workflow_id ? "open" : ""}><summary>Result</summary><pre class="console">${esc(typeof result === "object" && Object.keys(result).length ? pretty(result) : job.output)}</pre></details>` : ""}<p class="help-copy" style="margin:16px 0 0">You can close this dialog. The job and its logs remain available under Launch activity.</p>`,
    JSON.stringify([state.workspace, job.id]),
  );
}
function openProbe() {
  modal(
    "Ask the local model",
    "Probe a decision without starting a coding worker.",
    `<form id="probe-form"><div class="field"><label>Decision kind</label><select name="kind">${["intake", "review", "acceptance", "recovery"].map((k) => `<option>${k}</option>`).join("")}</select></div><div class="field"><label>Input · plain text or JSON</label><textarea name="text" required rows="7" placeholder='{"task":"Add CSV export with tests","summary":"Did nothing","changed":[],"tests":[]}'>Investigate the highest-impact reliability improvements in this repository. Return recommendations only.</textarea></div><p class="help-copy">The first checkpoint load can take tens of seconds. Subsequent decisions within the same process reuse the loaded model.</p><div class="dialog-footer"><small>Local inference · no coding agents</small><button class="button primary">Run probe →</button></div></form>`,
    "probe",
  );
}
function openLearning(options = {}) {
  modal(
    "Learn from reviewed outcomes",
    "Export, train, evaluate, and calibrate without automatically promoting a model.",
    `<form id="learning-form"><div class="field"><label>Action</label><select name="action" id="learning-action"><option value="export">Export reviewed labels</option><option value="train">Train a separate candidate</option><option value="evaluate">Evaluate with shuffled-state control</option><option value="calibrate">Calibrate held-out predictions</option></select></div><div class="field"><label>Dataset path · inside this workspace’s .fusion directory</label><input name="dataset" placeholder="ui/jobs/…/dataset.jsonl"><small>Export does not need a dataset. Other actions require a prior export or evaluation output.</small></div><div class="field"><label>Candidate model path · optional, inside .fusion</label><input name="model_path" placeholder="ui/jobs/…/candidate"><small>Used by train/evaluate; leave empty to use the configured checkpoint.</small></div><div class="launch-note">Outputs are written to a new job directory. Training never changes your active model. Calibration needs independent held-out groups to qualify.</div><button class="button primary">Start learning job →</button></form>`,
    "learning",
  );
  if (options.action) $("#learning-action").value = options.action;
  $("#learning-form [name=dataset]").value = options.dataset || state.learning?.exports?.[0]?.path || "";
  $("#learning-form [name=model_path]").value = options.model_path || "";
}
async function openFile(path) {
  try {
    const data = await api("file?path=" + encodeURIComponent(path));
    modal(
      "Source & artifacts",
      data.path,
      `<pre class="source-code">${esc(data.text)}</pre>`,
      null,
    );
  } catch (e) {
    toast(e.message);
  }
}
function download(name, text) {
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
function openResume() {
  const nodes = state.report.live_nodes.filter((n) => n.status !== "success");
  const selected = nodes.find((n) => n.id === state.node) || nodes[0];
  if (!selected) return toast("All stages are already accepted.");
  const worker =
    selected.agent === "auto" || selected.quota || selected.permission_failure
      ? "auto"
      : "";
  modal(
    "Resume workflow",
    "Keep accepted stages and retry unfinished work with your chosen worker.",
    `<form id="resume-form"><p class="help-copy">${esc(state.report.workflow_id)} · ${state.config.execution_mode === "yolo" ? "YOLO runtime: full access, no permission prompts" : "Restricted runtime"}</p><div class="field"><label for="resume-node">Stage to retry</label><select id="resume-node" name="node">${nodes.map((n) => `<option value="${esc(n.id)}" ${n.id === selected.id ? "selected" : ""}>${esc(n.id)} · ${esc(n.status.replaceAll("_", " "))}</option>`).join("")}</select></div><div class="field"><label for="resume-worker">Worker or route</label><select id="resume-worker" name="worker"><option value="" ${worker === "" ? "selected" : ""}>Keep the configured worker</option><option value="auto" ${worker === "auto" ? "selected" : ""}>Auto · fall back to available workers</option>${(state.config?.workers || []).map((w) => `<option value="${w.agent}" ${!w.available ? "disabled" : ""}>${w.agent} · ${!w.available ? "not on PATH" : w.automatic_ready === false ? "headless setup needed" : "installed"}</option>`).join("")}${Object.entries(
      state.config?.effective?.routes || {},
    )
      .map(
        ([name, route]) =>
          `<option value="route:${esc(name)}">${esc(name)} · ${esc(route.agent)} route</option>`,
      )
      .join(
        "",
      )}</select><small>Auto skips exhausted or currently blocked routes. Selecting a worker explicitly retries it using the current runtime access mode. Local CLIs can still call paid remote providers.</small></div><div class="field"><label for="resume-attempts">Attempt limit per stage</label><input id="resume-attempts" name="max_attempts" type="number" min="${selected.attempts + 1}" max="100" value="${Math.max(state.report.spec.max_attempts, selected.attempts + 1)}" required><small>Attempts already used count toward this limit. Fallback never resets the counter.</small></div>${!state.report.read_only ? '<label class="check-field"><input name="allow_write" type="checkbox" required> Allow this workflow to edit workspace files</label>' : ""}<button class="button primary">Resume →</button></form>`,
    "resume",
  );
}
const labTabs = [
  ["overview", "Overview", "Your model, teaching coverage, and progress at a glance."],
  ["review", "Review", "Inspect decisions, edit suggested labels, and approve the useful lessons."],
  ["garden", "Garden", "Configure automatic labeling and watch the council work live."],
  ["training", "Training", "Export approved data, train candidates, and run evaluations."],
  ["quality", "Quality", "Understand the evidence, balance, and provenance of your training data."],
  ["results", "Results", "Compare candidates with their source model on the same held-out examples."],
];
const labFilters = ["all", "needs_review", "needs_draft", "needs_evidence", "drafting", "approved", "needs_attention", "excluded"];
function readRoute() {
  const [path, query = ""] = location.hash.slice(1).split("?");
  const parts = path.split("/");
  const params = new URLSearchParams(query);
  const view = ["overview", "workflows", "workflow", "truffle", "routing", "decisions", "models", "settings"].includes(parts[0]) ? parts[0] : "overview";
  let id = null;
  try { id = parts[1] ? decodeURIComponent(parts[1]) : null; } catch {}
  return {view, id: view === "decisions" ? null : id, workspace: params.get("w"),
    labTab: labTabs.some(([key]) => key === id) ? id : "overview",
    forestTab: params.get("tab"), patch: params.get("patch"), grade: params.get("grade"), query: params.get("q"),
    decision: params.get("decision"), filter: labFilters.includes(params.get("filter")) ? params.get("filter") : "all"};
}
function routeHash() {
  if (state.view === "truffle") {
    const params = new URLSearchParams(), f = state.forest;
    if (state.workspace) params.set("w", state.workspace);
    if (f.tab !== "woodland") params.set("tab", f.tab);
    if (f.patch !== "all") params.set("patch", f.patch);
    if (f.grade !== "all") params.set("grade", f.grade);
    if (f.query) params.set("q", f.query);
    return "#truffle" + (state.id ? "/" + encodeURIComponent(state.id) : "") + (params.size ? "?" + params : "");
  }
  if (state.view !== "decisions") return "#" + state.view + (state.id ? "/" + encodeURIComponent(state.id) : "");
  const params = new URLSearchParams();
  if (state.workspace) params.set("w", state.workspace);
  if (state.labTab === "review") {
    if (state.decision) params.set("decision", state.decision);
    if (state.gardenFilter !== "all") params.set("filter", state.gardenFilter);
  }
  return "#decisions/" + state.labTab + (params.size ? "?" + params : "");
}
function writeRoute(mode = "push") {
  const hash = routeHash();
  if (location.hash !== hash) history[mode === "replace" ? "replaceState" : "pushState"](null, "", hash);
}
function writeLabURL(mode) { if (state.view === "decisions") writeRoute(mode); }
function openLabTab(tab, options = {}) {
  state.labTab = labTabs.some(([key]) => key === tab) ? tab : "overview";
  if ("decision" in options) state.decision = options.decision;
  if ("filter" in options) state.gardenFilter = options.filter;
  writeLabURL(options.history || "push");
  decisions();
}
async function navigate(view, id = null, options = {}) {
  state.view = view;
  state.id = id;
  state.node = null;
  state.tab = "report";
  state.signature = "";
  state.epoch++;
  writeRoute(options.history || "push");
  $$(".nav-item").forEach((b) =>
    b.classList.toggle(
      "active",
      b.dataset.view === (view === "workflow" ? "workflows" : view),
    ),
  );
  $("#view-name").textContent =
    {
      overview: "Overview",
      workflows: "Workflows",
      workflow: "Workflow",
      truffle: "Truffle pig",
      routing: "Routing",
      decisions: "Laya lab",
      models: "ORC models",
      settings: "Settings",
    }[view] || "Overview";
  $("#content").innerHTML = '<div class="loading">Loading workspace…</div>';
  error("");
  try {
    await refresh(true);
  } catch (e) {
    error(e.message);
  }
}
async function refresh(force = false) {
  const epoch = state.epoch,
    w = state.workspace;
  try {
    const data = await api("overview", undefined, w);
    if (epoch !== state.epoch) return;
    state.overview = data;
    $("#connection").textContent = "live";
    error("");
    if (force || !state.config) {
      state.config = await api("config", undefined, w);
      if (epoch !== state.epoch) return;
      $("#mode-chip").textContent = "Laya · " + state.config.mode;
      $("#execution-chip").textContent =
        state.config.execution_mode === "yolo"
          ? "YOLO · full access"
          : "Restricted runtime";
    }
    let signature = pretty({ ...data, now_ms: 0 });
    if (state.view === "truffle") {
      const scout = await api("truffle" + (state.id ? "?id=" + encodeURIComponent(state.id) : ""), undefined, w);
      if (epoch !== state.epoch) return;
      state.scout = scout;
      signature = pretty({scout, hunts:data.hunts, jobs:data.jobs});
    }
    if (state.view === "routing") {
      const routing = await api("routing-tasks" + (state.routingModel ? "?model=" + encodeURIComponent(state.routingModel) : ""), undefined, w);
      if (epoch !== state.epoch) return;
      state.routing = routing;
      signature = pretty(routing);
    }
    if (state.view === "workflow") {
      const report = await api(
        "workflow?id=" + encodeURIComponent(state.id),
        undefined,
        w,
      );
      if (epoch !== state.epoch) return;
      state.report = report;
      signature = pretty(report);
    }
    if (
      state.view === "decisions"
    ) {
      const d = await api("decisions", undefined, w);
      if (epoch !== state.epoch) return;
      state.decisions = d.records;
      state.learning = d.learning;
      state.trainingLoop = d.training_loop;
      state.garden = d.garden;
      state.labelRuns = d.label_runs || [];
      signature = pretty(d);
    }
    let didRender = false;
    const formsActive =
      !!$("input:focus,textarea:focus,select:focus");
    if (
      force ||
      (signature !== state.signature &&
        !formsActive &&
        !["settings", "models"].includes(state.view))
    ) {
      didRender = true;
      state.signature = signature;
      if (state.view === "overview") overview();
      else if (state.view === "workflows") workflows();
      else if (state.view === "truffle") truffleView();
      else if (state.view === "routing") routingView();
      else if (state.view === "workflow") workflow();
      else if (state.view === "decisions") decisions();
      else if (state.view === "settings") settings();
      else if (state.view === "models") models();
    }
    if (state.view === "decisions" && !didRender) {
      if ($("#garden-live-region")) replaceContent($("#garden-live-region"), gardenLive(), "garden-live:" + state.workspace);
      const current = state.decisions.find(r => r.id === state.decision);
      if ($("#label-live-region")) replaceContent($("#label-live-region"), current?.label_run ? labelRunCard(current.label_run) : "", "label-live:" + state.workspace + ":" + state.decision);
    }
    if (state.modal?.job) {
      const id = state.modal.job,
        job = await api("job?id=" + encodeURIComponent(id), undefined, w);
      if (state.modal?.job === id && epoch === state.epoch) renderJob(job);
    }
  } catch (e) {
    $("#connection").textContent = "offline";
    error(e.message);
    if (force) throw e;
  }
}
async function chooseWorkspace(id, route = null) {
  state.workspace = id;
  state.scout = null;
  forestRoute(route || {});
  localStorage.setItem("fusion-workspace", id);
  state.config = null;
  state.learning = state.garden = state.trainingLoop = null;
  state.learningRound = null;
  state.labelRuns = [];
  state.gardenFilter = route?.filter || "all";
  state.decision = route?.decision || null;
  state.labTab = route?.labTab || "overview";
  state.decisions = [];
  const ws = state.workspaces.find((w) => w.id === id);
  $("#workspace").value = id;
  $("#workspace-path").textContent = ws?.path || "";
  $("#workspace-path").title = ws?.path || "";
  await navigate(route?.view || "overview", route?.id || null, {history: route ? "replace" : "push"});
}
function fillWorkspaces() {
  $("#workspace").innerHTML = state.workspaces
    .map((w) => `<option value="${w.id}">${esc(w.name)}</option>`)
    .join("");
}

document.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-action],[data-view]");
  if (!target) return;
  try {
    if (target.dataset.view) {
      event.preventDefault();
      await navigate(target.dataset.view);
      return;
    }
    const a = target.dataset.action;
    if (a === "reconnect") openReconnect();
    else if (a === "guild-guide") modal("Meet the guild.", "A field guide to ORC and its companions.", ORCBrand.guide(), "guild");
    else if (a === "guild-visit") { closeModal(); await navigate(target.dataset.destination); }
    else if (a === "appearance")
      modal(
        "Make yourself at home.",
        "Ten palettes. Light, dark, or in sync with your device.",
        appearanceControls(),
        "appearance",
      );
    else if (a === "appearance-theme")
      ORCAppearance.set({ theme: target.dataset.theme });
    else if (a === "appearance-mode")
      ORCAppearance.set({ mode: target.dataset.mode });
    else if (a === "activity-latest") $(".worker-timeline")?.scrollTo({top: $(".worker-timeline").scrollHeight, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth"});
    else if (a === "close") closeModal();
    else if (a === "routing-filter") { state.routingModel = target.dataset.model || ""; state.signature = ""; await refresh(true); }
    else if (a === "routing-review") { const decision = target.dataset.id; await navigate("decisions"); openLabTab("review", {decision}); }
    else if (a === "template") target.dataset.template === "truffle" ? openHunt() : openLaunch(templates[target.dataset.template]);
    else if (a === "truffle-hunt") openHunt();
    else if (a === "truffle-open") { closeModal(); forestRoute(); await navigate("truffle", target.dataset.id || null); }
    else if (a === "forest-sync") openSurvey();
    else if (a === "forest-grade") openSurvey(true);
    else if (a === "forest-tab") forestNavigate({tab:target.dataset.tab});
    else if (a === "forest-patch") { closeModal(); forestNavigate({tab:"issues",patch:target.dataset.patch,grade:"all",query:""}); }
    else if (a === "forest-clear") forestNavigate({patch:"all",grade:"all",query:""});
    else if (a === "forest-filter") forestNavigate({tab:"issues",grade:state.forest.grade === target.dataset.grade ? "all" : target.dataset.grade});
    else if (a === "forest-issue") openForestIssue(Number(target.dataset.number));
    else if (a === "truffle-queue") openTruffleQueue();
    else if (a === "run") { closeModal(); await navigate("workflow", target.dataset.id); }
    else if (a === "stage") {
      state.node = target.dataset.node;
      workflow();
    } else if (a === "tab") {
      state.tab = target.dataset.tab;
      workflow();
    } else if (a === "copy-report")
      copy(
        state.report.outputs.find((o) => o.node_id === state.node)?.text || "",
      );
    else if (a === "export-report")
      download(state.report.workflow_id + ".md", state.report.markdown);
    else if (a === "finding") {
      const f = state.report.outputs
        .find((o) => o.node_id === state.node)
        ?.findings.find((f) => f.number === Number(target.dataset.number));
      modal(
        "Recommendation " + f.number,
        f.title,
        `<article class="markdown">${markdown(f.text)}</article>`,
      );
    } else if (a === "implement") {
      const f = state.report.outputs
        .find((o) => o.node_id === target.dataset.node)
        ?.findings.find((f) => f.number === Number(target.dataset.number));
      openLaunch({
        kind: "build",
        from_workflow: state.report.workflow_id,
        from_node: target.dataset.node,
        finding: f.number,
        title: f.title,
        text: "Keep this change scoped. Preserve existing work, add meaningful regression coverage, and run relevant checks. Do not commit, push, or deploy.",
      });
    } else if (a === "custom-from-run")
      openLaunch({ kind: "workflow", spec: withoutDerived(state.report.spec) });
    else if (a === "decision") {
      openLabTab("review", {decision:target.dataset.id});
    } else if (a === "suggest-labels") {
      const record = state.decisions.find(r => r.id === state.decision);
      const workspace = state.workspace;
      const options = labelingValues("label");
      target.disabled = true;
      try {
        await api("launch", { action: "suggest-labels", decision_id: record.id, ...options }, workspace);
        // Keep edits while the job runs; a fresh draft only replaces untouched fields.
        toast(options.approval_mode === "council" ? "Council started. Unanimous answers will be approved automatically." : "Drafting labels for your review.");
        if (workspace === state.workspace) await refresh(true);
      } finally { target.disabled = false; }
    } else if (a === "use-label-draft") {
      const record = state.decisions.find(r => r.id === state.decision);
      const draft = labelDraft(record);
      draft.dirty = false;
      draft.seen = null;
      decisions();
    } else if (a === "garden-settings") openGarden();
    else if (a === "lab-tab") {
      openLabTab(target.dataset.tab);
      $("#lab-tab-" + state.labTab)?.scrollIntoView({block:"nearest", inline:"nearest"});
    }
    else if (a === "garden-review") {
      openLabTab("review", {filter:"needs_review", decision:null});
      const editor = $(".review-form");
      if (editor) {
        editor.setAttribute("tabindex", "-1");
        editor.focus({preventScroll: true});
        editor.scrollIntoView({block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth"});
      }
    }
    else if (a === "garden-pause") {
      await api("garden", {enabled: false, agent: state.garden.agent});
      toast("Garden paused. Any current draft can finish; no new calls will start.");
      await refresh(true);
    } else if (a === "garden-filter") {
      openLabTab("review", {filter:target.dataset.filter, decision:null});
    } else if (a === "exclude-label") {
      await api("label-exclusion", {id: target.dataset.id, excluded: target.dataset.excluded === "true"});
      await refresh(true);
    } else if (a === "quality-decision") {
      openLabTab("review", {filter:"all", decision:target.dataset.id});
      $(".lab-layout > .panel:last-child")?.scrollIntoView({block: "start"});
    } else if (a === "learning-step") {
      openLearning({action: target.dataset.step, dataset: target.dataset.dataset, model_path: target.dataset.model});
    } else if (a === "probe") openProbe();
    else if (a === "learning") openLearning();
    else if (a === "training-settings") openTrainingSettings();
    else if (a === "training-toggle" || a === "training-retry") {
      await api("training-loop", {enabled:a === "training-retry" || target.dataset.enabled === "true", retry:a === "training-retry"});
      await refresh(true);
    } else if (a === "learning-round") { state.learningRound=target.dataset.id; decisions(); }
    else if (a === "orc") await loadOrc(target.dataset.command);
    else if (a === "job") await openJob(target.dataset.id);
    else if (a === "job-workflow") {
      closeModal();
      await navigate("workflow", target.dataset.id);
    } else if (a === "cancel") {
      target.disabled = true;
      await api("cancel", { id: target.dataset.id });
      toast(
        "Stop requested. Waiting for the coordinator to clean up its workers.",
      );
      await refresh();
    } else if (a === "resume") openResume();
    else if (a === "publish") openPublish();
    else if (a === "pr-refresh") {
      target.disabled = true;
      try { await api("pr-refresh", { run_id: state.report.workflow_id }); await refresh(true); }
      finally { target.disabled = false; }
    }
    else if (a === "prepared") {
      const file = await api(
        "file?path=" + encodeURIComponent(target.dataset.path),
      );
      openLaunch({ kind: "workflow", spec: JSON.parse(file.text) });
    } else if (a === "setup") {
      modal(
        "Set up Laya",
        "Install or repair the local runtime and download the English checkpoint.",
        `<p class="help-copy">Uses uv and the existing Fusion setup command. Progress is saved as a job. No coding workers start.</p><button class="button primary" data-action="setup-start">Install runtime →</button>`,
      );
    } else if (a === "setup-start") await launch({ action: "setup" });
    else if (a === "effective")
      modal(
        "Resolved Fusion settings",
        "Includes defaults and inherited configuration; credentials are masked.",
        `<pre class="console">${esc(pretty(state.config.effective))}</pre>`,
      );
    else if (a === "save-settings") {
      const isOrc = target.dataset.target === "orc";
      const value = JSON.parse(
        $(isOrc ? "#orc-config" : "#fusion-config").value,
      );
      target.disabled = true;
      try {
        state.config = await api("config", {
          target: target.dataset.target,
          value,
          revision: isOrc ? state.config.orc_revision : state.config.revision,
        });
        $("#execution-chip").textContent =
          state.config.execution_mode === "yolo"
            ? "YOLO · full access"
            : "Restricted runtime";
        settings();
        toast("Workspace settings saved. Future launches use these settings.");
      } finally {
        target.disabled = false;
      }
    }
  } catch (e) {
    if ($("#dialog").open) modalError(e.message);
    else error(e.message);
    target.disabled = false;
  }
});
document.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target,
    submit = $("[type=submit],button.button.primary", form);
  if (submit) submit.disabled = true;
  try {
    const values = Object.fromEntries(new FormData(form));
    if (form.id === "routing-filter") {
      state.routingModel = (values.model || "").trim(); state.signature = ""; await refresh(true);
    } else if (form.id === "reconnect-form") {
      await reconnectWithURL(values.url.trim());
      closeModal();
      toast("Reconnected. Your other tabs can use this connection too.");
    } else if (form.id === "truffle-survey-form") {
      const job = await api("launch", {...values, action:"truffle-survey", sync_only:!values.resume && !values.grade_now, include_assigned:!!values.include_assigned});
      closeModal(); forestRoute(); await navigate("truffle", values.resume || job.survey_id || null);
      toast("Expedition started. The map updates live; you can leave this tab.");
    } else if (form.id === "truffle-hunt-form") {
      await launch({...values, action: "truffle-hunt", include_assigned: !!values.include_assigned});
    } else if (form.id === "truffle-queue-form") {
      await launch({action: "truffle-run", scout_id: state.scout.id, issues: [...truffleSelected(state.scout)],
        attempts: values.attempts, publish: publishValues("truffle"), allow_write: !!values.allow_write});
    } else if (form.id === "launch-form") {
      const kind = values.kind;
      await launch({
        ...values,
        action: ["delegate", "workflow"].includes(kind) ? kind : "build",
        allow_write: !!values.allow_write,
        prepare: !!values.prepare,
        spec: kind === "workflow" ? JSON.parse(values.spec) : undefined,
        across: kind === "sweep" ? String(values.across || "").split(",").map((d) => d.trim()).filter(Boolean) : undefined,
        publish: ["build", "debug"].includes(kind) ? publishValues("launch") : undefined,
      });
    } else if (form.id === "publish-preview-form") {
      const p = await api("publish-preview", { run_id: form.dataset.run, publish: publishValues("pr") });
      if (p.url) { closeModal(); toast("This workflow already has a PR."); await refresh(true); }
      else showPublishPreview(p);
    } else if (form.id === "publish-form") {
      await launch({ action: "publish", run_id: form.dataset.run, snapshot_id: form.dataset.snapshot,
        title: values.title, body: values.body, draft: !!values.draft, accept_legacy_diff: !!values.accept_legacy_diff });
    } else if (form.id === "probe-form")
      await launch({ ...values, action: "probe", mode: "shadow" });
    else if (form.id === "learning-form") await launch(values);
    else if (form.id === "training-loop-form") {
      await api("training-loop", {enabled:!!values.enabled, min_new_answers:Number(values.min_new_answers)});
      closeModal(); await refresh(true); toast("Training settings saved.");
    }
    else if (form.id === "garden-form") {
      const options = labelingValues("garden");
      await api("garden", {enabled: !!values.enabled, ...options, include_existing: !!values.include_existing});
      closeModal();
      toast(values.enabled ? (options.approval_mode === "council" ? "Garden enabled. Supported council answers will be approved automatically." : "Garden enabled. New drafts will arrive for your review.") : "Garden paused.");
      await refresh(true);
    }
    else if (form.id === "resume-form") {
      const route = values.worker.startsWith("route:")
        ? values.worker.slice(6)
        : undefined;
      await launch({
        action: "resume",
        run_id: state.report.workflow_id,
        allow_write: !!values.allow_write,
        node: values.node,
        agent: route ? "auto" : values.worker || undefined,
        route,
        max_attempts: Number(values.max_attempts),
      });
    } else if (form.id === "workspace-form") {
      const ws = await api("workspaces", { path: values.path });
      if (!state.workspaces.some((w) => w.id === ws.id))
        state.workspaces.push(ws);
      fillWorkspaces();
      closeModal();
      await chooseWorkspace(ws.id);
    } else if (form.id === "label-form") {
      const record = state.decisions.find(r => r.id === form.dataset.decision);
      const draft = labelDraft(record);
      const answers = Object.fromEntries(
        Object.entries(values).filter(([, value]) => value),
      );
      await api("label", {
        id: record.id,
        answers,
        evidence: $("#label-evidence").value,
        suggestion_id: draft.suggestion_id,
        approved: true,
      });
      draft.dirty = false;
      toast("Reviewed labels saved.");
      await refresh(true);
    }
  } catch (e) {
    if ($("#dialog").open) modalError(e.message);
    else error(e.message);
  } finally {
    if (submit) submit.disabled = false;
  }
});
document.addEventListener("input", (event) => {
  if (event.target.id === "forest-search") {
    state.forest.query = event.target.value;
    const start = event.target.selectionStart, end = event.target.selectionEnd;
    writeRoute("replace");
    replaceContent($("#forest-results"), forestResults(state.scout), "forest-results:" + state.workspace + ":" + state.scout.id);
    $("#forest-search").focus(); $("#forest-search").setSelectionRange(start, end);
  }
  if (event.target.closest("#label-form")) captureLabelEdits();
  if (event.target.id === "run-search") {
    state.search = event.target.value;
    const runs = state.overview.workflows.filter(
      (r) =>
        (state.filter === "all" || r.status === state.filter) &&
        `${r.task} ${r.id} ${r.agents}`
          .toLowerCase()
          .includes(state.search.toLowerCase()),
    );
    $("#run-results").innerHTML = runs.length
      ? `<div class="panel">${runRows(runs)}</div>`
      : empty("No matching workflows.", "Try another filter.");
  }
});
document.addEventListener("change", (event) => {
  if (event.target.id === "forest-grade") forestNavigate({grade:event.target.value});
  if (event.target.id === "forest-patch") forestNavigate({patch:event.target.value});
  if (event.target.matches("[data-truffle-number]") && state.scout) {
    const selected = truffleSelected(state.scout), n = Number(event.target.dataset.truffleNumber);
    if (event.target.checked) selected.add(n); else selected.delete(n);
    if ($("#forest-basket-count")) $("#forest-basket-count").textContent = selected.size;
    $$(`[data-truffle-number="${n}"]`).forEach(el => { el.checked = selected.has(n); });
  }
  if (event.target.id.startsWith("settings-publish-")) {
    try {
      const value = JSON.parse($("#fusion-config").value);
      value.publish = publishValues("settings");
      $("#fusion-config").value = pretty(value);
    } catch { error("Correct the JSON before changing publishing defaults."); }
  }
  if (event.target.closest("#label-form")) captureLabelEdits();
  const controls = event.target.closest(".labeling-controls");
  if (controls) {
    const prefix = controls.dataset.labeling;
    if (event.target.id === prefix + "-approval" && event.target.value === "council")
      $("#" + prefix + "-mode").value = "council";
    if (event.target.id === prefix + "-mode" && event.target.value === "single")
      $("#" + prefix + "-approval").value = "human";
    const options = labelingValues(prefix);
    controls.querySelector(".single-worker").hidden = options.labeling_mode === "council";
    controls.querySelector(".council-members").hidden = options.labeling_mode !== "council";
    if (prefix === "label") {
      const draft = labelDraft(state.decisions.find(r => r.id === state.decision));
      Object.assign(draft, options, {worker: options.agent, optionsDirty:true});
      const trigger = $('[data-action="suggest-labels"]');
      if (trigger && !trigger.disabled) trigger.textContent = options.approval_mode === "council" ? "Run council & approve" : state.decisions.find(r=>r.id === state.decision)?.suggestions?.length ? "Regenerate labels" : "Suggest labels";
    }
  }
  if (event.target.id === "resume-node") {
    const node = state.report.live_nodes.find(
      (n) => n.id === event.target.value,
    );
    const attempts = $("#resume-attempts");
    attempts.min = node.attempts + 1;
    attempts.value = Math.max(Number(attempts.value), node.attempts + 1);
  }
  if (event.target.id === "run-filter") {
    state.filter = event.target.value;
    workflows();
  }
  if (
    ["setting-mode", "setting-worker", "setting-execution"].includes(
      event.target.id,
    )
  ) {
    try {
      const value = JSON.parse($("#fusion-config").value);
      if (event.target.id === "setting-mode")
        value.decisions = { ...value.decisions, mode: event.target.value };
      else if (event.target.id === "setting-execution")
        value.execution_mode = event.target.value;
      else value.sidekick = event.target.value;
      $("#fusion-config").value = pretty(value);
    } catch {
      error("Correct the JSON before changing the quick controls.");
    }
  }
});
$("#launch-top").addEventListener("click", () => openLaunch());
$("#workspace").addEventListener("change", (event) =>
  chooseWorkspace(event.target.value),
);
$("#add-workspace").addEventListener("click", () =>
  modal(
    "Add a workspace",
    "Connect an existing local repository. Its saved runs appear automatically.",
    `<form id="workspace-form"><div class="field"><label for="workspace-directory">Local directory</label><input id="workspace-directory" name="path" placeholder="~/code/visa/visa-mono" required autofocus></div><button class="button primary">Add workspace →</button></form>`,
  ),
);
$("#shortcuts").addEventListener("click", () =>
  modal(
    "Move at your speed",
    "Keyboard shortcuts",
    `<div class="shortcut-grid"><span>New run</span><kbd>N</kbd><span>Navigate sections</span><kbd>1–5</kbd><span>Search workflows</span><kbd>/</kbd><span>Close dialog</span><kbd>Esc</kbd><span>Show shortcuts</span><kbd>?</kbd></div>`,
  ),
);
$("#dialog").addEventListener("close", () => {
  if (!$("#dialog").open) state.modal = null;
});
document.addEventListener("keydown", (event) => {
  if (
    event.target.matches("input,textarea,select") ||
    event.ctrlKey ||
    event.metaKey ||
    event.altKey
  )
    return;
  if (event.key === "Enter" && event.target.matches("[role=button]"))
    event.target.click();
  if ($("#dialog").open) return;
  if (event.target.matches('[role="tab"][data-action="lab-tab"]') && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const index = labTabs.findIndex(([id]) => id === state.labTab);
    const next = event.key === "Home" ? 0 : event.key === "End" ? labTabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + labTabs.length) % labTabs.length;
    openLabTab(labTabs[next][0]);
    const tab = $("#lab-tab-" + state.labTab);
    tab.focus({preventScroll:true});
    tab.scrollIntoView({block:"nearest", inline:"nearest"});
    return;
  }
  if (event.key.toLowerCase() === "n") {
    event.preventDefault();
    openLaunch();
  }
  if ("12345".includes(event.key) && event.key.length === 1)
    navigate(
      ["overview", "workflows", "decisions", "models", "settings"][
        Number(event.key) - 1
      ],
    );
  if (event.key === "?") $("#shortcuts").click();
  if (event.key === "/") {
    event.preventDefault();
    navigate("workflows").then(() => $("#run-search")?.focus());
  }
});
async function connectWorkspace(data) {
  state.workspaces = data.workspaces;
  fillWorkspaces();
  const route = readRoute();
  const remembered = route.workspace || state.workspace || localStorage.getItem("fusion-workspace");
  const workspace = data.workspaces.some(w => w.id === remembered) ? remembered : data.default;
  if (state.workspace === workspace) {
    // Reconnecting preserves focused forms and unsaved edits.
    await refresh();
    return;
  }
  await chooseWorkspace(workspace, route);
}
let connectionAttempt = null;
async function connect() {
  if (connectionAttempt) return connectionAttempt;
  connectionAttempt = (async () => {
    try {
      await connectWorkspace(await api("bootstrap"));
      if (state.modal === "reconnect") closeModal();
    } catch (e) {
      $("#connection").textContent = state.authRequired ? "reconnect" : "offline";
      error(e.message);
      if (!state.workspace)
        mount(empty("Connect to your control room.", esc(e.message), button("Reconnect", "reconnect", "", "primary")));
    }
  })();
  try { await connectionAttempt; } finally { connectionAttempt = null; }
}
window.addEventListener("storage", async event => {
  if (event.storageArea === localStorage && event.key === credentialKey && event.newValue && event.newValue !== token) {
    token = event.newValue;
    await connect();
  }
});
window.addEventListener("hashchange", async () => {
  try {
    if (new URLSearchParams(location.hash.slice(1)).has("token")) {
      await reconnectWithURL(location.href);
      return;
    }
    if (!state.workspace) return;
    const route = readRoute();
    if (route.workspace && route.workspace !== state.workspace && state.workspaces.some(w => w.id === route.workspace)) {
      await chooseWorkspace(route.workspace, route);
    } else if (route.view === "decisions" && state.view === "decisions") {
      openLabTab(route.labTab, {decision: route.decision, filter: route.filter, history: "replace"});
    } else {
      forestRoute(route);
      state.labTab = route.labTab;
      state.decision = route.decision;
      state.gardenFilter = route.filter;
      await navigate(route.view, route.id, {history: "replace"});
    }
  } catch (e) {
    error(e.message);
  }
});
async function init() {
  await connect();
  setInterval(async () => {
    if (state.polling || document.hidden) return;
    state.polling = true;
    try {
      if (state.workspace) await refresh();
      else if (!state.authRequired) await connect();
    } finally {
      state.polling = false;
    }
  }, 2000);
}
init();
