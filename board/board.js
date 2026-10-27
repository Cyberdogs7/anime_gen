const STATES = ["inbox", "planned", "in_work", "agent_verified", "reviewing", "retake", "blocked", "deferred", "accepted"];
const STATE_LABEL = {inbox:"Inbox", planned:"Planned", in_work:"Running", agent_verified:"Verified",
  reviewing:"Review", retake:"Retake", blocked:"Blocked", deferred:"Deferred", accepted:"Accepted"};
const S = {board:null, page:"board", query:"", state:"", showFilter:"", shows:null, modal:false, sse:false, sseOpen:false, boardFresh:false, providerFilter:""};
let EVENTS = null;

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function api(path, opts = {}) {
  const r = await fetch(path, {headers:{"Content-Type":"application/json"}, ...opts});
  const j = await r.json().catch(() => ({error:`HTTP ${r.status}`}));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}
const post = (path, body) => api(path, {method:"POST", body:JSON.stringify(body || {})});

function toast(msg, err = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (err ? " err" : "");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => el.classList.add("hidden"), 3600);
}
function fmtDate(v) {
  if (!v) return "—";
  const d = typeof v === "number" ? new Date(v * 1000) : new Date(v);
  return isNaN(d) ? String(v) : d.toLocaleString();
}
function dur(run) {
  const end = run.ended || (run.state === "running" ? Date.now() / 1000 : null);
  if (!end || !run.started) return "—";
  const s = Math.max(0, Math.round(end - run.started));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}
function stateBadge(s) {
  const cls = s === "accepted" || s === "agent_verified" ? "ok" : s === "retake" || s === "blocked" ? "bad" : s === "reviewing" ? "warn" : "";
  return `<span class="badge ${cls}">${esc(STATE_LABEL[s] || s)}</span>`;
}
function tags(c) {
  return `<div class="meta"><span class="tag role">${esc(c.role || "—")}</span>
    <span class="tag">${esc(c.lane)}</span>
    ${c.attempts ? `<span class="tag">try ${c.attempts}</span>` : ""}
    ${c.artifacts?.length ? `<span class="tag proof">${c.artifacts.length} proof</span>` : ""}
    ${c.depends_on?.length ? `<span class="tag">${c.depends_on.length} dep</span>` : ""}</div>`;
}

function cardActions(c, detail = false) {
  const s = c.state, out = [];
  const sm = detail ? "" : " sm";
  if (c.reviewer === "human" && (s === "reviewing" || s === "agent_verified")) {
    out.push(`<button class="btn${sm} primary" data-act="approve" data-id="${esc(c.id)}">Approve</button>`);
    out.push(`<button class="btn${sm} danger" data-act="reject" data-id="${esc(c.id)}">Reject</button>`);
  }
  if (c.kind === "outline" && !["in_work", "reviewing", "accepted"].includes(s)) {
    out.push(`<button class="btn${sm}" data-act="review" data-id="${esc(c.id)}">Re-review</button>`);
  }
  if (s === "retake") out.push(`<button class="btn${sm}" data-act="advance" data-to="planned" data-id="${esc(c.id)}">Retry</button>`);
  if (s === "blocked") out.push(`<button class="btn${sm}" data-act="advance" data-to="planned" data-id="${esc(c.id)}">Unblock</button>`);
  if (s === "planned" || s === "inbox") out.push(`<button class="btn${sm}" data-act="advance" data-to="deferred" data-id="${esc(c.id)}">Defer</button>`);
  if (s === "deferred") out.push(`<button class="btn${sm}" data-act="advance" data-to="planned" data-id="${esc(c.id)}">Resume</button>`);
  if (detail) out.push(`<button class="btn${sm}" data-act="back">Back to board</button>`);
  out.push(`<button class="btn${sm} danger" data-act="delete" data-id="${esc(c.id)}">Delete</button>`);
  return out.join("");
}

const KIND_TITLE = {
  outline: "Outline", outline_skeleton: "Outline skeleton",
  outline_beats: "Outline beats", outline_scene: "Scene rewrite",
  script: "Screenplay", objects: "Object references",
  keyframes: "Keyframes", render: "Shot renders", qc: "Quality check",
  assemble: "Episode assembly",
  gate0_plan: "Show setup", gate0_images: "Reference art",
  gate0_voices: "Voice casting", voice_text: "Voice script", fix: "QC fix",
  image_retake: "Image retake",
  storyboard: "Storyboard", render_shot: "Shot render", shot: "Shot",
};

function showTitle(showId) {
  const s = (S.shows || []).find(x => x.id === showId);
  return s?.title || showId || "?";
}

function cardTitle(c) {
  const kind = c.kind || "";
  const char = (c.payload || {}).char || "";
  const ep = c.episode ? `EP${String(c.episode).padStart(2, "0")}` : "Gate 0";
  if (kind === "voice_text") return `Write voice sample: ${char || "character"}`;
  if (kind === "gate0_voices") return char ? `Record voice sample: ${char}` : "Record voice samples for the cast";
  if (kind === "gate0_images") return "Draw character & location references";
  if (kind === "gate0_plan") return "Build the show bible, cast & scenes";
  if (kind === "image_retake") {
    const a = (c.payload || {}).asset || {};
    if (a.type === "scene") return `Redraw location reference: ${a.id || "scene"}`;
    return `Redraw character reference: ${a.id || "character"} (${a.label || "ref"})`;
  }
  if (kind === "outline_scene") {
    return `Rewrite scene ${(c.payload || {}).scene_id || ""}`;
  }
  if (kind === "render_shot" || kind === "shot") {
    const sid = (c.payload || {}).shot_id || "shot";
    return `Render ${ep} shot ${sid}`;
  }
  if (kind === "fix") return `Fix ${ep} QC failures`;
  if (kind === "render") return `Render every ${ep} shot`;
  if (kind === "qc") return `Review ${ep} renders`;
  if (kind === "assemble") return `Assemble ${ep}`;
  if (kind === "outline") return `Write ${ep} outline`;
  if (kind === "script") return `Write ${ep} screenplay`;
  if (kind === "objects") return `Plan ${ep} object references`;
  if (kind === "keyframes") return `Draw ${ep} keyframes`;
  if (kind === "storyboard") return `Build ${ep} storyboard`;
  return c.title || `${ep}: ${KIND_TITLE[kind] || kind}`;
}

function cardCategory(c) {
  const kind = c.kind || "";
  if (["outline", "outline_skeleton", "outline_beats", "outline_scene",
       "script", "objects", "gate0_plan", "voice_text", "fix"].includes(kind)) return "write";
  if (["storyboard", "keyframes", "gate0_images", "image_retake"].includes(kind)) return "image";
  if (["gate0_voices"].includes(kind) || c.lane === "audio") return "audio";
  if (["render", "render_shot", "shot"].includes(kind)) return "video";
  if (["qc"].includes(kind) || c.lane === "vision") return "review";
  if (["assemble", "delivery"].includes(kind) || c.lane === "io") return "io";
  return "other";
}

function cardHTML(c) {
  const cat = cardCategory(c);
  const ep = c.episode ? `EP${String(c.episode).padStart(2, "0")}` : "Gate 0";
  const rv = reviewGrades(c);
  const blockingFails = rv.filter(r => REVIEW_BLOCKING.includes(r.name)
    && reviewVerdict(r) === "fail");
  const reviewLine = blockingFails.length
    ? `<div class="card-review">Review: ${esc(blockingFails.map(r => reviewerName(r.name)).join(", "))}</div>`
    : "";
  return `<article class="card kind-${cat}" data-id="${esc(c.id)}">
    <div class="kicker">${esc(showTitle(c.show_id))} · ${ep} · ${esc(KIND_TITLE[c.kind] || c.kind || "")}</div>
    <h3>${esc(cardTitle(c))}</h3>
    <div class="meta"><span class="tag">${esc(c.id)}</span>${c.reviewer === "human" ? '<span class="tag role">human</span>' : ""}</div>
    ${tags(c)}
    ${reviewLine}
    <div class="row">${cardActions(c)}</div>
  </article>`;
}

function renderBoard(el) {
  const b = S.board;
  const cards = (b?.cards || []).filter(c => {
    if (S.showFilter && c.show_id !== S.showFilter) return false;
    if (S.state && c.state !== S.state) return false;
    if (!S.query) return true;
    const hay = `${c.id} ${c.title} ${c.role} ${c.lane} ${c.kind}`.toLowerCase();
    return hay.includes(S.query.toLowerCase());
  });
  const lanes = b?.lanes || {};
  const running = (b?.runs || []).filter(r => r.state === "running");
  el.innerHTML = `
    <div class="page-head">
      <div><h1>Board</h1><p>Cards move only through the daemon. Human cards close here; agents never accept their own work.</p></div>
      <div class="actions">
        <button class="btn primary" id="new-show">New show</button>
        <button class="btn" id="queue-ep">Queue episode</button>
      </div>
    </div>
    <div class="lane-summary">${Object.entries(lanes).map(([k,v]) => {
      const n = running.filter(r => r.lane === k).length;
      return `<span class="lane"><b>${esc(k)}</b> ${n}/${v.capacity}</span>`;
    }).join("")}
      <span class="lane"><b>${running.length}</b> running</span>
      <span class="lane"><b>${(b?.cards || []).length}</b> cards</span>
    </div>
    <div class="toolbar">
      <input type="search" id="search" placeholder="Search id, title, role, lane" value="${esc(S.query)}">
      <select id="show-filter"><option value="">All shows</option>
        ${(S.shows || []).map(s => `<option value="${esc(s.id)}" ${S.showFilter === s.id ? "selected" : ""}>${esc(s.title || s.id)}</option>`).join("")}
      </select>
      <div class="chips">
        <button class="chip ${S.state === "" ? "on" : ""}" data-state="">All</button>
        ${STATES.map(s => `<button class="chip ${S.state === s ? "on" : ""}" data-state="${s}">${esc(STATE_LABEL[s])}</button>`).join("")}
      </div>
      ${(S.state || S.query || S.showFilter) ? '<button class="btn sm" id="clear-filters">Clear filters</button>' : ""}
    </div>
    ${(b?.cards || []).length === 0 ? `<div class="callout"><strong>No cards queued.</strong>
      <p>Open Shows and queue an episode, or re-run Gate 0 for an existing show.</p>
      <div class="actions" style="margin-top:8px"><button class="btn" id="empty-shows">Open Shows</button></div></div>` : ""}
    <div class="board">${STATES.map(s => {
      const cs = cards.filter(c => c.state === s);
      return `<section class="col" data-state="${s}"><h2><span class="state-dot state-${s}"></span>${esc(STATE_LABEL[s])}<small>${cs.length}</small></h2>
        <div class="col-body">${cs.map(cardHTML).join("") || '<div class="empty">Empty</div>'}</div></section>`;
    }).join("")}</div>`;

  $("#search").addEventListener("input", e => { S.query = e.target.value; renderBoard(el); });
  $("#show-filter").addEventListener("change", e => { S.showFilter = e.target.value; renderBoard(el); });
  $("#empty-shows")?.addEventListener("click", () => location.hash = "#/shows");
  $("#clear-filters")?.addEventListener("click", () => {
    S.state = ""; S.query = ""; S.showFilter = ""; renderBoard(el);
  });
  el.querySelectorAll(".chips [data-state]").forEach(bn => bn.addEventListener("click", () => {
    S.state = bn.dataset.state; renderBoard(el);
  }));
  $("#new-show").addEventListener("click", newShowModal);
  $("#queue-ep").addEventListener("click", queueEpisodeModal);
}

