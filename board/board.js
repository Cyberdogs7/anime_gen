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
    if (c.kind === "gate0_characters" || c.kind === "gate0_scenes") out.push(`<button class="btn${sm}" data-act="approve-all" data-id="${esc(c.id)}" title="Approve everything remaining at once">Approve all</button>`);
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
  locations: "Locations", cast: "Cast", outfits: "Outfits",
  narrative: "Narratives", dialogue: "Dialogue", shots: "Shot splits",
  camera: "Camera direction", action: "Action", sound: "Sound design",
  h3prompts: "H3 prompts",
  keyframes: "Keyframes", render: "Shot renders", qc: "Quality check",
  assemble: "Episode assembly",
  gate0_plan: "Show setup", gate0_ideas: "Show ideas",
  gate0_bible: "Series bible", gate0_characters: "Cast proposals",
  gate0_scenes: "Scene registry", gate0_images: "Reference art",
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
  if (kind === "locations") return `Set ${ep} locations`;
  if (kind === "cast") return `Cast ${ep} scenes`;
  if (kind === "outfits") return `Dress ${ep} cast`;
  if (kind === "narrative") return `Write ${ep} narratives`;
  if (kind === "dialogue") return `Write ${ep} dialogue`;
  if (kind === "shots") return `Split ${ep} shots`;
  if (kind === "camera") return `Direct ${ep} cameras`;
  if (kind === "action") return `Choreograph ${ep} action`;
  if (kind === "sound") return `Design ${ep} sound`;
  if (kind === "h3prompts") return `Compile ${ep} H3 prompts`;
  if (kind === "keyframes") return `Draw ${ep} keyframes`;
  if (kind === "storyboard") return `Build ${ep} storyboard`;
  return c.title || `${ep}: ${KIND_TITLE[kind] || kind}`;
}

