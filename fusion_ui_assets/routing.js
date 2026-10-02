"use strict";
function routingPercent(value) {
  return value == null ? "—" : Math.round(value * 100) + "%";
}
function routingMoney(value) {
  return value == null ? "—" : "$" + Number(value).toFixed(2);
}
function routingOutcome(outcome) {
  if (!outcome) return `<span class="status pending">no outcome yet</span>`;
  const text = (outcome.accepted ? "accepted" : "rejected") + (outcome.stage ? " · " + outcome.stage : "") + " · " + outcome.source;
  return `<span class="status ${outcome.accepted ? "success" : "failed"}" title="${esc(outcome.reason || "")}">${esc(text)}</span>`;
}
function routingLabels(labels) {
  if (!labels.length) return `<span class="help-copy">no decisions recorded</span>`;
  return labels.map((l) => {
    const answers = l.labeled ? Object.entries(l.answers || {}).map(([k, v]) => `${k}=${v}`).join(" ") : "unlabeled";
    return `<button class="button small subtle" data-action="routing-review" data-id="${esc(l.decision_id)}" title="Review or overrule this ${esc(l.kind || "decision")} label">${esc(l.kind || "decision")}: ${esc(answers)}${l.labeled ? " · " + esc(l.source || "") : ""}</button>`;
  }).join(" ");
}
function routingRow(row) {
  const p = row.posterior;
  const posterior = p ? `<span title="pooled local evidence across ${esc((p.lanes || []).join(", "))}">posterior ${p.successes}/${p.attempts} · ${routingPercent(p.p_win)} chance best · ${routingMoney(p.cost_per_accepted)}/accepted</span>` : "";
  const how = row.write_trial ? "write trial" : row.explored ? "explored" : p ? "sampled" : "ranked";
  return `<div class="run-row routing-row"><div><div class="run-title">${esc(row.model || row.chosen)}${row.reasoning_effort ? " · " + esc(row.reasoning_effort) : ""} <small class="mono">${esc(row.chosen)}</small></div>` +
    `<div class="run-meta"><span>${esc(row.role || "—")}${row.write ? " · writes" : ""}</span><span>${date(row.time_ms)}</span><span>${how} · propensity ${routingPercent(row.propensity)} of ${row.candidates}</span>${posterior}<span>${routingMoney(row.cost_usd)}</span><button class="subtle mono" data-action="file" data-path="${esc(row.result)}" title="Open ${esc(row.result)}">${esc(row.run)}</button></div>` +
    `<div class="run-meta">${routingLabels(row.labels)}</div></div><div class="run-side">${routingOutcome(row.outcome)}${row.status ? badge(row.status) : ""}</div></div>`;
}
function routingView() {
  const data = state.routing || {rows: [], models: {}, total: 0};
  const filter = state.routingModel || "";
  const models = Object.entries(data.models || {});
  const summary = models.length ? `<div class="panel"><div class="panel-body routing-models">${models.map(([name, m]) => {
    const decided = m.accepted + m.rejected;
    return `<button class="button small ${filter === name ? "primary" : ""}" data-action="routing-filter" data-model="${esc(filter === name ? "" : name)}" aria-pressed="${filter === name}">${esc(name)} · ${m.picks} picks · ${m.accepted}/${decided} accepted${m.pending ? " · " + m.pending + " pending" : ""} · ${routingMoney(m.cost_usd)}</button>`;
  }).join(" ")}</div></div>` : "";
  mount(intro("ROUTING", "Which model took each task, and how it went.",
      "Every automatic routing decision with its chosen model, its odds, its cost and its outcome. Labels open in the Laya lab, where you can review or overrule them.") +
    `<form id="routing-filter" class="routing-filter"><div class="field"><label for="routing-model">Model or lane contains</label><input id="routing-model" name="model" value="${esc(filter)}" autocomplete="off" spellcheck="false" placeholder="e.g. fable"></div><button type="submit" class="button small">Filter</button>${filter ? button("Clear", "routing-filter", 'data-model=""', "small subtle") : ""}</form>` +
    summary +
    `<div class="section-bar"><h2>Routed tasks <small>${data.rows.length} of ${data.total}</small></h2></div>` +
    (data.rows.length ? `<div class="panel">${data.rows.map(routingRow).join("")}</div>` :
      empty("No routed tasks yet.", filter ? "Nothing matches this filter." : "Automatic routing decisions appear here once work is dispatched.")));
}