function voiceRowsHTML(c) {
  const wavs = (c.artifacts || []).filter(a => /_voice\.wav$/i.test(a.path || ""));
  if (!wavs.length) return "";
  const chars = c.result?.characters || [];
  const rows = wavs.map(a => {
    const name = String(a.caption || "").replace(/\s+voice sample$/i, "");
    const ch = chars.find(x => String(x.name || "") === name);
    const artStatus = String(a.status || "");
    const status = artStatus || ch?.voice || "pending";
    const url = "/api/artifact?path=" + encodeURIComponent(a.path);
    let action = `<button class="btn sm danger" data-reject-voice="${esc(a.path)}" data-card="${esc(c.id)}">Reject voice</button>`;
    if (artStatus === "rejected" || artStatus === "pending") {
      action = a.retake_id
        ? `<a class="btn sm" href="#/card/${encodeURIComponent(a.retake_id)}">${artStatus === "pending" ? "Review retake" : "Retake queued"}</a>`
        : "";
    }
    const cls = status === "approved" || status === "accepted" ? "ok"
      : status === "rejected" ? "bad" : "warn";
    return `<tr><td>${esc(name)}</td><td><span class="badge ${cls}">${esc(status)}</span></td>
      <td><audio controls preload="none" src="${url}"></audio></td><td>${action}</td></tr>`;
  }).join("");
  return `<div class="panel"><h2>Voice samples</h2>
    <table><thead><tr><th>Character</th><th>Status</th><th>Sample</th><th></th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function failureText(reason) {
  const r = String(reason || "").toLowerCase();
  if (!r) return "The last run stopped without recording a reason.";
  if (r.includes("opencode exited")) {
    return "The worker process exited before reporting a verdict, usually after a tool timeout. Retry; if it repeats, check the model and the run log.";
  }
  if (r.includes("timed out") || r.includes("timeout")) {
    return "The worker timed out before finishing. Retry; if it repeats, check the model and GPU load.";
  }
  if (r.includes("dict") && r.includes("lower")) {
    return "A worker bug crashed while iterating the cast. The code path has been fixed; retry the step.";
  }
  if (r.includes("worker lost")) {
    return "The board restarted while this step was running, so its worker disappeared. The card was released; retry it.";
  }
  if (r.includes("no krea2") || r.includes("not configured") || r.includes("unavailable")) {
    return "The service this step needs is not configured or unavailable. Check the service URL in config, then retry.";
  }
  if (r.includes("permission") || r.includes("not allowed")) {
    return "The worker was not allowed to call a required tool. Check the role allowlist in config/agents.yaml.";
  }
  return "The last run stopped: " + reason;
}

const REVIEW_BLOCKING = ["structure", "continuity", "fan_service"];
const REVIEW_LABELS = {
  structure: "Structure", continuity: "Continuity", fan_service: "Fan service",
  slop: "Slop", pacing: "Pacing", stakes: "Stakes", visual: "Visual",
  agency: "Agency", serial: "Serial", repetition: "Repetition",
  voice: "Voice", exposition: "Exposition", growth: "Growth",
};

function reviewerName(name) {
  return REVIEW_LABELS[name] || String(name || "").replace(/_/g, " ");
}

function reviewGrades(card) {
  const v = card?.result?.value;
  if (!v || typeof v !== "object") return [];
  const structure = v.structure && typeof v.structure === "object" ? v.structure : {};
  const room = v.room && typeof v.room === "object" ? v.room
    : (v.reviews && typeof v.reviews === "object" ? v.reviews : {});
  return Object.entries({ ...structure, ...room })
    .filter(([, r]) => r && typeof r === "object"
      && (r.grade !== undefined || r.pass !== undefined))
    .map(([name, r]) => ({ name, ...r }));
}

function reviewVerdict(row) {
  return (row.pass === true || String(row.grade || "").toLowerCase() === "pass")
    ? "pass" : "fail";
}

function reviewTableHTML(rows) {
  if (!rows.length) return "";
  const blocking = rows.filter(r => REVIEW_BLOCKING.includes(r.name));
  const advisory = rows.filter(r => !REVIEW_BLOCKING.includes(r.name));
  const row = r => {
    const ok = reviewVerdict(r) === "pass";
    return `<tr>
      <td><strong>${esc(reviewerName(r.name))}</strong></td>
      <td><span class="badge ${ok ? "ok" : "bad"}">${ok ? "pass" : "fail"}</span></td>
      <td class="review-text">${esc(r.review || "") || '<span class="muted">No note.</span>'}</td>
    </tr>`;
  };
  return `<table class="review-table">
    <thead><tr><th>Reviewer</th><th>Verdict</th><th>What it found</th></tr></thead>
    <tbody>
      ${blocking.map(row).join("")}
      ${advisory.length ? `<tr class="review-divider"><td colspan="3">Advisory</td></tr>${advisory.map(row).join("")}` : ""}
    </tbody>
  </table>`;
}

function reviewSummaryHTML(card) {
  const rows = reviewGrades(card);
  if (!rows.length) return "";
  const v = card?.result?.value || {};
  const failed = rows.filter(r => reviewVerdict(r) === "fail");
  const blockingFailed = rows.filter(r => REVIEW_BLOCKING.includes(r.name)
    && reviewVerdict(r) === "fail");
  let title, cls;
  if (v.pass === true) {
    title = "Review passed"; cls = "ok";
  } else if (v.pass === false || failed.length) {
    const names = (blockingFailed.length ? blockingFailed : failed)
      .map(r => reviewerName(r.name));
    title = `Review failed${names.length ? ": " + names.join(", ") : ""}`;
    cls = "bad";
  } else {
    title = "Review"; cls = "warn";
  }
  return `<div class="review">
    <div class="review-head">
      <span class="badge ${cls}">${esc(title)}</span>
      <span class="muted">${rows.length} reviewer${rows.length === 1 ? "" : "s"} · ${failed.length} failed</span>
    </div>
    ${reviewTableHTML(rows)}
  </div>`;
}

function workSummaryText(card) {
  const v = card?.result?.value;
  if (!v || typeof v !== "object") return "";
  const parts = [];
  if (typeof v.scenes === "number") {
    parts.push(`${v.scenes} scene${v.scenes === 1 ? "" : "s"}`);
  }
  if (typeof v.rounds === "number") parts.push(`review round ${v.rounds}`);
  if (v.path) parts.push(String(v.path).split(/[\\/]/).pop());
  if (typeof v.plot === "string" && v.plot.trim()) {
    parts.push(v.plot.trim().slice(0, 110));
  }
  const rows = reviewGrades(card);
  if (rows.length) {
    const failed = rows.filter(r => reviewVerdict(r) === "fail");
    const blockingFailed = failed.filter(r => REVIEW_BLOCKING.includes(r.name));
    const names = (blockingFailed.length ? blockingFailed : failed)
      .slice(0, 3).map(r => reviewerName(r.name));
    parts.push(names.length ? `failed: ${names.join(", ")}` : "passed");
  }
  return parts.join(" · ");
}

function stageLabel(id, base) {
  const rest = id.slice(base.length).replace(/^\./, "");
  if (!rest) return "merge + review";
  if (rest.startsWith("beats.")) return "beats " + rest.split(".").pop();
  return rest;
}

function stageOrder(id, base) {
  const rest = id.slice(base.length).replace(/^\./, "");
  if (!rest) return 1000;
  if (rest === "skeleton") return 100;
  if (rest.startsWith("beats.")) return 500 + (parseInt(rest.split(".").pop(), 10) || 0);
  return 900;
}

// Artifact paths written by older staged tools are show-relative ("runs/EP01/...")
// while newer ones are repo-relative; normalize both to a show-file path.
function showRelPath(path, showId) {
  let p = String(path || "").replace(/\\/g, "/");
  if (!p || !showId) return "";
  const prefix = `shows/${showId}/`;
  if (p.startsWith(prefix)) return p.slice(prefix.length);
  const marker = `/${showId}/`;
  const i = p.indexOf(marker);
  if (i >= 0) return p.slice(i + marker.length);
  return p;
}

function stageFileLink(card, showId) {
  let p = card?.result?.value?.path
    || (card?.artifacts || [])[0]?.path
    || "";
  if (!p && card?.kind === "outline" && card?.episode) {
    p = `runs/EP${String(card.episode).padStart(2, "0")}/plan.json`;
  }
  const rel = showRelPath(p, showId);
  if (!rel) return "";
  const href = `/api/artifact?path=${encodeURIComponent(`shows/${showId}/${rel}`)}`;
  return `<a class="btn sm" href="${href}" target="_blank">open</a>`;
}

async function skeletonPanelHTML(card, cards) {
  if (!card?.show_id || card.kind === "outline_scene") return "";
  const base = String(card.id || "").split(".").slice(0, 2).join(".");
  const sk = card.kind === "outline_skeleton"
    ? card : (cards || []).find(x => x.id === base + ".skeleton");
  if (!sk) return "";
  const p = sk.result?.value?.path || (sk.artifacts || [])[0]?.path || "";
  const rel = showRelPath(p, card.show_id);
  if (!rel) return "";
  const href = `/api/artifact?path=${encodeURIComponent(`shows/${card.show_id}/${rel}`)}`;
  let data;
  try {
    const res = await fetch(href);
    if (!res.ok) return "";
    data = JSON.parse(await res.text());
  } catch (_) {
    return "";
  }
  const scenes = Array.isArray(data?.scenes) ? data.scenes : [];
  if (!scenes.length) return "";
  const head = [data.title, data.episode_type, data.plot]
    .filter(Boolean).map(x => esc(String(x).slice(0, 240))).join(" · ");
  const chars = Array.isArray(data.characters) && data.characters.length
    ? `<div class="muted" style="margin:6px 0">Characters: ${esc(data.characters.join(", "))}</div>`
    : "";
  const rows = scenes.map(s => {
    const cast = Array.isArray(s.characters) && s.characters.length
      ? `<div class="muted" style="font-size:11px">${esc(s.characters.join(", "))}</div>` : "";
    return `<tr>
      <td><code>${esc(s.id || "")}</code></td>
      <td>${esc(s.location_id || s.location || "")}</td>
      <td>${esc(s.time_of_day || "")}</td>
      <td class="review-text">${esc(s.summary || s.description || "")}${cast}</td>
    </tr>`;
  }).join("");
  return `<div class="panel" style="margin-bottom:16px">
    <h2>Skeleton <span class="muted" style="font-weight:400;font-size:12px">· ${scenes.length} one-line scenes</span></h2>
    ${head ? `<div class="muted" style="margin:-4px 0 8px">${head}</div>` : ""}
    ${chars}
    <table class="review-table">
      <thead><tr><th>Scene</th><th>Location</th><th>Time</th><th>Summary</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="actions" style="margin-top:8px"><a class="btn sm" href="${href}" target="_blank">Open raw JSON</a></div>
  </div>`;
}

async function scenesPanelHTML(card, cards) {
  if (!card?.show_id) return "";
  const isRetake = card.kind === "outline_scene";
  if (!isRetake && card.kind !== "outline") return "";
  const art = (card.artifacts || []).find(a => /(^|\/)plan\.json$/.test(String(a.path || "")));
  let p = art?.path || "";
  if (!p && card.episode) {
    p = `runs/EP${String(card.episode).padStart(2, "0")}/plan.json`;
  }
  const rel = showRelPath(p, card.show_id);
  if (!rel) return "";
  const href = `/api/artifact?path=${encodeURIComponent(`shows/${card.show_id}/${rel}`)}`;
  let plan;
  try {
    const res = await fetch(href);
    if (!res.ok) return "";
    plan = JSON.parse(await res.text());
  } catch (_) {
    return "";
  }
  const allScenes = Array.isArray(plan?.scenes) ? plan.scenes : [];
  if (!allScenes.length) return "";
  const payload = card.payload || {};
  const target = String(payload.scene_id || card.id.split(".retake.scene.")[1] || "")
    .split(".")[0];
  const scenes = isRetake
    ? allScenes.filter(s => String(s.id) === target) : allScenes;
  if (!scenes.length) return "";
  const parent = isRetake ? String(payload.parent || "") : card.id;
  const head = [plan.title, plan.episode_type, plan.status]
    .filter(Boolean).map(x => esc(String(x))).join(" · ");
  const plot = plan.plot
    ? `<div class="muted text-block" style="font-size:12px;margin:2px 0 8px">${esc(String(plan.plot).slice(0, 320))}</div>`
    : "";
  const sceneHTML = scenes.map(s => {
    const beats = Array.isArray(s.beats) ? s.beats : [];
    const beatRows = beats.map(b => {
      const m = String(b).match(/^(Setup|Change|Consequence)\s*:\s*([\s\S]*)$/i);
      return `<li>${m ? `<span class="beat-label">${esc(m[1])}</span>${esc(m[2])}` : esc(b)}</li>`;
    }).join("");
    const cast = Array.isArray(s.characters) && s.characters.length
      ? `<div class="scene-cast">${esc(s.characters.join(", "))}</div>` : "";
    const loc = s.location || s.location_id || "";
    const reject = `<button class="btn sm danger" data-reject-scene="${esc(s.id)}" data-card="${esc(parent)}" data-summary="${esc(s.summary || "")}">${isRetake ? "Reject again" : "Reject scene"}</button>`;
    return `<div class="scene">
      <div class="scene-head"><code>${esc(s.id || "")}</code>
        <span class="scene-loc">${esc(loc)}</span>
        <span class="scene-time">${esc(s.time_of_day || "")}</span>
      </div>
      ${cast}
      ${s.summary ? `<div class="text-block">${esc(s.summary)}</div>` : ""}
      ${beatRows ? `<ol class="beats">${beatRows}</ol>` : ""}
      <div class="actions scene-actions">${reject}</div>
    </div>`;
  }).join("");
  const retakes = !isRetake
    ? (cards || []).filter(x => x.kind === "outline_scene"
        && (x.payload || {}).parent === card.id)
    : [];
  const rewrites = retakes.length
    ? `<div class="muted" style="margin-top:8px;font-size:11px">Scene rewrites: ${retakes.map(x => {
        const sid = (x.payload || {}).scene_id || "";
        return `<a href="#/card/${encodeURIComponent(x.id)}">${esc(sid || x.id)}</a> (${esc(x.state)})`;
      }).join(" · ")}</div>`
    : "";
  const back = isRetake && parent
    ? `<div class="actions" style="margin-top:8px"><a class="btn sm" href="#/card/${encodeURIComponent(parent)}">Back to outline</a></div>`
    : "";
  return `<div class="panel" style="margin-bottom:16px">
    <h2>${isRetake ? `Rewritten scene ${esc(target)}` : "Scenes"} <span class="muted" style="font-weight:400;font-size:12px">· ${scenes.length} ${isRetake ? "scene" : "assembled"}</span></h2>
    ${head ? `<div class="muted" style="margin:-4px 0 6px">${head}</div>` : ""}
    ${plot}
    ${sceneHTML}
    ${rewrites}
    ${back}
    <div class="actions" style="margin-top:8px"><a class="btn sm" href="${href}" target="_blank">Open raw JSON</a></div>
  </div>`;
}

async function planContextHTML(card, cards) {
  if (!card?.show_id || card.kind !== "outline") return "";
  const art = (card.artifacts || []).find(a => /(^|\/)plan\.json$/.test(String(a.path || "")));
  let p = art?.path || "";
  if (!p && card.episode) {
    p = `runs/EP${String(card.episode).padStart(2, "0")}/plan.json`;
  }
  const rel = showRelPath(p, card.show_id);
  if (!rel) return "";
  const href = `/api/artifact?path=${encodeURIComponent(`shows/${card.show_id}/${rel}`)}`;
  let plan;
  try {
    const res = await fetch(href);
    if (!res.ok) return "";
    plan = JSON.parse(await res.text());
  } catch (_) {
    return "";
  }
  const season = plan._season?.entry || {};
  const dev = plan._plan_dev || {};
  const plot = plan.plotline_updates || {};
  const chips = arr => (Array.isArray(arr) ? arr : [])
    .map(x => `<span class="tag">${esc(String(x))}</span>`).join(" ");
  const rows = [];
  if (plan.synopsis) rows.push(["Synopsis", `<div class="text-block">${esc(plan.synopsis)}</div>`]);
  if (plan.threat_of_the_week) rows.push(["Threat of the week", esc(String(plan.threat_of_the_week))]);
  if (Array.isArray(plan.characters) && plan.characters.length) {
    rows.push(["Cast", esc(plan.characters.join(", "))]);
  }
  if (Array.isArray(plan.objects) && plan.objects.length) {
    rows.push(["Objects", chips(plan.objects)]);
  }
  if (plot.active_plotline_progress) {
    rows.push(["Active plotline", esc(plot.active_plotline_progress)]);
  }
  if (plot.dormant_plotline_beat) {
    rows.push(["Dormant plotline", esc(plot.dormant_plotline_beat)]);
  }
  if (dev.featured?.length) rows.push(["Featured", chips(dev.featured)]);
  if (dev.new_plotline?.name) {
    rows.push(["New plotline", `${esc(dev.new_plotline.name)} — ${esc(dev.new_plotline.summary || "")}`]);
  }
  if (season.logline) rows.push(["Season logline", esc(season.logline)]);
  if (season.character_arc) rows.push(["Character arc", esc(season.character_arc)]);
  if (season.cliffhanger) rows.push(["Season cliffhanger", esc(season.cliffhanger)]);
  if (season.featured_plotlines?.length) {
    rows.push(["Season plotlines", chips(season.featured_plotlines)]);
  }
  const keyBeats = Array.isArray(season.key_beats) ? season.key_beats : [];
  const beats = keyBeats.length
    ? `<h3>Season key beats</h3><ol class="beats">${keyBeats.map(b => `<li>${esc(b)}</li>`).join("")}</ol>`
    : "";
  const epx = `ep${String(card.episode || 0).padStart(2, "0")}.script`;
  const scriptId = card.show_id ? `${card.show_id}.${epx}` : epx;
  const script = (cards || []).find(x => x.id === scriptId);
  const scriptState = script?.state || "not queued";
  const detailNote = `<h3>Detailed scenes</h3>
    <div class="muted" style="font-size:12px">
      Not generated yet. Dialogue, shot lists and camera/continuity treatments are created by the
      screenplay stage (${script
        ? `<a href="#/card/${encodeURIComponent(script.id)}">${esc(script.id)}</a> — ${esc(scriptState)}`
        : `<code>${esc(scriptId)}</code>`}), which waits for the outline to be accepted.
    </div>`;
  if (!rows.length && !beats) return "";
  return `<details class="panel" style="margin-bottom:16px">
    <summary><h2>Plan context</h2><span class="muted" style="font-size:11px;margin-left:8px">synopsis · plotlines · season canon · what comes next</span></summary>
    <dl class="kv" style="margin-top:10px">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>
    ${beats}
    ${detailNote}
  </details>`;
}

function outlineStagesHTML(card, cards, runs) {
  if (!card?.id || !card.show_id || card.kind === "outline_scene") return "";
  const base = card.id.split(".").slice(0, 2).join(".");
  if (!/\.outline(\.|$)/.test(card.id)) return "";
  const stages = (cards || [])
    .filter(x => x.show_id === card.show_id && x.id.startsWith(base)
      && !x.id.includes(".retake."))
    .sort((a, b) => stageOrder(a.id, base) - stageOrder(b.id, base));
  if (stages.length < 2) return "";
  const rows = stages.map(x => {
    const work = workSummaryText(x);
    const cardHref = `#/card/${encodeURIComponent(x.id)}`;
    return `<tr>
      <td><a href="${cardHref}"><code>${esc(stageLabel(x.id, base))}</code></a></td>
      <td>${stateBadge(x.state)}</td>
      <td class="review-text">${esc(work) || '<span class="muted">—</span>'} ${stageFileLink(x, card.show_id)}</td>
    </tr>`;
  }).join("");
  return `<div class="panel" style="margin-bottom:16px">
    <h2>Outline stages</h2>
    <div class="muted" style="font-size:12px;margin:-4px 0 10px">
      Each stage is its own card. The merge/review card is the one you opened.
    </div>
    <table class="review-table">
      <thead><tr><th>Stage</th><th>State</th><th>What it produced</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`;
}