function cardCategory(c) {
  const kind = c.kind || "";
  if (["outline", "outline_skeleton", "outline_beats", "outline_scene",
       "script", "objects", "locations", "cast", "outfits", "narrative",
       "dialogue", "shots", "camera", "action", "sound", "h3prompts",
       "gate0_plan", "gate0_ideas", "gate0_bible",
       "gate0_characters", "gate0_scenes", "voice_text", "fix"].includes(kind)) return "write";
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

/**
 * Voice text cards are retired: text + recording live on the voices card
 * now. Point anyone who lands on one at the card that does the work.
 */
function voiceRetiredBanner(c, cards) {
  const char = (c.payload || {}).char || "";
  const target = voiceRetakeTarget(cards, c.show_id, char);
  return `<div class="callout warn" style="margin-top:12px"><strong>Retired card type.</strong>
    Voice text cards no longer do anything — writing <em>and</em> recording happen
    on the voices card now (reject there re-runs the full loop).
    ${target ? `<div class="actions" style="margin-top:8px"><a class="btn primary" href="#/card/${encodeURIComponent(target)}">Open the voices card${char ? " for " + esc(char) : ""}</a></div>` : `<div class="muted">No live voices card found for this show.</div>`}</div>`;
}

/**
 * A "Retake queued" link pointing at a retired voice_text card is a dead
 * end. Reroute it to the voices card that actually owns the rework now:
 * the per-character one when it exists, else the bulk voices card.
 */
function voiceRetakeTarget(cards, showId, charName) {
  const all = cards || [];
  const match = (c) => c.kind === "gate0_voices" && c.show_id === showId
    && c.state !== "accepted";
  const perChar = all.find(c => match(c)
    && String((c.payload || {}).char || "").toLowerCase() === String(charName || "").toLowerCase());
  if (perChar) return perChar.id;
  const bulk = all.find(c => match(c) && !(c.payload || {}).char);
  return bulk ? bulk.id : "";
}

function voiceRowsHTML(c, roster, cards) {
  const wavs = (c.artifacts || []).filter(a => /_voice\.wav$/i.test(a.path || ""));
  const chars = (roster && roster.length ? roster : (c.result?.characters || []));
  const names = chars.length ? chars.map(x => String(x.name || ""))
    : [...new Set(wavs.map(a => String(a.caption || "").replace(/\s+voice sample$/i, "")))];
  if (!names.length) return "";
  const rows = names.map(name => {
    const ch = chars.find(x => String(x.name || "") === name);
    const a = wavs.find(x => String(x.caption || "").replace(/\s+voice sample$/i, "") === name);
    const artStatus = String((a && a.status) || "");
    const status = ch?.voice || artStatus || (a ? "pending" : "not generated");
    const url = a ? "/api/artifact?path=" + encodeURIComponent(a.path) : "";
    const approvedYet = status === "approved" || status === "accepted";
    let action = "";
    if (a && !approvedYet) {
      action = `<button class="btn sm danger" data-reject-voice="${esc(a.path)}" data-card="${esc(c.id)}">Reject voice</button>`;
      if (artStatus === "rejected" || artStatus === "pending") {
        const live = resolveRetakeTarget(cards, a.retake_id, c.id);
        if (live) {
          action += ` <a class="btn sm" href="#/card/${encodeURIComponent(live.id)}">${artStatus === "pending" ? "Review retake" : "Retake queued"}</a>`;
        }
      }
      action += ` <button class="btn sm primary" data-char-approve="${esc(name)}" data-card="${esc(c.id)}">Approve</button>`;
    }
    const cls = approvedYet ? "ok"
      : status === "rejected" ? "bad" : "warn";
    const sample = url ? `<audio controls preload="none" src="${url}"></audio>` : '<span class="muted">not generated yet</span>';
    return `<tr><td>${esc(name)}</td><td><span class="badge ${cls}">${esc(status)}</span>
      <td>${sample}</td><td>${action}</td></tr>`;
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
    <pre class="json-block">${esc(JSON.stringify(r, null, 2))}</pre>
    <div class="row"><button class="btn sm" data-run-files="${esc(r.id)}">Load files (prompt · trace · logs)</button></div>
    <div class="run-files"></div></details>`).join("");
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
    .map(a => artHTML(a.path, a.caption, a, c.kind === "gate0_images" ? c.id : "", c.show_id, cards)).join("");
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
  // Live Gate 0 snapshot (single fetch): fresh per-item approval state for
  // the voice roster and the review-materials buttons. The card's own
  // result can lag behind, so it is only a fallback.
  let showSnap = null;
  if (/^gate0_(characters|voices|scenes|ideas|images)$/.test(c.kind || "") && c.show_id) {
    try { showSnap = await api("/api/show?id=" + encodeURIComponent(c.show_id)); } catch (_) {}
  }
  const snapChars = ((showSnap && showSnap.gate0) || {}).characters || [];
  const snapLocs = ((showSnap && showSnap.gate0) || {}).locations || [];
  const snapSceneRefs = ((showSnap && showSnap.gate0) || {}).scene_refs || {};
  const voiceTable = c.kind === "gate0_voices" ? voiceRowsHTML(c, snapChars.length ? snapChars : undefined, cards) : "";
  const fails = (d.history || []).filter(h => h.to_state === "blocked" || h.to_state === "retake");
  const lastFail = fails[fails.length - 1];
  const reviewRows = reviewGrades(c);
  const isReviewStop = (c.state === "blocked" || c.state === "retake") && reviewRows.length > 0;
  const scenesPanel = await scenesPanelHTML(c, cards);
  const skeletonPanel = await skeletonPanelHTML(c, cards);
  const planContext = await planContextHTML(c, cards);
  // Review materials: inline every readable attached document (yaml/json/md/txt)
  // so the card view carries what a reviewer needs — bible, concept, cast,
  // screenplay, object prompts, QC reports — instead of bare download links.
  // The outline family keeps its richer parsed panels (skeleton/scenes tables).
  let reviewMats = "";
  if (!/^(outline|outline_skeleton|outline_beats|outline_scene)$/.test(c.kind || "")) {
    const approved = {};
    if (/^gate0_(characters|voices|scenes)$/.test(c.kind || "")) {
      if (showSnap) {
        for (const ch of snapChars) {
          if (c.kind === "gate0_characters" && ch.proposal === "approved") approved[ch.name] = true;
          if (c.kind === "gate0_voices" && ch.voice === "approved") approved[ch.name] = true;
        }
        for (const lc of snapLocs) {
          if (c.kind === "gate0_scenes" && lc.status === "approved") approved[lc.id] = true;
        }
      } else {
        for (const ch of (c.result?.characters || [])) {
          if (ch.proposal === "approved" && c.kind === "gate0_characters") approved[ch.name] = true;
          if (ch.voice === "approved" && c.kind === "gate0_voices") approved[ch.name] = true;
        }
      }
    }
    const docs = [];
    for (const a of (c.artifacts || [])) {
      const p = String(a.path || "");
      if (/\/bootstrap\.json$/i.test(p)) continue; // pipeline state, not review content
      if (!/\.(yaml|yml|json|md|txt)$/i.test(p)) continue;
      const rel = p.replace(/^shows\/[^/]+\//, "");
      try {
        const wantParsed = /\.ya?ml$/i.test(p) ? "&parsed=1" : "";
        const f = await api("/api/show/file?show_id=" + encodeURIComponent(c.show_id) + "&path=" + encodeURIComponent(rel) + wantParsed);
        let text = String(f.content ?? "");
        if (/\.json$/i.test(p)) {
          try { text = JSON.stringify(JSON.parse(text), null, 2); } catch (_) {}
        }
        let parsed = ("data" in f && f.data && typeof f.data === "object") ? f.data : null;
        if (!parsed && /\.json$/i.test(p)) {
          try { parsed = JSON.parse(String(f.content ?? "")); } catch (_) {}
        }
        let body;
        if (parsed && typeof parsed === "object") {
          body = formatDoc(rel, parsed, text, c, approved);
          if (body.length > 60000) body = `<div style="max-height:50vh;overflow:auto">${body.slice(0, 60000)}…[truncated]</div>`;
        } else {
          if (text.length > 40000) text = text.slice(0, 40000) + `\n…[truncated, ${text.length} chars total]`;
          body = `<pre class="json-block" style="max-height:50vh;overflow:auto">${esc(text)}</pre>`;
        }
        docs.push(`<details open><summary style="cursor:pointer"><strong>${esc(a.caption || rel)}</strong></summary>${body}</details>`);
      } catch (_) {
        docs.push(`<div class="empty">${esc(rel)} (could not load — use Raw link below)</div>`);
      }
    }
    if (docs.length) reviewMats = `<div class="panel" style="margin-top:16px"><h2>Review materials</h2>${docs.join("")}</div>`;
  }
  // Missing / failed references (images card only): characters and scenes
  // with no usable ref yet have no artifact to act on, so they get explicit
  // rows with Reject buttons here. Rejected items already carry their own
  // per-artifact buttons and are not repeated.
  let missingRefs = "";
  if ((c.kind || "") === "gate0_images" && c.state !== "accepted") {
    const missChars = (snapChars || []).filter(ch =>
      ["", null, undefined].includes(ch.refs) || ch.refs === "stub" || ch.refs === "missing");
    const missScenes = Object.entries(snapSceneRefs || {})
      .filter(([_, v]) => ["", null, undefined, "missing", "stub", "failed"].includes(v && v.status))
      .map(([sid, v]) => ({id: sid, status: (v && v.status) || "missing", note: (v && v.note) || ""}));
    const rows = [];
    for (const ch of missChars) {
      const st = (ch.refs === "stub" || ch.refs === "missing") ? ch.refs : "missing";
      const note = ch.refs_note || (st === "missing" ? "no ref generated yet" : "");
      rows.push(`<tr><td>${esc(ch.name || "?")}</td><td>character</td>`
        + `<td><span class="badge warn">${esc(st)}</span>${note ? `<div class="muted small">${esc(note)}</div>` : ""}</td>`
        + `<td><button class="btn sm danger" data-missing-reject="char" data-name="${esc(ch.name || "")}" data-card="${esc(c.id)}">Reject</button></td></tr>`);
    }
    for (const sc of missScenes) {
      rows.push(`<tr><td><code>${esc(sc.id)}</code></td><td>scene</td>`
        + `<td><span class="badge warn">${esc(sc.status)}</span>${sc.note ? `<div class="muted small">${esc(sc.note)}</div>` : ""}</td>`
        + `<td><button class="btn sm danger" data-missing-reject="scene" data-name="${esc(sc.id)}" data-card="${esc(c.id)}">Reject</button></td></tr>`);
    }
    if (rows.length) {
      missingRefs = `<div class="panel" style="margin-top:16px"><h2>Missing / failed references</h2>`
        + `<p class="muted">These have no image to review yet. Rejecting requeues the card; the rerun regenerates everything listed here.</p>`
        + `<table class="review-table"><thead><tr><th>Name</th><th>Kind</th><th>Status</th><th></th></tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
    }
  }
  let ideasSection = "";
  if ((c.kind || "") === "gate0_ideas") {
    try {
      const sd = await api("/api/show?id=" + encodeURIComponent(c.show_id));
      const reviewable = c.state === "reviewing" || c.state === "agent_verified";
      ideasSection = `<div class="panel" style="margin-top:16px"><h2>Show ideas</h2>${ideasSectionHTML(sd.ideas, sd.concept, reviewable ? c.id : null, c.show_id, true)}</div>`;
    } catch (_) {}
  }
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
    ${c.kind === "voice_text" ? voiceRetiredBanner(c, cards) : ""}
    ${outlineStagesHTML(c, cards, runs)}
    ${ideasSection}
    ${missingRefs}
    ${reviewMats}
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
          <details><summary style="cursor:pointer;color:var(--muted)">Raw step result (see All data below for full detail)</summary>${resultHTML(c.result)}</details>
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

function pendingDescriptor(pending) {
  if (!pending || typeof pending !== "object") return "";
  const kind = pending.kind, step = pending.step;
  if (step === "ideas") return "pitch selection — pick one or more ideas";
  if (step === "concept") return "concept review";
  if (step === "bible") return "bible review";
  if (step === "character") return `character proposal${pending.char ? ": " + pending.char : ""}`;
  if (step === "voice") return `voice sample${pending.char ? ": " + pending.char : ""}`;
  if (step === "refs") return `reference images${pending.char ? ": " + pending.char : ""}`;
  if (step === "scene") return `scene${pending.scene ? ": " + pending.scene : pending.id ? " " + pending.id : ""} review`;
  if (step === "scenes") return "scene registry review";
  return step || kind || "";
}

function cardStatusHTML(c, cards, runs, lastReason) {
  const byId = {};
  for (const x of (cards || [])) byId[x.id] = x;
  const deps = c.depends_on || [];
  const openDeps = deps.filter(d => (byId[d] || {}).state !== "accepted");
  const pend = pendingDescriptor(c?.result?.pending);
  const running = (runs || []).find(r => r.state === "running");
  const lines = [];
  if (c.state === "planned" || c.state === "inbox") {
    if (openDeps.length) {
      const links = openDeps.map(function (d) {
        const short = String(d).split(".").slice(-2).join(".");
        const st = (byId[d] || {}).state || "missing";
        return '<a href="#/card/' + encodeURIComponent(d) + '"><code>' + esc(short) + "</code></a> (" + esc(st) + ")";
      }).join(", ");
      lines.push("Waiting on: " + links);
    } else {
      lines.push("Queued — a worker will claim it.");
    }
  } else if (c.state === "in_work") {
    lines.push(running
      ? `Running <code>${esc(running.id)}</code> · ${esc(running.role || "")} · ${esc(running.model || "")} · ${esc(dur(running))} elapsed`
      : "Claimed by a worker.");
  } else if (c.state === "agent_verified" || c.state === "reviewing") {
    lines.push(pend ? `Waiting on: <strong>${esc(pend)}</strong>` : "Ready for review.");
  } else if (c.state === "retake") {
    lines.push(`Sent back for rework${lastReason ? ": " + esc(String(lastReason).slice(0, 220)) : "."} The daemon requeues it automatically.`);
  } else if (c.state === "blocked") {
    lines.push(`Blocked${lastReason ? ": " + esc(String(lastReason).slice(0, 220)) : "."} Unblock above to retry.`);
  } else if (c.state === "accepted") {
    lines.push("Accepted. Downstream stages may proceed.");
  } else if (c.state === "deferred") {
    lines.push("Deferred by a human. Resume from the actions above when ready.");
  }
  const att = `${c.attempts || 0} / ${c.max_attempts || 0}`;
  return `<div class="callout ${c.state === "blocked" ? "fail" : c.state === "accepted" ? "ok" : ""}">
    <strong>${esc(cardTitle(c))}</strong> · ${stateBadge(c.state)} <span class="muted">attempts ${esc(att)} · lane ${esc(c.lane || "—")}</span>
    ${lines.map(l => `<div style="margin-top:4px">${l}</div>`).join("")}</div>`;
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

function resolveRetakeTarget(cards, retakeId, excludeId) {
  if (!retakeId) return null;
  const t = (cards || []).find(x => x.id === retakeId);
  if (!t || t.id === excludeId) return null;
  return t;
}

function artHTML(path, caption, art = null, cardId = "", showId = "", cards = []) {
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
      const liveRetake = resolveRetakeTarget(cards, art.retake_id, cardId);
      if ((status === "rejected" || status === "pending") && liveRetake) {
        // Only legacy in-flight retakes get a link; current rework happens
        // on this card, so its own buttons stay available.
        actions = `<a class="btn sm" href="#/card/${encodeURIComponent(liveRetake.id)}">${status === "pending" ? "Review retake" : "Retake queued"}</a>`;
      } else if (cardId) {
        actions = `<button class="btn sm primary" data-approve-image="${esc(path)}" data-card="${esc(cardId)}">Approve</button> <button class="btn sm danger" data-reject-image="${esc(path)}" data-card="${esc(cardId)}">Reject</button>`;
      }
    }
  const badge = status
    ? `<span class="badge ${status === "accepted" ? "ok" : status === "rejected" ? "bad" : "warn"}">${esc(status)}</span>`
    : "";
  return `<figure class="art art-${esc(status || "new")}">${body}
    <figcaption class="cap">${badge} ${esc(caption || path)}<br>${esc(path)}</figcaption>
    ${actions ? `<div class="art-actions">${actions}</div>` : ""}</figure>`;
}

const _chips = arr => (Array.isArray(arr) ? arr : []).map(x => `<span class="tag">${esc(String(x))}</span>`).join(" ");
const _kv = rows => rows.filter(r => r[1]).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("");
function formatDoc(path, obj, text, card, approved) {
  // Per-item buttons only on the owning stage card: a character sheet shown
  // anywhere else must not approve through the wrong card/domain.
  const cardId = (card && (card.kind === "gate0_characters" || card.kind === "gate0_scenes") && card.id) || "";
  const isApproved = (name) => !!(approved && approved[name]);
  const itemBtns = (name) => cardId && name && !isApproved(name)
    ? `<div class="actions" style="margin-top:6px"><button class="btn sm primary" data-char-approve="${esc(name)}" data-card="${esc(cardId)}">Approve ${esc(name)}</button> <button class="btn sm danger" data-char-reject="${esc(name)}" data-card="${esc(cardId)}">Reject</button></div>`
    : (cardId && name && isApproved(name) ? `<div style="margin-top:6px"><span class="badge ok">approved</span></div>` : "");
  const raw = `<pre class="json-block" style="max-height:50vh;overflow:auto">${esc(text)}</pre>`;
  if (obj === null || typeof obj !== "object") return raw;
  // Pitch archive -> pitch tiles.
  if (Array.isArray(obj.pitches)) {
    const ap = obj.active || "";
    return `<div class="grid">${obj.pitches.map(p => pitchTile(p, false, ap, "")).join("")}</div>`;
  }
  // Concept -> header card.
  if (obj.premise && obj.logline && !obj.world) {
    return `<h3>${esc(obj.title || "Untitled")}</h3><p><em>${esc(obj.logline || "")}</em></p>`
      + `<p>${esc(obj.premise || "")}</p><p>${_chips(obj.genre)} ${_chips(obj.tone)}</p>`
      + (obj.hook ? `<p style="font-size:12px"><strong>Hook:</strong> ${esc(obj.hook)}</p>` : "");
  }
  // Bible -> sections.
  if (obj.world || obj.plotlines || obj.overall_plotline) {
    const w = obj.world || {}, ad = obj.art_direction || {};
    const pls = Array.isArray(obj.plotlines) ? obj.plotlines : [];
    const op = obj.overall_plotline || {};
    return `<h3>${esc(obj.title || "Untitled")}</h3>`
      + (obj.logline ? `<p><em>${esc(obj.logline)}</em></p>` : "")
      + `<p>${_chips(obj.genre)} ${_chips(obj.tone)}</p>`
      + (w.setting ? `<h4>World</h4><p>${esc(w.setting)}</p>` : "")
      + (w.rules?.length ? `<h4>Rules</h4><ul>${w.rules.map(r => `<li>${esc(r)}</li>`).join("")}</ul>` : "")
      + (w.established_facts?.length ? `<h4>Established facts</h4><ul>${w.established_facts.map(r => `<li>${esc(r)}</li>`).join("")}</ul>` : "")
      + (op.name ? `<h4>Overall: ${esc(op.name)}</h4><p>${esc(op.summary || "")}</p>` : "")
      + (pls.length ? `<h4>Plotlines</h4><table class="review-table"><thead><tr><th>Name</th><th>Status</th><th>Summary</th></tr></thead><tbody>${pls.map(p => `<tr><td><strong>${esc(p.name || p.id || "")}</strong><div class="muted">${esc((p.characters || []).join(", "))}</div></td><td>${esc(p.status || "")}</td><td class="review-text">${esc(p.summary || "")}</td></tr>`).join("")}</tbody></table>` : "")
      + (ad.palette || ad.lighting || ad.camera ? `<h4>Art direction</h4><dl class="kv">${_kv([["Palette", esc(ad.palette || "")], ["Lighting", esc(ad.lighting || "")], ["Camera", esc(ad.camera || "")]])}</dl>` : "")
      + (obj.mature_spec ? `<h4>Mature spec</h4><p style="font-size:12px">${esc(obj.mature_spec)}</p>` : "");
  }
  // Character sheet.
  if (obj.appearance_canonical || obj.identity_canonical) {
    const voice = obj.voice || {};
    return `<h3>${esc(obj.name || "Unnamed")}</h3>`
      + `<dl class="kv">${_kv([["Identity", esc(obj.identity_canonical || "")], ["Appearance", esc(obj.appearance_canonical || "")], ["Wardrobe", esc(obj.wardrobe_canonical || "")], ["Notes", esc(obj.appearance_notes || "")]])}</dl>`
      + (obj.voice_sample_text ? `<h4>Voice sample</h4><p><em>${esc(obj.voice_sample_text)}</em></p>` : "")
      + (voice.voice_description ? `<p style="font-size:12px"><strong>Voice:</strong> ${esc(voice.voice_description)}</p>` : "")
      + itemBtns(obj.name);
  }
  const scenes = Array.isArray(obj.scenes) ? obj.scenes : [];
  // Screenplay (scenes with shots/dialogue).
  if (scenes.length && scenes[0] && Array.isArray(scenes[0].shots)) {
    return scenes.map(s => {
      const shots = (s.shots || []).map(sh => {
        const dlg = (sh.dialogue || []).map(d => `<div><strong>${esc(d.char || "?")}:</strong> ${esc(d.line || "")}</div>`).join("");
        return `<li><code>${esc(sh.id || "")}</code>${sh.silence ? ' <span class="badge">silent</span>' : ""}${dlg ? `<div style="margin:4px 0 4px 12px;font-size:12px">${dlg}</div>` : ""}</li>`;
      }).join("");
      return `<details open><summary style="cursor:pointer"><strong>${esc(s.id || "")}</strong> <span class="muted">${esc(s.summary || "")}</span></summary><ul>${shots}</ul></details>`;
    }).join("");
  }
  // Single scene location file (what gate0_scenes actually attaches):
  // detail card with per-scene actions, not raw YAML.
  const isSceneCard = card && card.kind === "gate0_scenes";
  if (isSceneCard && obj.id && (obj.description || obj.setting_prompt || obj.location)) {
    const sid = obj.id;
    const done = !!(approved && approved[sid]);
    const btns = card.id && !done
      ? `<div class="actions" style="margin-top:6px"><button class="btn sm primary" data-scene-approve="${esc(sid)}" data-card="${esc(card.id)}">Approve</button> <button class="btn sm danger" data-scene-reject="${esc(sid)}" data-card="${esc(card.id)}">Reject</button></div>`
      : (card.id && done ? `<div style="margin-top:6px"><span class="badge ok">approved</span></div>` : "");
    return `<h3>${esc(obj.name || sid)}</h3>`
      + (obj.description ? `<p>${esc(obj.description)}</p>` : "")
      + `<dl class="kv">${_kv([["Location", esc(obj.location || "")], ["Setting", esc(obj.setting_prompt || "")]])}</dl>`
      + btns;
  }
  // Scene registry / scene list.
  if (scenes.length) {
    const sid = (card && card.kind === "gate0_scenes" && card.id) || "";
    const rowBtns = (s) => {
      if (!sid || !s.id) return "";
      if (approved && approved[s.id]) return `<span class="badge ok">approved</span>`;
      return `<button class="btn sm primary" data-scene-approve="${esc(s.id)}" data-card="${esc(sid)}">Approve</button> <button class="btn sm danger" data-scene-reject="${esc(s.id)}" data-card="${esc(sid)}">Reject</button>`;
    };
    return `<table class="review-table"><thead><tr><th>Scene</th><th>Location</th><th>Summary</th>${sid ? "<th></th>" : ""}</tr></thead><tbody>${scenes.map(s => `<tr><td><code>${esc(s.id || "")}</code></td><td>${esc(s.location || s.location_id || s.time_of_day || "")}</td><td class="review-text">${esc(s.summary || s.description || "")}</td>${sid ? `<td class="actions">${rowBtns(s)}</td>` : ""}</tr>`).join("")}</tbody></table>`;
  }
  // QC / vcheck report ({shot: {qc:{verdict,issues}}}).
  const vals = Object.values(obj);
  if (vals.length && vals.every(v => v && typeof v === "object" && (v.qc || v.vcheck || v.verdict))) {
    return `<table class="review-table"><thead><tr><th>Shot</th><th>QC</th><th>V-check</th><th>Issues</th></tr></thead><tbody>${Object.entries(obj).map(([sid, r]) => {
      const q = r.qc || {}, v = r.vcheck || {}, verdict = r.verdict || q.verdict || v.verdict || "";
      const issues = [...(q.issues || []), ...(v.issues || [])];
      const badge = verdict === "fail" ? "bad" : verdict === "pass" ? "ok" : "warn";
      return `<tr><td><code>${esc(sid)}</code></td><td><span class="badge ${badge}">${esc(q.verdict || verdict || "?")}</span></td><td><span class="badge ${badge}">${esc(v.verdict || verdict || "?")}</span></td><td class="review-text">${issues.map(esc).join("<br>")}</td></tr>`;
    }).join("")}</tbody></table>`;
  }
  return raw;
}

function pitchTile(p, pickable, activePitch, showId) {
  return `<div class="tile">
      <h3>${pickable ? `<input type="checkbox" class="pick-box" value="${esc(p.id)}" style="margin-right:6px">` : ""}${esc(p.title || p.id)}
        <span class="badge">${esc(p.status || "")}</span>${p.id === activePitch ? ' <span class="badge">active</span>' : ""}</h3>
      ${p.show_id ? `<p style="font-size:11px;color:var(--muted)">id: <code>${esc(p.show_id)}</code></p>` : ""}
      <p><em>${esc(p.logline || "")}</em></p>
      <p>${esc(p.premise || "")}</p>
      <p style="font-size:11px;color:var(--muted)">${esc((p.genre || []).join(" / "))} · ${esc((p.tone || []).join(", "))} · hook: ${esc(p.hook || "")}</p>
      ${!pickable ? `<button class="btn sm" data-reselect="${esc(p.id)}"${showId ? ` data-show="${esc(showId)}"` : ""} ${p.id === activePitch ? "disabled" : ""}>${p.id === activePitch ? "Active concept" : "Select"}</button>` : ""}
    </div>`;
}

function ideasSectionHTML(ideas, concept, pickCardId, showId, rePitch) {
  ideas = ideas || {};
  const pitches = ideas.pitches || [];
  if (!pitches.length) return '<div class="empty">No pitches yet — the ideas card has not run.</div>';
  const activePitch = ideas.active || (concept || {}).picked_from || "";
  const needPick = pitches.some(p => p.status === "proposed") && !pitches.some(p => p.status === "selected");
  return `
    ${needPick && pickCardId ? `<p style="color:var(--muted);font-size:12px">Tick one or more pitches, then approve — the first pick becomes the concept. Everything stays archived and reselectable.</p>
    <div class="grid">${pitches.filter(p => p.status === "proposed").map(p => pitchTile(p, true, activePitch, showId)).join("")}</div>
    <div class="row" style="margin-top:10px">
      <button class="btn primary" data-ideas-approve="${esc(pickCardId)}">Approve selection</button>
      ${rePitch && showId ? `<button class="btn" data-repitch="${esc(showId)}">Re-pitch (adds 5 more)</button>` : ""}
    </div>` : ""}
    <h3>Archive (${pitches.length})</h3>
    <div class="grid">${pitches.map(p => pitchTile(p, false, activePitch, showId)).join("")}</div>`;
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
          <button class="btn danger" data-del-show="${esc(s.id)}">Delete</button>
        </div>
      </div>`).join("") : '<div class="empty">No shows yet.</div>'}</div>`;
  $("#new-show")?.addEventListener("click", newShowModal);
  el.querySelectorAll("[data-open-show]").forEach(b => b.addEventListener("click", () => location.hash = "#/show/" + encodeURIComponent(b.dataset.openShow)));
  el.querySelectorAll("[data-ep-show]").forEach(b => b.addEventListener("click", () => queueEpisodeModal(b.dataset.epShow)));
  el.querySelectorAll("[data-del-show]").forEach(b => b.addEventListener("click", async () => {
    const sid = b.dataset.delShow;
    if (!confirm(`Delete show ${sid}? Its folder moves to .trash (recoverable) and its board rows are removed.`)) return;
    try {
      await post("/api/show/delete", {show_id: sid});
      toast(`Deleted ${sid}`); load();
    } catch (err) { toast(err.message, true); }
  }));
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
  const charCard = (d.cards || []).find(c => c.kind === "gate0_characters" && c.state === "reviewing")
    || (d.cards || []).find(c => c.kind === "gate0_characters");
  const charCardId = charCard ? charCard.id : "";
  const charApproved = {};
  for (const ch of ((d.gate0 || {}).characters || [])) {
    if (ch.proposal === "approved") charApproved[ch.name] = true;
  }
  const chars = (d.characters || []).map(c => {
    const name = c.name || c.id || "", cid = c.id || "";
    return `<tr>
      <td><strong>${esc(name)}</strong><div class="meta">${esc(c.role || "")}</div></td>
      <td>${statusBadge(c.proposal, "proposal")}</td>
      <td>${statusBadge(c.refs, "refs")}</td>
      <td>${statusBadge(c.voice, "voice")}</td>
      <td class="actions">
        <button class="btn sm" data-char-detail="${esc(name)}">Details</button>
        ${charCardId && !charApproved[name] ? `<button class="btn sm primary" data-char-approve="${esc(name)}" data-card="${esc(charCardId)}">Approve</button><button class="btn sm danger" data-char-reject="${esc(name)}" data-card="${esc(charCardId)}">Reject</button>` : ""}
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
  const ideas = d.ideas || {};
  const ideasCards = (d.cards || []).filter(c => (c.kind || "") === "gate0_ideas");
  const pickCard = ideasCards.find(c => c.state === "reviewing")
    || ideasCards.find(c => c.state === "agent_verified") || null;
  const ideasHtml = `<div class="panel" style="margin-top:16px"><h2>Show ideas</h2>${ideasSectionHTML(ideas, d.concept, pickCard ? pickCard.id : null, id, true)}</div>`;
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
        ${ideasHtml}
        <div class="panel" style="margin-top:16px"><h2>Gate 0</h2>
          <div class="row" style="gap:8px;flex-wrap:wrap">
            <span class="badge ${g.pending_by_domain?.ideas ? "warn" : "ok"}">ideas ${esc(g.pending_by_domain?.ideas?.step || "clear")}</span>
            <span class="badge ${g.pending_by_domain?.bible ? "warn" : "ok"}">bible ${esc(g.pending_by_domain?.bible?.step || "clear")}</span>
            <span class="badge ${g.pending_by_domain?.characters ? "warn" : "ok"}">characters ${esc(g.pending_by_domain?.characters?.char || g.pending_by_domain?.characters?.step || "clear")}</span>
            <span class="badge ${g.pending_by_domain?.scenes ? "warn" : "ok"}">scenes ${esc(g.pending_by_domain?.scenes?.step || "clear")}</span>
            <span class="badge ${g.pending_by_domain?.plan ? "warn" : "ok"}">plan ${esc(g.pending_by_domain?.plan?.step || "clear")}</span>
            <span class="badge ${g.pending_by_domain?.images ? "warn" : "ok"}">images ${esc(g.pending_by_domain?.images?.char || "clear")}</span>
            <span class="badge ${g.pending_by_domain?.voices ? "warn" : "ok"}">voices ${esc(g.pending_by_domain?.voices?.char || "clear")}</span>
          </div>
          <div class="row" style="gap:8px;margin-top:8px;flex-wrap:wrap">
            <button class="btn sm" data-regen="ideas">Re-pitch</button>
            <button class="btn sm" data-regen="bible">Rewrite bible</button>
            <button class="btn sm" data-regen="characters">Re-propose cast</button>
            <button class="btn sm" data-regen="scenes">Rebuild scenes</button>
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
  // Ideas actions (approve / reselect / re-pitch with data-show) are handled
  // by the document-level listener; other regen buttons bind here.
  el.querySelectorAll("[data-regen]:not([data-show])").forEach(b => b.addEventListener("click", () => regenerate(id, b.dataset.regen, b.dataset.char || "")));
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

async function regenerate(showId, kind, char = "", notes = "") {
  if (!notes && !confirm(`Queue ${kind} for ${showId}${char ? " / " + char : ""}?`)) return;
  try {
    const r = await post("/api/show/regenerate", {show_id:showId, kind, char, notes});
    if (r.card) upsertCard(r.card);
    toast(`Queued ${r.card.id}`);
  } catch (e) { toast(e.message, true); }
}

function repitchModal(showId) {
  openModal(`<h2>Re-pitch show ideas</h2>
  <div class="body">
    <label>Guidance for this round (optional)<textarea id="rp-notes" placeholder="e.g. less corporate conspiracy, more comedy; drop theChosenOne angle; pitches should feel like cozy mysteries"></textarea></label>
    <p style="color:var(--muted);font-size:12px">Adds 5 more pitches to the archive without erasing existing ones. Your notes are appended to the brief for this round only.</p>
  </div><div class="row"><button class="btn" id="rp-cancel">Cancel</button><button class="btn primary" id="rp-create">Re-pitch</button></div>`);
  $("#rp-cancel").addEventListener("click", closeModal);
  $("#rp-create").addEventListener("click", async () => {
    try {
      const notes = $("#rp-notes").value.trim();
      await post("/api/show/regenerate", {show_id:showId, kind:"ideas", notes});
      closeModal(); toast("Re-pitch queued"); load();
    } catch (e) { toast(e.message, true); }
  });
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
// No backdrop-click dismissal: a stray click off the dialog must never
// discard in-progress form work. Close only via the dialog's own buttons
// (Cancel/close) or Escape.
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

function newShowModal() {
  openModal(`<h2>New show</h2><div class="body">
    <label>Show id (optional — the picked pitch supplies it)<input id="ns-name" placeholder="leave blank for pitch-picked id"></label>
    <label>Brief<textarea id="ns-brief" placeholder="A tense one-location cyberpunk chase..."></textarea></label>
    <p style="color:var(--muted);font-size:12px">Creates the skeleton and queues the Gate 0 idea → bible → cast → scenes chain. Leave the id blank and the selected pitch names the show.</p>
  </div><div class="row"><button class="btn" id="ns-cancel">Cancel</button><button class="btn primary" id="ns-create">Create</button></div>`);
  $("#ns-cancel").addEventListener("click", closeModal);
  $("#ns-create").addEventListener("click", async () => {
    try {
      const name = $("#ns-name").value.trim();
      const out = await post("/api/show", {name, brief: $("#ns-brief").value});
      closeModal(); toast("Show queued: " + (out.show_id || name));
      location.hash = "#/show/" + encodeURIComponent(out.show_id || name);
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

function runFilesHTML(d) {
  let trace = String(d.trace || "");
  if (trace) {
    try { trace = JSON.stringify(JSON.parse(trace), null, 2); } catch (_) {}
  }
  const sec = (t, v) => `<h3 style="font-size:11px;letter-spacing:.1em;color:var(--muted)">${t}</h3><pre style="max-height:40vh;overflow:auto">${esc(v || "—")}</pre>`;
  return sec("TRACE", trace) + sec("STDOUT", d.stdout) + sec("STDERR", d.stderr);
}

async function runLogModal(id) {
  try {
    const d = await api("/api/run/log?id=" + encodeURIComponent(id));
    openModal(`<h2>Run <code>${esc(id)}</code></h2><div class="body">
      <h3 style="font-size:11px;letter-spacing:.1em;color:var(--muted)">PROMPT</h3><pre>${esc(d.prompt || "—")}</pre>
      ${runFilesHTML(d)}
    </div><div class="row"><button class="btn" id="rl-close">Close</button></div>`);
    $("#rl-close").addEventListener("click", closeModal);
  } catch (e) { toast(e.message, true); }
}

function rejectModal(id, char = "", scene = "", preset = "") {
  const target = char || scene;
  openModal(`<h2>Reject ${target ? esc(target) : "card"}</h2><div class="body"><label>Notes<textarea id="rj-notes" placeholder="What must change?">${esc(preset || "")}</textarea></label>
    <div class="row"><button class="btn" id="rj-cancel">Cancel</button><button class="btn danger" id="rj-go">Reject${target ? " " + esc(target) : ""}</button></div>`);
  $("#rj-cancel").addEventListener("click", closeModal);
  $("#rj-go").addEventListener("click", async () => {
    try {
      const out = await post("/api/gate0/decide", {card_id:id, action:"reject", notes:$("#rj-notes").value || "rejected", char, scene});
      if (out.retake) upsertCard(out.retake);
      closeModal();
      if (out.retake?.id) {
        toast(`Rejected — revision queued: ${out.retake.id}`);
        location.hash = "#/card/" + encodeURIComponent(out.retake.id);
      } else { toast("Rejected"); load(); }
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
      toast(out.retake?.id ? "Retake queued: " + out.retake.id : "Reworking on this card");
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
  const approveImage = e.target.closest("[data-approve-image]");
  if (approveImage) {
    e.preventDefault();
    e.stopPropagation();
    try {
      await post("/api/cards/approve_image", {card_id: approveImage.dataset.card, path: approveImage.dataset.approveImage});
      toast("Image approved"); load();
    } catch (err) { toast(err.message, true); }
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
  // Ideas actions for both views (buttons carry data-show).
  const ia = e.target.closest("[data-ideas-approve]");
  if (ia) {
    e.preventDefault();
    const picks = [...document.querySelectorAll(".pick-box:checked")].map(b => b.value);
    if (!picks.length) { toast("Select at least one pitch", true); return; }
    try {
      const out = await post("/api/gate0/decide", {card_id: ia.dataset.ideasApprove, action: "approve", picks});
      toast(`Concept approved from ${picks[0]}` + (out.renamed ? ` — show is now ${out.show_id}` : ""));
      if (out.renamed && out.show_id) location.hash = "#/show/" + encodeURIComponent(out.show_id);
      else load();
    } catch (err) { toast(err.message, true); }
    return;
  }
  const rs = e.target.closest("[data-reselect]");
  if (rs && rs.dataset.show) {
    e.preventDefault();
    try {
      const out = await post("/api/show/reselect", {show_id: rs.dataset.show, pitch_id: rs.dataset.reselect});
      toast(`Reselected ${rs.dataset.reselect}` + (out.renamed ? ` — show is now ${out.show_id}` : ""));
      if (out.renamed && out.show_id) location.hash = "#/show/" + encodeURIComponent(out.show_id);
      else load();
    } catch (err) { toast(err.message, true); }
    return;
  }
  const rgn = e.target.closest("[data-regen]");
  if (rgn && rgn.dataset.show) {
    e.preventDefault();
    regenerate(rgn.dataset.show, rgn.dataset.regen, rgn.dataset.char || "");
    return;
  }
  const cap = e.target.closest("[data-char-approve]");
  if (cap) {
    e.preventDefault();
    try {
      const out = await post("/api/gate0/decide", {card_id: cap.dataset.card, action: "approve", char: cap.dataset.charApprove});
      toast((out.messages || []).join("; ") || `Approved ${cap.dataset.charApprove}`);
      load();
    } catch (err) { toast(err.message, true); }
    return;
  }
  const crj = e.target.closest("[data-char-reject]");
  if (crj) {
    e.preventDefault();
    rejectModal(crj.dataset.card, crj.dataset.charReject);
    return;
  }
  const sca = e.target.closest("[data-scene-approve]");
  if (sca) {
    e.preventDefault();
    try {
      const out = await post("/api/gate0/decide", {card_id: sca.dataset.card, action: "approve", scene: sca.dataset.sceneApprove});
      toast((out.messages || []).join("; ") || `Approved ${sca.dataset.sceneApprove}`);
      load();
    } catch (err) { toast(err.message, true); }
    return;
  }
  const scr = e.target.closest("[data-scene-reject]");
  if (scr) {
    e.preventDefault();
    rejectModal(scr.dataset.card, "", scr.dataset.sceneReject);
    return;
  }
  const mrj = e.target.closest("[data-missing-reject]");
  if (mrj) {
    e.preventDefault();
    const isScene = mrj.dataset.missingReject === "scene";
    const preset = isScene
      ? `Regenerate missing scene ref: ${mrj.dataset.name || "?"}. `
      : `Regenerate missing refs: ${mrj.dataset.name || "?"}. `;
    rejectModal(mrj.dataset.card, isScene ? "" : (mrj.dataset.name || ""), isScene ? (mrj.dataset.name || "") : "", preset);
    return;
  }
  const rp = e.target.closest("[data-repitch]");
  if (rp) {
    e.preventDefault();
    repitchModal(rp.dataset.repitch);
    return;
  }
  const runFiles = e.target.closest("[data-run-files]");
  if (runFiles) {
    e.preventDefault();
    e.stopPropagation();
    const box = runFiles.closest("details")?.querySelector(".run-files");
    if (!box || box.dataset.loaded) return;
    box.innerHTML = '<div class="empty">Loading run files…</div>';
    try {
      const d = await api("/api/run/log?id=" + encodeURIComponent(runFiles.dataset.runFiles));
      box.innerHTML = runFilesHTML(d);
      box.dataset.loaded = "1";
    } catch (err) { box.innerHTML = `<div class="empty">${esc(err.message)}</div>`; }
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
        const out = await post("/api/gate0/decide", {card_id:id, action:"approve"});
        toast((out.messages || []).join("; ") || "Approved"); load();
      } else if (act.dataset.act === "approve-all") {
        const out = await post("/api/gate0/decide", {card_id:id, action:"approve", all:true});
        toast((out.messages || []).join("; ") || "Approved all"); load();
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

async function checkVersion() {
  try {
    const v = await api("/api/version");
    const known = S.versions;
    S.versions = v;
    if (!known) return;
    const changed = ["board_js", "api", "daemon"].filter(k => v[k] !== known[k]);
    if (changed.length && !document.getElementById("verbar")) {
      const bar = document.createElement("div");
      bar.id = "verbar";
      bar.innerHTML = `New dashboard code is available (${changed.join(", ")} changed). <button class="btn sm primary" id="verbar-reload">Reload now</button>`;
      Object.assign(bar.style, {position:"fixed", bottom:"12px", right:"12px", zIndex: 9999, background:"var(--panel, #222)", border:"2px solid var(--accent, #f90)", padding:"10px 14px", borderRadius:"8px", boxShadow:"0 4px 16px rgba(0,0,0,.4)"});
      document.body.appendChild(bar);
      document.getElementById("verbar-reload").addEventListener("click", () => location.reload());
    }
  } catch (_) {}
}

async function render() {
  checkVersion();
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
