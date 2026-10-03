/* The ORC guild: local, theme-colored symbols. No icon font or remote assets. */
"use strict";
(() => {
  const shapes = {
    orc: '<path class="sigil-fill" d="M8 11 2 7l2 12 5 3 3 6h8l3-6 5-3 2-12-6 4-2-6-6-3-6 3Z"/><path d="m10 13 4 2m8-2-4 2m-5 7h6m-6-3h6m-3-14-2 4 2 2"/><path class="sigil-tusk" d="M9 18q-3 7 4 6Zm14 0q3 7-4 6Z"/>',
    forge: '<path class="sigil-fill" d="M4 15h24l-4 6h-5v5h5v3H8v-3h5v-5H8Z"/><path d="m10 3 6 6-4 4-6-6Zm4 8 7 7M23 3v5m-2-2h4"/>',
    truffle: '<path class="sigil-fill" d="m7 11-3-6 9 3q9-2 13 6l3-1-1 7-4 5H10l-5-5q-3-1-3-4t5-3Z"/><path d="m20 10 3-5 3 7M9 25v3m12-3v3"/><ellipse cx="10" cy="19" rx="5" ry="4"/><path d="M8 18v2m4-2v2m7-6h.01"/><path class="sigil-tusk" d="m17 21 3-4q3 6-3 4Z"/>',
    mushroom: '<path class="sigil-fill" d="M3 18C3 3 29 3 29 18Z"/><path d="M13 18 11 29h10l-2-11M9 14h.01M16 10h.01M23 14h.01M6 29h20"/>',
    patch: '<path class="sigil-fill" d="m16 2 9 11h-5l8 10H4l8-10H7Z"/><path d="M16 23v7M3 29q13-4 26 0M16 9v11"/>',
    basket: '<path class="sigil-fill" d="m3 14 4 15h18l4-15Z"/><path d="m8 14 8-12 8 12M11 18l1 7m9-7-1 7M3 14h26"/>',
    rune: '<path class="sigil-fill" d="m16 2 11 14-11 14L5 16Z"/><path d="m16 7-6 9 6 9 6-9Zm0 0v18M5 16h22"/>',
    council: '<circle class="sigil-fill" cx="16" cy="7" r="4"/><circle class="sigil-fill" cx="6" cy="16" r="4"/><circle class="sigil-fill" cx="26" cy="16" r="4"/><path d="M9 27q0-8 7-8t7 8M3 23l-1 5m27-5 1 5m-18-1 3 3 6-6"/>',
    seed: '<path class="sigil-fill" d="M16 18Q3 19 4 6q13 0 12 12Zm0-5Q17 2 28 3q1 11-12 10Z"/><path d="M16 28V13M9 11l7 7m7-10-7 6M5 28q11-4 22 0"/>',
    compass: '<circle cx="16" cy="16" r="12"/><path class="sigil-fill" d="m21 10-3 9-8 3 3-9Z"/><path d="M16 1v4m0 22v4M1 16h4m22 0h4"/>',
    scroll: '<path class="sigil-fill" d="M8 4h17q5 0 5 5h-7v15q0 5-5 5H6q-4 0-4-5h13q0 5 4 5M8 4q-4 0-4 5v11"/><path d="M10 11h8m-8 5h8"/>',
    bug: '<path class="sigil-fill" d="M9 15a7 7 0 0 1 14 0v7a7 7 0 0 1-14 0Z"/><path d="M12 9V5m8 4V5M9 16 4 12m19 4 5-4M9 21H3m20 0h6M9 26l-4 4m18-4 4 4M16 15v14"/>',
    shield: '<path class="sigil-fill" d="m16 2 12 5v10q0 8-12 13Q4 25 4 17V7Z"/><path d="m10 16 4 4 8-9"/>',
    gear: '<path class="sigil-fill" d="m12 3-1 4-4 1-3-1-3 5 3 3v3l-3 3 3 5 4-1 3 1 1 4h7l1-4 4-1 3 1 3-5-3-3v-3l3-3-3-5-4 1-3-1-1-4Z"/><circle cx="15.5" cy="16" r="5"/>',
    library: '<path class="sigil-fill" d="M3 6h7v23H3Zm10-3h7v26h-7Zm10 3 5-1 4 23-5 1Z"/><path d="M4 11h5m5-3h5M4 24h5m5 0h5"/>',
    spark: '<path class="sigil-fill" d="m16 2 4 10 10 4-10 4-4 10-4-10-10-4 10-4Z"/>',
    moon: '<path class="sigil-fill" d="M25 22A13 13 0 0 1 10 3a13 13 0 1 0 15 19Z"/><path d="M24 3v6m-3-3h6"/>',
    sun: '<circle class="sigil-fill" cx="16" cy="16" r="7"/><path d="M16 1v4m0 22v4M1 16h4m22 0h4M5 5l3 3m16 16 3 3M5 27l3-3M24 8l3-3"/>',
    flag: '<path d="M6 30V3"/><path class="sigil-fill" d="M6 4q6-4 12 0t10 0v14q-4 4-10 0T6 18Z"/>',
  };
  function icon(name, extra = "") {
    return `<svg class="sigil ${extra}" viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${shapes[name] || shapes.spark}</svg>`;
  }
  const chapters = {overview:"orc", workflows:"forge", workflow:"forge", truffle:"truffle", routing:"compass", decisions:"rune", models:"library", settings:"gear"};
  function hero() {
    return `<section class="guild-hero"><div class="guild-hero-copy"><div class="eyebrow">${icon("forge")} THE ORC GUILD · YOUR AGENT WORKSPACE</div><h1>Good work.<br>Forged here.</h1><p>Your agents bring the muscle. You set the mission.<br>Explore, build and review—with the evidence in view.</p><button class="subtle guild-guide-link" data-action="guild-guide">Meet the guild ${icon("scroll")} <span aria-hidden="true">↗</span></button></div><div class="guild-hero-art" aria-hidden="true"><div class="rune-ring"></div><img src="/brand/forge-companions.png" width="1536" height="1024" alt="" decoding="async"><span class="forge-spark spark-one">✦</span><span class="forge-spark spark-two">✧</span><span class="guild-art-caption">SMALL DIFFS. STRONG EVIDENCE.</span></div></section>`;
  }
  const members = [
    ["orc", "Gruk", "The forgekeeper", "Big tusks. Careful hands. Gruk believes the best work survives a second look.", "Your control room: coordinate workers and inspect the evidence.", "overview"],
    ["forge", "Fusion", "The forge", "An idea arrives as ore. Exploration, planning, implementation and review give it shape.", "Workflows turn a request into work you can inspect and verify.", "workflows"],
    ["truffle", "Snout", "The truffle scout", "Raised behind Gruk’s forge, Snout learned that the loudest issue rarely hides the best truffle. One chipped tusk, a muddy ledger, and an unreasonable devotion to regression tests.", "Scout open issues and queue the ones supported by source evidence.", "truffle"],
    ["rune", "Laya", "The lorekeeper", "Every useful lesson gets a place in the ledger. No lesson becomes wisdom just by being written down.", "Approve labels, train candidates and measure results before choosing a model.", "decisions"],
    ["council", "The council", "Keepers of the seal", "Different eyes around one table. Their seal records agreement; the evidence stays beside it.", "Independent label assessments, visible votes and optional unanimous approval.", "decisions"],
    ["seed", "The garden", "Where lessons grow", "Snout finds the seeds. The guild tends the soil. A good garden leaves room to pull a weed.", "Draft labels continuously, inspect their quality, and correct or exclude examples.", "decisions"],
  ];
  function guide() {
    return `<div class="guild-book"><p class="guild-motto">Big tusks. Small diffs. Show your work.</p><p class="help-copy">A little lore for the tools you use every day.</p><div class="guild-cast">${members.map(([mark,name,role,story,meaning,view])=>`<article class="guild-member"><div class="guild-member-mark">${icon(mark)}</div><div class="eyebrow">${role}</div><h3>${name}</h3><p>${story}</p><div class="guild-member-function">${meaning}</div><button class="subtle" data-action="guild-visit" data-destination="${view}">Open ${view === "decisions" ? "Laya lab" : view === "truffle" ? "Truffle pig" : view} →</button></article>`).join("")}</div><div class="guild-seals" aria-label="Guild symbols">${Object.keys(shapes).map(name=>`<span title="${name}" aria-label="${name}">${icon(name)}</span>`).join("")}</div></div>`;
  }
  function truffleLore(status) {
    const working = ["scouting", "running"].includes(status);
    const stage = status === "scouting" ? 0 : status === "ready" ? 1 : ["running", "paused", "waiting", "interrupted"].includes(status) ? 2 : status === "complete" ? 3 : -1;
    const mood = status === "complete" ? "THE BASKET IS HOME" : status === "paused" || status === "interrupted" ? "A ROOT IN THE PATH" : status === "waiting" ? "AT THE FORGE" : working ? "NOSE TO THE GROUND" : "SNOUT’S FIELD NOTES";
    return `<section class="snout-field ${working ? "is-sniffing" : ""}" aria-label="Snout’s field notes"><div class="snout-portrait" aria-hidden="true">${icon("truffle")}<span class="snout-scent">· · ✦</span></div><div class="snout-copy"><div class="eyebrow">${mood}</div><h2>Small tusks. Excellent instincts.</h2><p>Snout noses through the issue forest for fixes the guild can prove. A good truffle comes with a reproduction, a small diff, and a test that catches it.</p><details data-disclosure-key="snout-field-guide"><summary>Read Snout’s field guide</summary><p>Gruk found him asleep in a basket of broken builds. Now he patrols the roots of the backlog with a brass compass and a ledger Laya insists he keep dry.</p><ul><li><strong>Follow the scent.</strong> An issue is a lead. Source evidence earns a place in the basket.</li><li><strong>Leave the ancient roots.</strong> Ambiguous scope and giant rewrites stay in the forest, with a reason recorded.</li><li><strong>Bring proof to the forge.</strong> Every chosen fix gets its own worktree, checks, and independent review.</li><li><strong>Count the harvest honestly.</strong> A shortlist is a possibility. An accepted fix is work done. A published PR is ready for its next review.</li></ul><p class="snout-motto">“If you can’t reproduce the scent, don’t dig.” — Snout</p></details></div><ol class="snout-trail" aria-label="Hunt journey">${[["compass","Sniff","Inspect open issues"],["truffle","Shortlist","Choose proven leads"],["forge","Forge","Implement & review"],["flag","Harvest","Accepted fixes & PRs"]].map(([mark,title,description],i)=>`<li ${i === stage ? 'aria-current="step"' : ""}>${icon(mark)}<span><strong>${title}</strong><small>${description}</small></span></li>`).join("")}</ol></section>`;
  }
  window.ORCBrand = {icon, chapters, hero, guide, truffleLore};
  document.querySelectorAll("[data-brand-icon]").forEach(el => { el.innerHTML = icon(el.dataset.brandIcon); });
  function favicon() {
    const palette = getComputedStyle(document.documentElement);
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 36"><style>svg{color:${palette.getPropertyValue("--lime")}}.sigil-fill{fill:${palette.getPropertyValue("--panel2")}}.sigil-tusk{fill:${palette.getPropertyValue("--lime")}}</style><rect width="36" height="36" rx="9" fill="${palette.getPropertyValue("--bg")}"/><g transform="translate(2 2)" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${shapes.orc}</g></svg>`;
    document.querySelector('link[rel="icon"]').href = "data:image/svg+xml," + encodeURIComponent(svg);
  }
  document.addEventListener("orc-appearance-change", favicon);
  favicon();
})();