function noteBlocks(text) {
  const names = Object.keys(REVIEW_LABELS).sort((a, b) => b.length - a.length);
  const pattern = new RegExp(`(?:^|[;\\n])\\s*(${names.join("|")}):\\s*`, "gi");
  const out = [];
  let match, lastIndex = 0, last = null;
  while ((match = pattern.exec(text))) {
    if (last) out.push({ name: last, text: text.slice(lastIndex, match.index).trim() });
    last = match[1].toLowerCase();
    lastIndex = pattern.lastIndex;
  }
  if (last) out.push({ name: last, text: text.slice(lastIndex).trim() });
  return out.length ? out : null;
}

function notesHTML(card) {
  const raw = card.notes || [];
  if (!raw.length) return '<div class="empty">No notes.</div>';
  return `<div class="note-list">${raw.map(n => {
    const blocks = noteBlocks(n.text);
    const body = blocks
      ? `<div class="note-blocks">${blocks.map(b => `<div class="note-block">
          <span class="badge ${REVIEW_BLOCKING.includes(b.name) ? "bad" : ""}">${esc(reviewerName(b.name))}</span>
          <div class="text-block">${esc(b.text)}</div>
        </div>`).join("")}</div>`
      : `<div class="text-block">${esc(n.text)}</div>`;
    return `<details class="note">
      <summary><strong>${esc(n.by)}</strong> · ${esc(n.ts)}</summary>
      ${body}
    </details>`;
  }).join("")}</div>`;
}

function humanBytes(n) {
  const x = Number(n) || 0;
  if (x < 1024) return `${x} B`;
  if (x < 1024 * 1024) return `${(x / 1024).toFixed(1)} KB`;
  return `${(x / 1024 / 1024).toFixed(1)} MB`;
}

function dataFileGroup(path) {
  if (path.includes("/prompts/")) return "Prompt logs";
  if (path.includes("/reviews/")) return "Review transcripts";
  if (path.includes("/storyboard/")) return "Storyboard";
  if (path.includes("/video/")) return "Video";
  if (/^bible\./.test(path) || path === "plan.json"
      || path === "plan.skeleton.json") return "Canon";
  if (/^seasons\//.test(path)) return "Season canon";
  if (/^characters\//.test(path)) return "Characters";
  if (/^scenes\//.test(path)) return "Scenes";
  if (/^voices\//.test(path)) return "Voices";
  if (/^gate0\//.test(path)) return "Gate 0";
  if (/^assets\//.test(path)) return "Assets";
  if (/^runs\/EP\d+\/plan/.test(path)) return "Plan";
  if (/^runs\/EP\d+\/(script|scene_details)/.test(path)) return "Script / details";
  return "Other";
}

function fileTableHTML(files, showId) {
  const groups = new Map();
  for (const f of files || []) {
    const g = dataFileGroup(f.path);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(f);
  }
  const order = ["Plan", "Script / details", "Prompt logs", "Review transcripts",
                 "Storyboard", "Video", "Canon", "Season canon", "Characters",
                 "Scenes", "Voices", "Gate 0", "Assets", "Other"];
  return order.filter(g => groups.has(g)).map(g => {
    const rows = groups.get(g).map(f => {
      const href = `/api/artifact?path=${encodeURIComponent(`shows/${showId}/${f.path}`)}`;
      const view = f.viewable
        ? `<button class="btn sm" data-view-file="${esc(f.path)}" data-show="${esc(showId)}">View</button>`
        : `<a class="btn sm" href="${href}" target="_blank">Open</a>`;
      return `<tr><td class="file-path">${esc(f.path)}</td>
        <td>${esc(f.format || "")}</td><td>${humanBytes(f.bytes)}</td>
        <td>${view} <a class="btn sm" href="${href}" target="_blank">Raw</a></td></tr>`;
    }).join("");
    return `<details class="data-group" open>
      <summary>${esc(g)} <span class="muted">${groups.get(g).length}</span></summary>
      <table class="review-table"><thead><tr><th>File</th><th>Format</th><th>Size</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table>
    </details>`;
  }).join("");
}

function allDataHTML(card, history, runs, files) {
  const runHtml = (runs || []).map(r => `<details class="data-run">
    <summary><code>${esc(r.id)}</code> · ${esc(r.role)} · ${esc(r.phase)} · ${esc(r.state)}</summary>
    <pre class="json-block">${esc(JSON.stringify(r, null, 2))}</pre></details>`).join("");
  return `<details class="panel" style="margin-top:16px" id="all-data">
    <summary><h2>All data</h2><span class="muted" style="font-size:11px;margin-left:8px">raw card · runs · history · every file this stage produced</span></summary>
    <div class="data-section">
      <h3>This card (raw JSON)</h3>
      <pre class="json-block">${esc(JSON.stringify(card, null, 2))}</pre>
    </div>
    <div class="data-section">
      <h3>Runs (${(runs || []).length})</h3>
      ${runHtml || '<div class="empty">No runs.</div>'}
    </div>
    <div class="data-section">
      <h3>History (raw)</h3>
      <pre class="json-block">${esc(JSON.stringify(history || [], null, 2))}</pre>
    </div>
    <div class="data-section">
      <h3>Data files (${(files || []).length})</h3>
      ${(files || []).length ? fileTableHTML(files, card.show_id)
        : '<div class="empty">No files found.</div>'}
    </div>
  </details>`;
}

async function viewFileModal(showId, path) {
  const ext = String(path).split(".").pop().toLowerCase();
  const rawHref = `/api/artifact?path=${encodeURIComponent(`shows/${showId}/${path}`)}`;
  if (!["json", "yaml", "yml", "md", "txt", "csv", "log"].includes(ext)) {
    window.open(rawHref, "_blank");
    return;
  }
  let text = "";
  try {
    const res = await fetch(`/api/show/file?show_id=${encodeURIComponent(showId)}&path=${encodeURIComponent(path)}`);
    if (!res.ok) { window.open(rawHref, "_blank"); return; }
    const data = await res.json();
    text = String(data.content ?? "");
    if (ext === "json") {
      try { text = JSON.stringify(JSON.parse(text), null, 2); } catch (_) {}
    }
  } catch (_) { window.open(rawHref, "_blank"); return; }
  openModal(`<h2>${esc(path)}</h2><div class="body">
    <pre class="json-block" style="max-height:68vh;overflow:auto">${esc(text)}</pre>
    </div><div class="row"><button class="btn" id="vf-close">Close</button><a class="btn" href="${rawHref}" target="_blank">Open raw</a></div>`);
  $("#vf-close").addEventListener("click", closeModal);
}

async function renderCard(el, id) {
  if (!id) return el.innerHTML = '<div class="empty">No card id.</div>';
  const d = await api("/api/card?id=" + encodeURIComponent(id));
  const board = await api("/api/board");
  const c = d.card, cards = board.cards || [],
    runs = (board.runs || []).filter(r => r.card_id === c.id);
  const seenArt = new Set();
  const artifacts = (c.artifacts || [])
    .filter(a => {
      const raw = a.path;
      if (!raw) return false;
      const key = (c.show_id && !/^shows\//.test(raw)
        && !/^[A-Za-z]:[\\/]/.test(raw))
        ? `shows/${c.show_id}/${String(raw).replace(/\\/g, "/")}` : raw;
      if (seenArt.has(key)) return false;
      seenArt.add(key);
      return true;
    })
    .map(a => artHTML(a.path, a.caption, a, c.kind === "gate0_images" ? c.id : "", c.show_id)).join("");
  const notes = notesHTML(c);
  const hist = (d.history || []).map(h => {
    const reason = String(h.reason || "");
    const short = reason.length > 160
      ? reason.slice(0, 160).replace(/\s+/g, " ").trim() + "…" : reason;
    const detail = reason.length > 160
      ? `<details><summary>${esc(short)}</summary><pre>${esc(reason)}</pre></details>`
      : (reason ? `<div class="muted">${esc(reason)}</div>` : "");
    return `<li><div>${esc(h.ts)} · ${esc(h.from_state || "—")} → ${esc(h.to_state)} · ${esc(h.actor || "")}</div>${detail}</li>`;
  }).join("");
  const runRows = runs.map(r => {
    const res = r.result || {};
    const final = res.final || (res.status || res.verdict ? res : {});
    const tool = res.tool || res.result?.tool || r.role;
    let outcome = [final.status, final.verdict].filter(Boolean).join(" / ");
    if (!outcome && res.result && typeof res.result.pass === "boolean") {
      outcome = res.result.pass ? "verified / pass" : "retake / fail";
    }
    if (!outcome && res.result && typeof res.result === "object"
        && !Array.isArray(res.result)) {
      outcome = r.state === "done" ? "verified" : (r.state || "");
    }
    const summary = final.summary || final.notes || "";
    return `<tr class="click" data-run="${esc(r.id)}" title="${esc(summary)}">
      <td><code>${esc(tool)}</code></td><td>${esc(r.phase)}</td><td>${stateBadge(r.state)}</td>
      <td>${esc(outcome || "—")}</td><td>${esc(r.model || "")}</td><td>${dur(r)}</td>
      <td>${esc(fmtDate(r.started))}</td></tr>`;
  }).join("");
  const voiceTable = c.kind === "gate0_voices" ? voiceRowsHTML(c) : "";
  const fails = (d.history || []).filter(h => h.to_state === "blocked" || h.to_state === "retake");
  const lastFail = fails[fails.length - 1];
  const reviewRows = reviewGrades(c);
  const isReviewStop = (c.state === "blocked" || c.state === "retake") && reviewRows.length > 0;
  const scenesPanel = await scenesPanelHTML(c, cards);
  const skeletonPanel = await skeletonPanelHTML(c, cards);
  const planContext = await planContextHTML(c, cards);
  let cardFiles = [];
  try {
    const fd = await api("/api/card/files?id=" + encodeURIComponent(c.id));
    cardFiles = fd.files || [];
  } catch (_) {}
  const allData = allDataHTML(c, d.history || [], runs, cardFiles);
  const failed = isReviewStop
    ? `<div class="callout ${c.state === "blocked" ? "fail" : "warn"}">
        <strong>${esc(c.state === "blocked" ? "Blocked by review" : "Retake: reviewer changes requested")}</strong>
        <p>${esc(c.state === "blocked"
          ? `Attempt ${c.attempts}/${c.max_attempts}: the outline still fails a blocking reviewer.`
          : "This card is being revised with the review notes below.")}</p>
        ${reviewSummaryHTML(c)}
        <div class="actions">
          ${c.state === "blocked" ? `<button class="btn primary" data-act="advance" data-to="planned" data-id="${esc(c.id)}">Retry this step</button>` : ""}
          ${String(lastFail?.actor || "").startsWith("run_") ? `<button class="btn" data-run="${esc(lastFail.actor)}">View run log</button>` : ""}
        </div>
      </div>`
    : c.state === "blocked" && lastFail
      ? `<div class="callout fail">
          <strong>Blocked: the last run did not finish</strong>
          <p>${esc(failureText(lastFail.reason))}</p>
          <details><summary>Technical detail</summary><pre>${esc(lastFail.reason || "No reason recorded.")}</pre></details>
          <div class="actions">
            <button class="btn primary" data-act="advance" data-to="planned" data-id="${esc(c.id)}">Retry this step</button>
            ${String(lastFail.actor || "").startsWith("run_") ? `<button class="btn" data-run="${esc(lastFail.actor)}">View run log</button>` : ""}
          </div>
        </div>`
      : "";
  el.innerHTML = `
    <div class="page-head">
      <div><h1>${esc(cardTitle(c))}</h1><p>${esc(showTitle(c.show_id))} · ${c.episode ? "EP" + String(c.episode).padStart(2, "0") : "Gate 0"} · ${esc(KIND_TITLE[c.kind] || c.kind)} · <code>${esc(c.id)}</code></p></div>
      <div class="actions">${stateBadge(c.state)}${cardActions(c, true)}</div>
    </div>
    ${outlineStagesHTML(c, cards, runs)}
    ${scenesPanel}
    ${skeletonPanel}
    ${planContext}
    ${failed}
    ${voiceTable}
    <div class="detail">
      <div>
        <div class="panel">
          <h2>What this step produced</h2>
          ${workSummaryText(c) ? `<div class="work-summary">${esc(workSummaryText(c))}</div>` : ""}
          ${resultHTML(c.result)}
          <h3>Artifacts</h3>${artifacts || '<div class="empty">No artifacts yet.</div>'}
          <h3>Notes</h3>${notes}
        </div>
        <div class="panel" style="margin-top:16px">
          <h2>Runs</h2>
          ${runRows ? `<table><thead><tr><th>Tool</th><th>Phase</th><th>State</th><th>Outcome</th><th>Model</th><th>Time</th><th>Started</th></tr></thead><tbody>${runRows}</tbody></table>` : '<div class="empty">No runs yet.</div>'}
        </div>
      </div>
      <div>
        <div class="panel">
          <h2>Card</h2>
          <dl class="kv">
            <dt>State</dt><dd>${esc(c.state)}</dd>
            <dt>Role</dt><dd>${esc(c.role || "—")}</dd>
            <dt>Reviewer</dt><dd>${esc(c.reviewer || "—")}</dd>
            <dt>Lane</dt><dd>${esc(c.lane)}</dd>
            <dt>Attempts</dt><dd>${c.attempts} / ${c.max_attempts}</dd>
            <dt>Priority</dt><dd>${c.priority}</dd>
            <dt>Show</dt><dd>${esc(c.show_id || "—")}</dd>
            <dt>Episode</dt><dd>${c.episode || "—"}</dd>
            <dt>Depends</dt><dd>${esc((c.depends_on || []).join(", ") || "—")}</dd>
            <dt>Units</dt><dd>${(c.units || []).filter(u => u.done).length}/${(c.units || []).length}</dd>
          </dl>
          <h3>History</h3>${hist ? `<ul class="history">${hist}</ul>` : '<div class="empty">No history.</div>'}
        </div>
      </div>
    </div>
    ${allData}`;
  el.querySelectorAll("[data-run]").forEach(tr => tr.addEventListener("click", () => runLogModal(tr.dataset.run)));
}

function renderValue(v, depth = 0) {
  if (v === null || v === undefined) return '<span class="muted">?</span>';
  if (typeof v === "string") return `<div class="text-block">${esc(v)}</div>`;
  if (typeof v === "number" || typeof v === "boolean") return `<span>${esc(String(v))}</span>`;
  if (Array.isArray(v)) {
    return v.length
      ? `<ul class="notes">${v.map(x => `<li>${renderValue(x, depth + 1)}</li>`).join("")}</ul>`
      : '<span class="muted">?</span>';
  }
  if (typeof v === "object") {
    if (depth > 4) return '<span class="muted">?</span>';
    const entries = Object.entries(v);
    if (!entries.length) return '<span class="muted">?</span>';
    return `<dl class="kv">${entries.map(([k, x]) =>
      `<dt>${esc(k.replace(/_/g, " "))}</dt><dd>${renderValue(x, depth + 1)}</dd>`).join("")}</dl>`;
  }
  return `<span>${esc(String(v))}</span>`;
}

function resultHTML(result) {
  if (!result || !Object.keys(result).length) return '<div class="empty">No result yet.</div>';
  const head = { ...result };
  const value = head.value;
  delete head.value;
  const top = Object.keys(head).length ? renderValue(head) : "";
  const nested = value
    ? `<details style="margin-top:8px"><summary style="cursor:pointer;color:var(--muted)">Tool output</summary>${renderValue(value)}</details>`
    : "";
  const audio = result.audio_card
    ? `<div class="actions" style="margin-top:10px"><button class="btn" onclick="location.hash='#/card/${esc(result.audio_card)}'">Open voice sample</button></div>`
    : "";
  return `<div class="result">${top}${nested}${audio}</div>`;
}

function artHTML(path, caption, art = null, cardId = "", showId = "") {
  const ext = (path.split(".").pop() || "").toLowerCase();
  const rel = showRelPath(path, showId);
  const showRelative = showId && rel && !/^shows\//.test(String(path))
    && !/^[A-Za-z]:[\\/]/.test(String(path));
  const url = showRelative
    ? "/api/artifact?path=" + encodeURIComponent(`shows/${showId}/${rel}`)
    : "/api/artifact?path=" + encodeURIComponent(path);
  const body = ["png","jpg","jpeg","webp","gif"].includes(ext)
    ? `<img src="${url}" data-full="${url}" data-caption="${esc(caption || path)}" alt="">`
    : ext === "mp4" || ext === "webm"
      ? `<video src="${url}" controls preload="metadata"></video>`
      : ["wav","mp3","m4a","ogg"].includes(ext)
        ? `<audio controls src="${url}"></audio>`
      : `<div class="empty" style="padding:8px"><a href="${url}" target="_blank">${esc(path.split("/").pop())}</a></div>`;
  const status = art?.status || "";
  let actions = "";
  if (art?.retakeable) {
    if (status === "rejected" || status === "pending") {
      actions = art.retake_id
        ? `<a class="btn sm" href="#/card/${encodeURIComponent(art.retake_id)}">${status === "pending" ? "Review retake" : "Retake queued"}</a>`
        : "";
    } else if (cardId) {
      actions = `<button class="btn sm danger" data-reject-image="${esc(path)}" data-card="${esc(cardId)}">Reject image</button>`;
    }
  }
  const badge = status
    ? `<span class="badge ${status === "accepted" ? "ok" : status === "rejected" ? "bad" : "warn"}">${esc(status)}</span>`
    : "";
  return `<figure class="art art-${esc(status || "new")}">${body}
    <figcaption class="cap">${badge} ${esc(caption || path)}<br>${esc(path)}</figcaption>
    ${actions ? `<div class="art-actions">${actions}</div>` : ""}</figure>`;
}

async function renderShows(el) {
  const d = await api("/api/shows");
  el.innerHTML = `
    <div class="page-head"><div><h1>Shows</h1><p>Gate 0 and episode progress. Queue an episode when the show is complete.</p></div>
      <div class="actions"><button class="btn primary" id="new-show">New show</button></div></div>
    <div class="grid">${d.shows.length ? d.shows.map(s => `
      <div class="tile">
        <h3>${esc(s.title || s.id)}</h3>
        <p><code>${esc(s.id)}</code> · ${s.cards} cards · ${s.accepted} accepted</p>
        <div class="row">
          <span class="badge ${s.complete ? "ok" : "warn"}">${s.complete ? "Gate 0 complete" : "Gate 0 open"}</span>
          ${s.episodes.length ? `<span class="badge">EP ${s.episodes.join(", ")}</span>` : ""}
        </div>
        <div class="row">
          <button class="btn" data-open-show="${esc(s.id)}">Open</button>
          <button class="btn primary" data-ep-show="${esc(s.id)}">Queue episode</button>
        </div>
      </div>`).join("") : '<div class="empty">No shows yet.</div>'}</div>`;
  $("#new-show")?.addEventListener("click", newShowModal);
  el.querySelectorAll("[data-open-show]").forEach(b => b.addEventListener("click", () => location.hash = "#/show/" + encodeURIComponent(b.dataset.openShow)));
  el.querySelectorAll("[data-ep-show]").forEach(b => b.addEventListener("click", () => queueEpisodeModal(b.dataset.epShow)));
}

function statusBadge(v, label) {
  const s = String(v || "");
  const cls = s === "approved" ? "ok" : s === "stub" ? "bad" : s ? "warn" : "";
  return `<span class="badge ${cls}">${esc(label)}: ${esc(s || "?")}</span>`;
}

function showCardRows(cards) {
  return (cards || []).map(c => `<tr data-card="${esc(c.id)}">
    <td><code>${esc(c.id)}</code></td><td>${esc(cardTitle(c))}</td><td>${stateBadge(c.state)}</td><td>${esc(c.role)}</td></tr>`).join("");
}

async function renderShow(el, id) {
  const d = await api("/api/show?id=" + encodeURIComponent(id));
  S.showData = d;
  const bible = d.bible || {}, overall = bible.overall_plotline || {}, world = bible.world || {};
  const g = d.gate0 || {}, cont = d.continuity || {};
  const plotlines = d.plotlines || [], seasons = d.seasons || [], scenes = d.scenes || [];
  const chars = (d.characters || []).map(c => {
    const name = c.name || c.id || "", cid = c.id || "";
    return `<tr>
      <td><strong>${esc(name)}</strong><div class="meta">${esc(c.role || "")}</div></td>
      <td>${statusBadge(c.proposal, "proposal")}</td>
      <td>${statusBadge(c.refs, "refs")}</td>
      <td>${statusBadge(c.voice, "voice")}</td>
      <td class="actions">
        <button class="btn sm" data-char-detail="${esc(name)}">Details</button>
        <button class="btn sm" data-refs-char="${esc(name)}">View refs</button>
        ${cid ? `<button class="btn sm" data-edit-file="characters/${esc(cid)}.yaml" data-show="${esc(id)}">Edit</button>` : ""}
        <button class="btn sm" data-regen="refs" data-char="${esc(name)}">Regen refs</button>
        <button class="btn sm" data-regen="voices" data-char="${esc(name)}">Regen voice</button>
      </td></tr>`;
  }).join("");
  const eps = (d.episodes || []).map(e => `<tr>
    <td>EP${String(e.episode).padStart(2, "0")}<div class="meta">${esc(e.title || "")} ${e.type ? " · " + esc(e.type) : ""}</div></td>
    <td>${esc(e.synopsis || "")}</td>
    <td>${e.plan ? "plan " : ""}${e.script ? "script " : ""}${e.storyboard ? "board " : ""}${e.shots} shot(s)${e.episode_file ? " ? final" : ""}</td>
    <td>${e.cards} cards ? ${e.accepted} accepted</td>
    <td><button class="btn sm" data-ep-detail="${e.episode}">Details</button>
        <button class="btn sm" data-ep-open="${e.episode}">Board</button>
        <button class="btn sm" data-ep-queue="${e.episode}">Queue</button></td>
  </tr>`).join("");
  const files = (d.files || []).map(f => `<div class="file-row">
    <code>${esc(f.path)}</code><span class="badge">${esc(f.format)}</span>
    <span style="color:var(--muted)">${f.bytes} B</span>
    <button class="btn sm" data-edit-file="${esc(f.path)}" data-show="${esc(id)}">Edit</button>
  </div>`).join("");
  const cards = (d.cards || []).map(c => `<tr class="click" data-card="${esc(c.id)}">
    <td><code>${esc(c.id)}</code></td><td>${esc(cardTitle(c))}</td><td>${stateBadge(c.state)}</td><td>${esc(c.role)}</td></tr>`).join("");
  const charRefs = (d.characters || []).flatMap(c =>
    (c.refs_images || []).map(x => artHTML(x.path, `${c.name} ? ${x.label}`))).join("");
  const sceneRefs = scenes.flatMap(s =>
    (s.images || []).map(x => artHTML(x.path, `${s.id} ? ${x.label}`))).join("");
  const plotlineRows = plotlines.map(p => `<tr>
    <td><strong>${esc(p.name || p.id)}</strong><div class="meta">${esc(p.id || "")}</div></td>
    <td>${esc(p.kind || "")}</td><td>${statusBadge(p.status, "status")}</td>
    <td>${esc((p.characters || []).join(", "))}</td>
    <td>${esc(p.summary || "")}</td><td>${p.last_seen_episode || 0}</td>
    <td>${p.arc_remaining ?? "?"}</td><td>${p.cooldown_remaining ?? "?"}</td>
  </tr>`).join("");
  const seasonHtml = seasons.length ? seasons.map(s => `
    <h3>${esc(s.title || "Season " + s.season)} <span class="badge">${esc(s.status || "")}</span>
      <span class="badge">${s.num_episodes || (s.episodes || []).length} eps</span></h3>
    ${(s.season_arc || {}).summary ? `<p style="color:var(--muted);font-size:12px;margin:0 0 10px">${esc(s.season_arc.summary)}</p>` : ""}
    <table><thead><tr><th>Ep</th><th>Title</th><th>Type</th><th>Logline</th><th>Plotlines</th><th>Cliffhanger</th></tr></thead><tbody>
      ${(s.episodes || []).map(e => `<tr><td>${e.episode || ""}</td><td>${esc(e.title || "")}</td>
        <td>${esc(e.episode_type || "")}</td><td>${esc(e.logline || "")}</td>
        <td>${esc((e.featured_plotlines || []).join(", "))}</td><td>${esc(e.cliffhanger || "")}</td></tr>`).join("")}
    </tbody></table>`).join("") : '<div class="empty">No season plan yet.</div>';
  const sceneHtml = scenes.length ? `<div class="grid">${scenes.map(s => `<div class="tile">
      <h3>${esc(s.id)} ? ${esc(s.location || "")}</h3>
      <p>${esc(s.time_of_day || "")} ? ${esc((s.characters || []).join(", "))}</p>
      <p>${esc(s.summary || "")}</p>
      <p style="font-size:11px;color:var(--muted)">${esc(s.location_setting || "")}</p>
      <div class="art-grid">${(s.images || []).map(x => artHTML(x.path, x.label)).join("")}</div>
    </div>`).join("")}</div>` : '<div class="empty">No scenes yet.</div>';
  el.innerHTML = `
    <div class="page-head">
      <div><h1>${esc(bible.title || id)}</h1>
        <p><code>${esc(id)}</code> · ${esc((bible.genre || []).join(" / "))} · Gate 0 ${d.state?.complete ? "complete" : "open"} · ${d.characters?.length || 0} characters · ${d.episodes?.length || 0} episodes</p></div>
      <div class="actions">
        <button class="btn primary" data-queue-ep="${esc(id)}">Queue episode</button>
        <button class="btn" data-regen="plan">Re-run plan</button>
        <button class="btn" data-regen="refs">Re-run refs</button>
        <button class="btn" data-regen="voices">Re-run voices</button>
        <button class="btn" data-back-shows>Shows</button>
      </div>
    </div>
    <div class="detail">
      <div>
        <div class="panel"><h2>Plot</h2>
          <p style="font-size:14px;line-height:1.6;margin:0 0 10px">${esc(bible.logline || "")}</p>
          <div class="row" style="margin-bottom:10px">
            ${(bible.genre || []).map(x => `<span class="badge">${esc(x)}</span>`).join("")}
            ${(bible.tone || []).map(x => `<span class="badge">${esc(x)}</span>`).join("")}
          </div>
          <dl class="kv">
            <dt>Overall</dt><dd><strong>${esc(overall.name || "")}</strong> ? ${esc(overall.summary || "")}</dd>
            <dt>Characters</dt><dd>${esc((overall.characters || []).join(", "))}</dd>
            <dt>Objects</dt><dd>${esc((overall.objects || []).join(", "))}</dd>
            <dt>World</dt><dd>${esc(world.setting || "")}</dd>
            <dt>Maturity</dt><dd>${esc(bible.content_policy || "")} ? ${esc((bible.mature_spec || {}).quotient || "")}</dd>
            <dt>Runtime</dt><dd>${esc(bible.runtime_target_s || "")}${bible.runtime_target_s ? "s" : ""}</dd>
          </dl>
          ${(world.rules || []).length ? `<h3>World rules</h3><ul class="notes">${(world.rules || []).map(r => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
        </div>
        <div class="panel" style="margin-top:16px"><h2>Plotlines</h2>
          ${plotlines.length ? `<table><thead><tr><th>Plotline</th><th>Kind</th><th>Status</th><th>Characters</th><th>Summary</th><th>Last ep</th><th>Arc</th><th>Cd</th></tr></thead><tbody>${plotlineRows}</tbody></table>` : '<div class="empty">No plotlines.</div>'}
        </div>
        <details class="panel" style="margin-top:16px"><summary><h2>Season${seasons.length ? " ? " + esc(seasons[0].title || "") + " ? " + (seasons[0].num_episodes || 0) + " eps" : ""}</h2></summary>${seasonHtml}</details>
        <details class="panel" style="margin-top:16px"><summary><h2>Scenes (${scenes.length})</h2></summary>${sceneHtml}</details>
        <div class="panel" style="margin-top:16px"><h2>Episodes</h2>
          ${eps ? `<table><thead><tr><th>Episode</th><th>Synopsis</th><th>Artifacts</th><th>Cards</th><th></th></tr></thead><tbody>${eps}</tbody></table>` : '<div class="empty">No episodes yet. Queue one from the header.</div>'}
        </div>
        <div class="panel" style="margin-top:16px"><h2>Gate 0</h2>
          <div class="row" style="gap:8px;flex-wrap:wrap">
            <span class="badge ${g.pending_by_domain?.plan ? "warn" : "ok"}">plan ${esc(g.pending_by_domain?.plan?.step || "clear")}</span>
            <span class="badge ${g.pending_by_domain?.images ? "warn" : "ok"}">images ${esc(g.pending_by_domain?.images?.char || "clear")}</span>
            <span class="badge ${g.pending_by_domain?.voices ? "warn" : "ok"}">voices ${esc(g.pending_by_domain?.voices?.char || "clear")}</span>
          </div>
          <h3>Characters</h3>
          ${chars ? `<table><thead><tr><th>Name</th><th>Proposal</th><th>Refs</th><th>Voice</th><th>Actions</th></tr></thead><tbody>${chars}</tbody></table>` : '<div class="empty">No characters.</div>'}
        </div>
        <details class="panel" open style="margin-top:16px"><summary><h2>Reference images (${(d.characters || []).reduce((n, c) => n + (c.refs_images || []).length, 0) + scenes.reduce((n, s) => n + (s.images || []).length, 0)})</h2></summary>
          ${charRefs ? `<h3>Characters</h3><div class="art-grid">${charRefs}</div>` : ""}
          ${sceneRefs ? `<h3>Scenes</h3><div class="art-grid">${sceneRefs}</div>` : ""}
          ${!charRefs && !sceneRefs ? '<div class="empty">No reference images yet.</div>' : ""}
        </details>
        <details class="panel" style="margin-top:16px"><summary style="cursor:pointer"><h2 style="display:inline">Files (${(d.files || []).length})</h2></summary>
          ${files || '<div class="empty">No editable files.</div>'}
        </details>
        <div class="panel" style="margin-top:16px"><h2>Cards</h2>
          <table><thead><tr><th>Id</th><th>Title</th><th>State</th><th>Role</th></tr></thead>
          <tbody id="show-cards-body">${showCardRows(d.cards || []) || '<tr><td colspan="4" class="empty">No cards.</td></tr>'}</tbody></table>
        </div>
      </div>
      <div>
        <div class="panel"><h2>Continuity</h2>
          <dl class="kv"><dt>Episode</dt><dd>${esc(cont.episode || 0)}</dd></dl>
          <h3>Unresolved threads</h3>
          <ul class="notes">${(cont.unresolved_threads || []).map(t => `<li>${esc(t)}</li>`).join("") || "<li>None.</li>"}</ul>
          <h3>Rules</h3>
          <ul class="notes">${(cont.continuity_rules || []).map(t => `<li>${esc(t)}</li>`).join("") || "<li>None.</li>"}</ul>
        </div>
        <div class="panel" style="margin-top:16px"><h2>Canon</h2>
          <details><summary style="cursor:pointer;color:var(--muted)">Bible</summary>${resultHTML(d.bible || {})}</details>
          <details style="margin-top:10px"><summary style="cursor:pointer;color:var(--muted)">Concept</summary>${resultHTML(d.concept || {})}</details>
        </div>
      </div>
    </div>`;
  el.querySelector("[data-back-shows]").addEventListener("click", () => location.hash = "#/shows");
  el.querySelector("[data-queue-ep]").addEventListener("click", () => queueEpisodeModal(id));
  el.querySelectorAll("[data-regen]").forEach(b => b.addEventListener("click", () => regenerate(id, b.dataset.regen, b.dataset.char || "")));
  el.querySelectorAll("[data-edit-file]").forEach(b => b.addEventListener("click", () => editFile(b.dataset.show, b.dataset.editFile)));
  el.querySelectorAll("[data-refs-char]").forEach(b => b.addEventListener("click", () => {
    const c = (d.characters || []).find(x => (x.name || "") === b.dataset.refsChar) || {};
    refsModal(c);
  }));
  el.querySelectorAll("[data-char-detail]").forEach(b => b.addEventListener("click", () => {
    const c = (d.characters || []).find(x => (x.name || "") === b.dataset.charDetail) || {};
    charModal(c);
  }));
  el.querySelectorAll("[data-ep-detail]").forEach(b => b.addEventListener("click", () => episodeModal(id, Number(b.dataset.epDetail))));
  el.querySelectorAll("[data-ep-open]").forEach(b => b.addEventListener("click", () => {
    S.showFilter = id; location.hash = "#/board";
  }));
  el.querySelectorAll("[data-ep-queue]").forEach(b => b.addEventListener("click", () => queueEpisodeModal(id, Number(b.dataset.epQueue))));
  el.querySelectorAll("[data-card]").forEach(tr => tr.addEventListener("click", () => location.hash = "#/card/" + encodeURIComponent(tr.dataset.card)));
}

async function episodeModal(showId, ep) {
  try {
    const d = await api(`/api/show/episode?show_id=${encodeURIComponent(showId)}&episode=${ep}`);
    const plan = d.plan || {}, script = d.script || {};
    const scenes = (plan.scenes || []).map(s => `<li><strong>${esc(s.id)} ${esc(s.location || "")}</strong> ? ${esc(s.summary || "")}</li>`).join("");
    openModal(`<h2>EP${String(ep).padStart(2, "0")} ${esc(plan.title || "")}</h2><div class="body">
      <dl class="kv">
        <dt>Type</dt><dd>${esc(plan.episode_type || "")}</dd>
        <dt>Threat</dt><dd>${esc(plan.threat_of_the_week || "")}</dd>
        <dt>Plot</dt><dd>${esc(plan.plot || "")}</dd>
        <dt>Characters</dt><dd>${esc((plan.characters || []).join(", "))}</dd>
        <dt>Objects</dt><dd>${esc((plan.objects || []).join(", "))}</dd>
      </dl>
      <h3 style="font-size:11px;letter-spacing:.1em;color:var(--muted)">SCENES</h3>
      <ul class="notes">${scenes || "<li>No scenes.</li>"}</ul>
      <h3 style="font-size:11px;letter-spacing:.1em;color:var(--muted)">SCRIPT</h3>
      ${resultHTML(script)}
    </div><div class="row"><button class="btn" id="ep-close">Close</button></div>`);
    $("#ep-close").addEventListener("click", closeModal);
  } catch (e) { toast(e.message, true); }
}

function charModal(c) {
  const imgs = (c.refs_images || []).map(x => artHTML(x.path, x.label)).join("");
  const audio = c.voice_sample ? `<audio controls style="width:100%;margin:10px 0" src="/api/artifact?path=${encodeURIComponent(c.voice_sample)}"></audio>` : "";
  const personality = (c.personality || []).map(x => `<li>${esc(x)}</li>`).join("");
  openModal(`<h2>${esc(c.name || "Character")}</h2><div class="body">
    <dl class="kv">
      <dt>Role</dt><dd>${esc(c.role || "")}</dd>
      <dt>Gender</dt><dd>${esc(c.gender || "")}</dd>
      <dt>Appearance</dt><dd>${esc(c.appearance_canonical || c.identity_canonical || "")}</dd>
      <dt>Wardrobe</dt><dd>${esc(c.wardrobe_canonical || "")}</dd>
      <dt>Notes</dt><dd>${esc(c.appearance_notes || "")}</dd>
      <dt>Traits</dt><dd>${esc(c.traits_for_llm || "")}</dd>
    </dl>
    ${personality ? `<h3 style="font-size:11px;letter-spacing:.1em;color:var(--muted)">PERSONALITY</h3><ul class="notes">${personality}</ul>` : ""}
    <h3 style="font-size:11px;letter-spacing:.1em;color:var(--muted)">REFS</h3>
    <div class="art-grid">${imgs || '<div class="empty">No refs.</div>'}</div>${audio}
  </div><div class="row"><button class="btn" id="char-close">Close</button></div>`);
  $("#char-close").addEventListener("click", closeModal);
}

async function editFile(showId, path) {
  try {
    const d = await api(`/api/show/file?show_id=${encodeURIComponent(showId)}&path=${encodeURIComponent(path)}`);
    openModal(`<h2>Edit ${esc(path)}</h2><div class="body">
      <label>${esc((d.format || "").toUpperCase())} ? saved to this show
        <textarea id="edit-content" spellcheck="false" style="min-height:420px;font-family:var(--mono);font-size:12px">${esc(d.content)}</textarea>
      </label></div>
      <div class="row"><button class="btn" id="edit-cancel">Cancel</button><button class="btn primary" id="edit-save">Save</button></div>`);
    $("#edit-cancel").addEventListener("click", closeModal);
    $("#edit-save").addEventListener("click", async () => {
      try {
        await post("/api/show/file", {show_id:showId, path, content:$("#edit-content").value});
        closeModal(); toast("Saved " + path);
        location.hash = "#/show/" + encodeURIComponent(showId);
        render();
      } catch (e) { toast(e.message, true); }
    });
  } catch (e) { toast(e.message, true); }
}

function imageModal(url, caption) {
  openModal(`<h2>${esc(caption || "Image")}</h2><div class="body" style="text-align:center">
    <img src="${url}" style="max-width:100%;max-height:72vh;object-fit:contain">
  </div><div class="row"><button class="btn" id="img-close">Close</button></div>`);
  $("#img-close").addEventListener("click", closeModal);
}

function refsModal(c) {
  const imgs = (c.refs_images || []).map(x => artHTML(x.path, x.label)).join("");
  const audio = c.voice_sample
    ? `<audio controls style="width:100%;margin:10px 0" src="/api/artifact?path=${encodeURIComponent(c.voice_sample)}"></audio>`
    : "";
  openModal(`<h2>${esc(c.name || "Refs")}</h2><div class="body">
    <div class="art-grid">${imgs || '<div class="empty">No ref images yet.</div>'}</div>${audio}
  </div><div class="row"><button class="btn" id="refs-close">Close</button></div>`);
  $("#refs-close").addEventListener("click", closeModal);
}

async function regenerate(showId, kind, char = "") {
  if (!confirm(`Queue ${kind} for ${showId}${char ? " / " + char : ""}?`)) return;
  try {
    const r = await post("/api/show/regenerate", {show_id:showId, kind, char});
    if (r.card) upsertCard(r.card);
    toast(`Queued ${r.card.id}`);
  } catch (e) { toast(e.message, true); }
}

function modelProvider(m) {
  return m.kind === "local" ? "local" : (m.id.split("/")[0] || "remote");
}

function modelOptions(current, models, provider) {
  const norm = (id) => String(id || "").replace(/^lm_studio_local\//, "");
  const filtered = provider ? models.filter(m => modelProvider(m) === provider) : models;
  const local = filtered.filter(m => m.kind === "local");
  const remote = filtered.filter(m => m.kind === "remote");
  const match = filtered.find(m => m.id === current || norm(m.id) === norm(current));
  const selectedId = match ? match.id : current;
  const head = current && !match
    ? `<option value="${esc(current)}" selected>${esc(current)} (current)</option>` : "";
  const group = (label, list) => list.length
    ? `<optgroup label="${esc(label)}">${list.map(m =>
        `<option value="${esc(m.id)}" ${m.id === selectedId ? "selected" : ""}>${esc(m.label || m.id)}</option>`).join("")}</optgroup>`
    : "";
  const byProvider = {};
  for (const m of remote) {
    const p = m.id.split("/")[0] || "remote";
    (byProvider[p] || (byProvider[p] = [])).push(m);
  }
  const remoteGroups = Object.entries(byProvider).map(([p, list]) => group(p, list)).join("");
  return head + group("Local (LM Studio)", local) + remoteGroups;
}

async function renderRoles(el) {
  const roles = S.board?.roles || {};
  let models = S.models;
  if (!models) {
    try { models = (await api("/api/models")).models || []; S.models = models; }
    catch (e) { models = []; }
  }
  const providers = {};
  for (const m of models) {
    const p = modelProvider(m);
    providers[p] = (providers[p] || 0) + 1;
  }
  const providerOptions = Object.entries(providers)
    .map(([p, n]) => `<option value="${esc(p)}" ${S.providerFilter === p ? "selected" : ""}>${esc(p)} (${n})</option>`).join("");
  el.innerHTML = `
    <div class="page-head"><div><h1>Roles &amp; models</h1><p>Each role runs its own model. Set writes <code>config/agents.yaml</code> and applies immediately.</p></div>
      <div class="actions">
        <label style="font-size:11px;color:var(--muted)">Provider
          <select id="provider-filter" style="margin-left:6px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:5px 8px">
            <option value="" ${S.providerFilter === "" ? "selected" : ""}>all (${models.length})</option>${providerOptions}
          </select>
        </label>
        <button class="btn" id="reload-models">Reload models</button>
      </div></div>
    <table><thead><tr><th>Role</th><th>Kind</th><th>Lane</th><th>Model</th><th></th></tr></thead><tbody>
    ${Object.entries(roles).map(([name, r]) => {
      const fixed = r.kind === "exec" || name === "human";
      return `<tr>
      <td><strong>${esc(name)}</strong></td><td>${esc(r.kind)}</td><td>${esc(r.lane)}</td>
      <td>${fixed ? `<span class="badge">${name === "human" ? "human gate" : "deterministic"}</span>`
        : `<select class="model-in" id="model-${esc(name)}">${modelOptions(r.model || "", models, S.providerFilter)}</select>`}</td>
      <td>${fixed ? "" : `<button class="btn sm" data-model="${esc(name)}">Set</button>`}</td></tr>`;
    }).join("")}
    </tbody></table>`;
  $("#reload-models").addEventListener("click", () => { S.models = null; renderRoles(el); });
  $("#provider-filter").addEventListener("change", e => {
    S.providerFilter = e.target.value; renderRoles(el);
  });
  el.querySelectorAll("[data-model]").forEach(b => b.addEventListener("click", async () => {
    const role = b.dataset.model, model = $("#model-" + CSS.escape(role)).value;
    try {
      const out = await post("/api/agents/model", {role, model});
      if (S.board) S.board.roles = out.roles;
      toast(`${role} ? ${model || "(default)"}`);
      renderRoles(el);
    } catch (e) { toast(e.message, true); }
  }));
}

function renderRuns(el) {
  const runs = [...(S.board?.runs || [])].sort((a,b) => (b.started || 0) - (a.started || 0));
  el.innerHTML = `
    <div class="page-head"><div><h1>Runs</h1><p>Every agent and executor run. Click a row for prompt, stdout, stderr.</p></div>
      <div class="actions"></div></div>
    <table><thead><tr><th>Card</th><th>Role</th><th>Phase</th><th>Lane</th><th>State</th><th>Model</th><th>Time</th><th>Started</th></tr></thead><tbody>
    ${runs.length ? runs.map(r => `<tr class="click" data-run="${esc(r.id)}">
      <td><code>${esc(r.card_id)}</code></td><td>${esc(r.role)}</td><td>${esc(r.phase)}</td><td>${esc(r.lane)}</td>
      <td>${stateBadge(r.state)}</td><td>${esc(r.model || "")}</td><td>${dur(r)}</td><td>${esc(fmtDate(r.started))}</td></tr>`).join("") : '<tr><td colspan="8" class="empty">No runs yet.</td></tr>'}
    </tbody></table>`;
  el.querySelectorAll("[data-run]").forEach(tr => tr.addEventListener("click", () => runLogModal(tr.dataset.run)));
}

function renderDone(el) {
  const cards = (S.board?.cards || []).filter(c => c.state === "accepted");
  el.innerHTML = `
    <div class="page-head"><div><h1>Done</h1><p>Accepted cards across all shows.</p></div></div>
    <div class="grid">${cards.length ? cards.map(c => `<div class="tile" data-open-card="${esc(c.id)}">
      <h3>${esc(cardTitle(c))}</h3><p>${esc(showTitle(c.show_id))} · <code>${esc(c.id)}</code> · ${esc(c.role)}</p>
      <div class="row">${stateBadge(c.state)}<span class="tag">${c.artifacts?.length || 0} proof</span></div></div>`).join("") : '<div class="empty">Nothing accepted yet.</div>'}</div>`;
  el.querySelectorAll("[data-open-card]").forEach(t => t.addEventListener("click", () => location.hash = "#/card/" + encodeURIComponent(t.dataset.openCard)));
}

/* ---------- modals ---------- */
function openModal(html) {
  S.modal = true;
  $("#modal-body").innerHTML = html;
  $("#modal").classList.remove("hidden");
}
function closeModal() { S.modal = false; $("#modal").classList.add("hidden"); }
$("#modal").addEventListener("click", e => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

function newShowModal() {
  openModal(`<h2>New show</h2><div class="body">
    <label>Show id<input id="ns-name" placeholder="rainline"></label>
    <label>Brief<textarea id="ns-brief" placeholder="A tense one-location cyberpunk chase..."></textarea></label>
    <p style="color:var(--muted);font-size:12px">Creates the skeleton and queues Gate 0 plan, images and voices.</p>
  </div><div class="row"><button class="btn" id="ns-cancel">Cancel</button><button class="btn primary" id="ns-create">Create</button></div>`);
  $("#ns-cancel").addEventListener("click", closeModal);
  $("#ns-create").addEventListener("click", async () => {
    try {
      const name = $("#ns-name").value.trim();
      if (!name) return toast("Name is required", true);
      await post("/api/show", {name, brief: $("#ns-brief").value});
      closeModal(); toast("Show queued: " + name); render();
    } catch (e) { toast(e.message, true); }
  });
}

async function queueEpisodeModal(showId = "", episode = 1) {
  const d = await api("/api/shows");
  const opts = d.shows.map(s => `<option value="${esc(s.id)}" ${s.id === showId ? "selected" : ""}>${esc(s.title || s.id)} (${esc(s.id)})</option>`).join("");
  openModal(`<h2>Queue episode</h2><div class="body">
    <label>Show<select id="qe-show">${opts || '<option value="">no shows</option>'}</select></label>
    <label>Episode<input id="qe-ep" type="number" min="1" value="${episode}"></label>
  </div><div class="row"><button class="btn" id="qe-cancel">Cancel</button><button class="btn primary" id="qe-go">Queue</button></div>`);
  $("#qe-cancel").addEventListener("click", closeModal);
  $("#qe-go").addEventListener("click", async () => {
    try {
      const show_id = $("#qe-show").value, episode = Number($("#qe-ep").value);
      if (!show_id || !episode) return toast("Show and episode are required", true);
      await post("/api/episode", {show_id, episode});
      closeModal(); toast(`Queued ${show_id} EP${episode}`);
    } catch (e) { toast(e.message, true); }
  });
}

async function runLogModal(id) {
  try {
    const d = await api("/api/run/log?id=" + encodeURIComponent(id));
    openModal(`<h2>Run <code>${esc(id)}</code></h2><div class="body">
      <h3 style="font-size:11px;letter-spacing:.1em;color:var(--muted)">PROMPT</h3><pre>${esc(d.prompt || "—")}</pre>
      <h3 style="font-size:11px;letter-spacing:.1em;color:var(--muted)">STDOUT</h3><pre>${esc(d.stdout || "—")}</pre>
      <h3 style="font-size:11px;letter-spacing:.1em;color:var(--muted)">STDERR</h3><pre>${esc(d.stderr || "—")}</pre>
    </div><div class="row"><button class="btn" id="rl-close">Close</button></div>`);
    $("#rl-close").addEventListener("click", closeModal);
  } catch (e) { toast(e.message, true); }
}

function rejectModal(id) {
  openModal(`<h2>Reject card</h2><div class="body"><label>Notes<textarea id="rj-notes" placeholder="What must change?"></textarea></label></div>
    <div class="row"><button class="btn" id="rj-cancel">Cancel</button><button class="btn danger" id="rj-go">Reject</button></div>`);
  $("#rj-cancel").addEventListener("click", closeModal);
  $("#rj-go").addEventListener("click", async () => {
    try {
      const out = await post("/api/gate0/decide", {card_id:id, action:"reject", notes:$("#rj-notes").value || "rejected"});
      if (out.card) upsertCard(out.card);
      closeModal(); toast("Rejected");
    } catch (e) { toast(e.message, true); }
  });
}

function rejectImageModal(cardId, path, caption) {
  openModal(`<h2>Reject image</h2><div class="body">
    <p>${esc(caption || path)}</p>
    <label>Notes<textarea id="ri-notes" placeholder="What must change in this image?"></textarea></label>
    </div><div class="row"><button class="btn" id="ri-cancel">Cancel</button><button class="btn danger" id="ri-go">Reject image</button></div>`);
  $("#ri-cancel").addEventListener("click", closeModal);
  $("#ri-go").addEventListener("click", async () => {
    try {
      const out = await post("/api/cards/reject_image", {card_id: cardId, path, notes: $("#ri-notes").value || "rejected"});
      if (out.card) upsertCard(out.card);
      if (out.retake) upsertCard(out.retake);
      closeModal();
      toast("Retake queued" + (out.retake?.id ? ": " + out.retake.id : ""));
      load();
    } catch (e) { toast(e.message, true); }
  });
}

function rejectSceneModal(cardId, sceneId, summary) {
  openModal(`<h2>Reject scene ${esc(sceneId)}</h2><div class="body">
    <p>${esc(summary || "")}</p>
    <label>Feedback<textarea id="rs-notes" placeholder="What should change in this scene?"></textarea></label>
    </div><div class="row"><button class="btn" id="rs-cancel">Cancel</button><button class="btn danger" id="rs-go">Reject &amp; rewrite</button></div>`);
  $("#rs-cancel").addEventListener("click", closeModal);
  $("#rs-go").addEventListener("click", async () => {
    try {
      const out = await post("/api/cards/reject_scene", {
        card_id: cardId, scene_id: sceneId,
        notes: $("#rs-notes").value || "rejected"});
      if (out.card) upsertCard(out.card);
      if (out.retake) upsertCard(out.retake);
      closeModal();
      toast("Scene rewrite queued" + (out.retake?.id ? ": " + out.retake.id : ""));
      load();
    } catch (e) { toast(e.message, true); }
  });
}

function rejectVoiceModal(cardId, path, caption) {
  openModal(`<h2>Reject voice sample</h2><div class="body">
    <p>${esc(caption || path)}</p>
    <label>Notes<textarea id="rv-notes" placeholder="What should change about this voice?"></textarea></label>
    </div><div class="row"><button class="btn" id="rv-cancel">Cancel</button><button class="btn danger" id="rv-go">Reject voice</button></div>`);
  $("#rv-cancel").addEventListener("click", closeModal);
  $("#rv-go").addEventListener("click", async () => {
    try {
      const out = await post("/api/cards/reject_voice", {card_id: cardId, path, notes: $("#rv-notes").value || "rejected"});
      if (out.card) upsertCard(out.card);
      if (out.retake) upsertCard(out.retake);
      closeModal();
      toast("Voice retake queued" + (out.retake?.id ? ": " + out.retake.id : ""));
      load();
    } catch (e) { toast(e.message, true); }
  });
}

/* ---------- actions ---------- */
document.addEventListener("click", async e => {
  const img = e.target.closest(".art img");
  if (img) {
    e.preventDefault();
    imageModal(img.dataset.full || img.src, img.dataset.caption || "");
    return;
  }
  const rejectVoice = e.target.closest("[data-reject-voice]");
  if (rejectVoice) {
    e.preventDefault();
    e.stopPropagation();
    const row = rejectVoice.closest("tr");
    const name = row?.querySelector("td")?.textContent?.trim() || "";
    rejectVoiceModal(rejectVoice.dataset.card, rejectVoice.dataset.rejectVoice,
                     name ? name + " voice sample" : "");
    return;
  }
  const rejectImage = e.target.closest("[data-reject-image]");
  if (rejectImage) {
    e.preventDefault();
    e.stopPropagation();
    rejectImageModal(rejectImage.dataset.card, rejectImage.dataset.rejectImage,
                     rejectImage.closest(".art")?.querySelector(".cap")?.textContent?.trim() || "");
    return;
  }
  const rejectScene = e.target.closest("[data-reject-scene]");
  if (rejectScene) {
    e.preventDefault();
    e.stopPropagation();
    rejectSceneModal(rejectScene.dataset.card, rejectScene.dataset.rejectScene,
                     rejectScene.dataset.summary || "");
    return;
  }
  const viewFile = e.target.closest("[data-view-file]");
  if (viewFile) {
    e.preventDefault();
    e.stopPropagation();
    viewFileModal(viewFile.dataset.show, viewFile.dataset.viewFile);
    return;
  }
  const act = e.target.closest("[data-act]");
  if (act) {
    e.stopPropagation();
    const id = act.dataset.id;
    try {
      if (act.dataset.act === "advance") {
        await post("/api/cards/advance", {id, to:act.dataset.to, by:"human"});
        toast(`${id} → ${act.dataset.to}`); load();
      } else if (act.dataset.act === "review") {
        await post("/api/cards/review", {id});
        toast("Review queued"); load();
      } else if (act.dataset.act === "approve") {
        await post("/api/gate0/decide", {card_id:id, action:"approve"});
        toast("Approved"); load();
      } else if (act.dataset.act === "reject") {
        rejectModal(id);
      } else if (act.dataset.act === "back") {
        location.hash = "#/board";
      } else if (act.dataset.act === "delete") {
        if (!confirm(`Delete card ${id}? Running work may continue in the background.`)) return;
        await post("/api/cards/delete", {id});
        removeCard(id);
        toast("Deleted " + id);
      }
    } catch (err) { toast(err.message, true); }
    return;
  }
  const cardRow = e.target.closest("[data-card]");
  if (cardRow) { location.hash = "#/card/" + encodeURIComponent(cardRow.dataset.card); return; }
  const card = e.target.closest(".card[data-id]");
  if (card) location.hash = "#/card/" + encodeURIComponent(card.dataset.id);
});

async function toggleDaemon() {
  try {
    const on = !S.board?.daemon;
    await post("/api/daemon", {enabled:on});
    if (S.board) S.board.daemon = on;
    updateChrome();
    toast(on ? "Daemon started" : "Daemon stopped");
  } catch (e) { toast(e.message, true); }
}

function updateChrome() {
  const b = S.board;
  const dot = $("#daemon-dot"), label = $("#daemon-state");
  dot.classList.toggle("on", !!b?.daemon);
  label.textContent = b?.daemon ? "daemon running" : "daemon stopped";
  $("#toggle-daemon").textContent = b?.daemon ? "Stop daemon" : "Start daemon";
  $("#live").textContent = b?.daemon ? (S.sseOpen ? "live" : "connecting") : "paused";
}

function matchesCard(c) {
  if (S.showFilter && c.show_id !== S.showFilter) return false;
  if (S.state && c.state !== S.state) return false;
  if (!S.query) return true;
  return `${c.id} ${c.title} ${c.role} ${c.lane} ${c.kind}`.toLowerCase().includes(S.query.toLowerCase());
}

function updateLaneSummary() {
  const el = document.querySelector(".lane-summary");
  if (!el || !S.board) return;
  const running = (S.board.runs || []).filter(r => r.state === "running");
  el.innerHTML = Object.entries(S.board.lanes || {}).map(([k, v]) => {
    const n = running.filter(r => r.lane === k).length;
    return `<span class="lane"><b>${esc(k)}</b> ${n}/${v.capacity}</span>`;
  }).join("") + `<span class="lane"><b>${running.length}</b> running</span>
    <span class="lane"><b>${(S.board.cards || []).length}</b> cards</span>`;
}

function updateBoardCounts() {
  if (!S.board) return;
  document.querySelectorAll(".col[data-state]").forEach(col => {
    const s = col.dataset.state;
    const n = S.board.cards.filter(c => c.state === s).length;
    const small = col.querySelector("h2 small");
    if (small) small.textContent = n;
  });
  updateLaneSummary();
}

function removeCardDom(id) {
  const el = document.querySelector(`.card[data-id="${CSS.escape(id)}"]`);
  if (el) {
    const body = el.closest(".col-body");
    el.remove();
    if (body && !body.querySelector(".card")) body.innerHTML = '<div class="empty">Empty</div>';
  }
}

function upsertCard(card) {
  if (!S.board || !card?.id) return;
  const cards = S.board.cards;
  const i = cards.findIndex(c => c.id === card.id);
  const prev = i >= 0 ? cards[i] : null;
  const isNew = i < 0;
  if (i >= 0) cards[i] = card; else cards.push(card);
  if (isNew) toast(`New card: ${card.title || card.id} ? ${STATE_LABEL[card.state] || card.state}`);
  if (S.page === "board") {
    if (!matchesCard(card)) { removeCardDom(card.id); updateBoardCounts(); return; }
    const old = document.querySelector(`.card[data-id="${CSS.escape(card.id)}"]`);
    const html = cardHTML(card);
    const col = document.querySelector(`.col[data-state="${card.state}"] .col-body`);
    if (old && prev && prev.state !== card.state) {
      const oldBody = old.closest(".col-body");
      old.remove();
      if (oldBody && !oldBody.querySelector(".card")) oldBody.innerHTML = '<div class="empty">Empty</div>';
      if (col) {
        col.querySelector(".empty")?.remove();
        col.insertAdjacentHTML("beforeend", html);
      }
    } else if (old) {
      old.outerHTML = html;
    } else if (col) {
      col.querySelector(".empty")?.remove();
      col.insertAdjacentHTML("beforeend", html);
    }
    document.querySelector(".callout")?.remove();
    if (isNew && col) {
      const section = col.closest(".col");
      if (section) {
        section.classList.add("flash");
        setTimeout(() => section.classList.remove("flash"), 1400);
        const r = section.getBoundingClientRect();
        if (r.left < 0 || r.right > window.innerWidth) {
          section.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
        }
      }
    }
    updateBoardCounts();
  } else if (S.page === "show" && S.showData && S.showData.id === card.show_id) {
    const showCards = S.showData.cards || (S.showData.cards = []);
    const si = showCards.findIndex(c => c.id === card.id);
    if (si >= 0) showCards[si] = card; else showCards.push(card);
    const body = document.getElementById("show-cards-body");
    if (body) body.innerHTML = showCardRows(showCards) || '<tr><td colspan="4" class="empty">No cards.</td></tr>';
  } else if (S.page === "card" && (location.hash.split("/")[2] || "") === card.id) {
    if (!prev || prev.state !== card.state ||
        JSON.stringify(prev.result || {}) !== JSON.stringify(card.result || {})) {
      renderCard($("#page"), card.id);
    }
  }
}

function upsertRun(run) {
  if (!S.board || !run?.id) return;
  const runs = S.board.runs || (S.board.runs = []);
  const i = runs.findIndex(r => r.id === run.id);
  if (i >= 0) runs[i] = run; else runs.push(run);
  if (S.page === "board") updateLaneSummary();
  else if (S.page === "runs") renderRuns($("#page"));
}

function removeCard(id) {
  if (!S.board) return;
  S.board.cards = S.board.cards.filter(c => c.id !== id);
  if (S.page === "board") {
    removeCardDom(id);
    updateBoardCounts();
    if (S.board.cards.length === 0) renderBoard($("#page"));
  } else if (S.page === "show" && S.showData) {
    S.showData.cards = (S.showData.cards || []).filter(c => c.id !== id);
    const body = document.getElementById("show-cards-body");
    if (body) body.innerHTML = showCardRows(S.showData.cards) || '<tr><td colspan="4" class="empty">No cards.</td></tr>';
  } else if (S.page === "card" && location.hash.endsWith("/" + encodeURIComponent(id))) {
    location.hash = "#/board";
  }
}

function handleEvent(ev) {
  if (!ev) return;
  if (ev.type === "card" && ev.card) upsertCard(ev.card);
  else if (ev.type === "deleted") removeCard(ev.id);
  else if (ev.type === "run" && ev.run) upsertRun(ev.run);
}

function connectEvents() {
  if (EVENTS) EVENTS.close();
  EVENTS = new EventSource("/api/events");
  EVENTS.onmessage = e => { try { handleEvent(JSON.parse(e.data)); } catch (err) {} };
  EVENTS.onerror = () => { S.sseOpen = false; updateChrome(); };
  EVENTS.onopen = async () => {
    S.sseOpen = true;
    updateChrome();
    if (S.sse) {
      try {
        S.board = await api("/api/board");
        if (S.page === "board") renderBoard($("#page"));
        else if (S.page === "runs") renderRuns($("#page"));
      } catch (err) {}
    }
    S.sse = true;
  };
}

async function refreshBoard() {
  const snap = await api("/api/board");
  S.board = snap;
  try { S.shows = (await api("/api/shows")).shows || S.shows || []; } catch (e) {}
  S.boardFresh = true;
  updateChrome();
}

async function render() {
  const hash = location.hash || "#/board";
  const parts = hash.replace(/^#\//, "").split("/").filter(Boolean);
  const page = parts[0] || "board";
  S.page = page;
  document.querySelectorAll("#nav button").forEach(b => b.classList.toggle("active", b.dataset.route === "#/" + page));
  const crumb = {board:"BOARD", shows:"SHOWS", roles:"ROLES & MODELS", runs:"RUNS", done:"DONE", card:"CARD", show:"SHOW"}[page] || page.toUpperCase();
  $("#crumb").textContent = "STUDIO / " + crumb;
  const el = $("#page");
  try {
    if (page === "board") {
      if (!S.boardFresh) {
        try { await refreshBoard(); } catch (e) {}
      }
      S.boardFresh = false;
      renderBoard(el);
    }
    else if (page === "card") await renderCard(el, decodeURIComponent(parts[1] || ""));
    else if (page === "shows") await renderShows(el);
    else if (page === "show") await renderShow(el, decodeURIComponent(parts[1] || ""));
    else if (page === "roles") renderRoles(el);
    else if (page === "runs") renderRuns(el);
    else if (page === "done") renderDone(el);
    else location.hash = "#/board";
  } catch (e) { toast(e.message, true); }
}

async function load() {
  try {
    await refreshBoard();
    await render();
  } catch (e) { toast(e.message, true); }
}

$("#theme").addEventListener("click", () => {
  const html = document.documentElement;
  const next = html.dataset.theme === "dark" ? "light" : "dark";
  html.dataset.theme = next;
  localStorage.setItem("studio-theme", next);
  $("#theme").textContent = "Theme: " + next;
});
$("#toggle-daemon").addEventListener("click", toggleDaemon);
document.querySelectorAll("#nav button").forEach(btn => {
  btn.addEventListener("click", () => {
    if (btn.dataset.route === "#/board") {
      S.showFilter = ""; S.state = ""; S.query = "";
      if (location.hash === "#/board") { render(); return; }
    }
    location.hash = btn.dataset.route;
  });
});
window.addEventListener("hashchange", () => render());
document.addEventListener("DOMContentLoaded", () => {
  const theme = localStorage.getItem("studio-theme") || "dark";
  document.documentElement.dataset.theme = theme;
  $("#theme").textContent = "Theme: " + theme;
  load().then(connectEvents);
});
