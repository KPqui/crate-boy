/* Crate Boy Pocket - phone recorder for KP's library notes and DJ sets.
   Audio is kept in the phone's own storage (IndexedDB) until it has reached the private inbox on GitHub.
   No legend or word lists live in this public code: they're loaded from the private repo after connecting. */
"use strict";
const APP_VERSION = "1.2";
const CFG_KEY = "cb-cfg", LEG_KEY = "cb-legend";
const PART_MS = +(localStorage.getItem("cb-part-ms") || 5 * 60 * 1000), SLICE_MS = 5000, MIN_NOTE_MS = 600;
const $ = s => document.querySelector(s);
const S = { cfg: load(CFG_KEY), legend: load(LEG_KEY), view: "talk", stream: null, sending: false };

function load(k){ try { return JSON.parse(localStorage.getItem(k) || "null"); } catch(e){ return null; } }
function save(k, v){ try { v ? localStorage.setItem(k, JSON.stringify(v)) : localStorage.removeItem(k); } catch(e){} }
function toast(t, ms = 2600){ const e = $("#toast"); e.textContent = t; e.style.display = "block"; clearTimeout(e._t); e._t = setTimeout(() => e.style.display = "none", ms); }
function esc(s){ return String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function uid(){ return new Date().toISOString().replace(/[-:T.Z]/g, "").slice(0, 14) + Math.random().toString(36).slice(2, 6); }
function hms(ms){ const s = Math.floor(ms / 1000); return Math.floor(s / 3600) + ":" + String(Math.floor(s % 3600 / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0"); }
function buzz(p){ try { navigator.vibrate && navigator.vibrate(p); } catch(e){} }

/* ------------------------------------------------------------ storage (IndexedDB) */
let dbp = null;
function db(){
  return dbp || (dbp = new Promise((ok, no) => {
    const r = indexedDB.open("crateboy", 1);
    r.onupgradeneeded = () => { r.result.createObjectStore("items", { keyPath: "id" }); r.result.createObjectStore("chunks", { keyPath: "k" }); };
    r.onsuccess = () => ok(r.result); r.onerror = () => no(r.error);
  }));
}
async function tx(store, mode, fn){
  const d = await db();
  return new Promise((ok, no) => { const t = d.transaction(store, mode); const st = t.objectStore(store); const res = fn(st); t.oncomplete = () => ok(res && res.result !== undefined ? res.result : res); t.onerror = () => no(t.error); });
}
const putItem = it => tx("items", "readwrite", s => s.put(it));
const getItem = id => tx("items", "readonly", s => s.get(id));
const allItems = () => tx("items", "readonly", s => s.getAll());
const delItem = id => tx("items", "readwrite", s => s.delete(id));
const putChunk = (k, blob) => tx("chunks", "readwrite", s => s.put({ k, blob }));
async function chunksOf(prefix){
  const d = await db();
  return new Promise((ok, no) => {
    const out = []; const t = d.transaction("chunks", "readonly");
    const r = t.objectStore("chunks").openCursor(IDBKeyRange.bound(prefix, prefix + "￿"));
    r.onsuccess = () => { const c = r.result; if (c){ out.push(c.value); c.continue(); } };
    t.oncomplete = () => ok(out.sort((a, b) => a.k.localeCompare(b.k, undefined, { numeric: true })).map(x => x.blob));
    t.onerror = () => no(t.error);
  });
}
async function delChunks(prefix){
  const d = await db();
  return new Promise(ok => { const t = d.transaction("chunks", "readwrite");
    const r = t.objectStore("chunks").openCursor(IDBKeyRange.bound(prefix, prefix + "￿"));
    r.onsuccess = () => { const c = r.result; if (c){ c.delete(); c.continue(); } }; t.oncomplete = ok; });
}

/* ------------------------------------------------------------ microphone */
const MIME = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"].find(m => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "";
const EXT = MIME.includes("mp4") ? "m4a" : "webm";
async function mic(){
  if (S.stream && S.stream.getAudioTracks().some(t => t.readyState === "live")) return S.stream;
  S.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: true, autoGainControl: true } });
  meter(S.stream);
  return S.stream;
}
let analyser = null;
function meter(stream){
  try {
    const ac = new (window.AudioContext || window.webkitAudioContext)();
    analyser = ac.createAnalyser(); analyser.fftSize = 1024;
    ac.createMediaStreamSource(stream).connect(analyser);
    const buf = new Float32Array(analyser.fftSize);
    (function tick(){ analyser.getFloatTimeDomainData(buf); let s = 0; for (const v of buf) s += v * v;
      const db = 20 * Math.log10(Math.sqrt(s / buf.length) + 1e-9); $("#lvl").style.width = Math.max(0, Math.min(100, (db + 60) / 60 * 100)) + "%";
      requestAnimationFrame(tick); })();
  } catch(e){}
}

/* ------------------------------------------------------------ TALK: hold to record a note */
let note = null;
async function holdStart(e){
  e.preventDefault();
  if (note || rec) return;
  try { await mic(); } catch(err){ toast("Microphone blocked. Allow it in the browser settings."); return; }
  const chunks = []; const mr = new MediaRecorder(S.stream, MIME ? { mimeType: MIME, audioBitsPerSecond: 48000 } : undefined);
  note = { mr, chunks, t0: Date.now(), at: new Date().toISOString() };
  mr.ondataavailable = ev => ev.data.size && chunks.push(ev.data);
  mr.start(1000);
  $("#hold").classList.add("rec"); buzz(30);
  note.timer = setInterval(() => $("#hold-t").textContent = ((Date.now() - note.t0) / 1000).toFixed(1) + " s", 100);
}
async function holdEnd(e){
  e && e.preventDefault();
  if (!note) return;
  const n = note; note = null;
  clearInterval(n.timer); $("#hold").classList.remove("rec");
  await new Promise(ok => { n.mr.onstop = ok; n.mr.stop(); });
  const dur = Date.now() - n.t0;
  if (dur < MIN_NOTE_MS){ $("#hold-t").textContent = "too short"; buzz([20, 40, 20]); return; }
  buzz(60);
  const id = uid();
  await putChunk(id + ":0:0", new Blob(n.chunks, { type: MIME || "audio/webm" }));
  await putItem({ id, kind: "note", recordedAt: n.at, dur, mime: MIME, ext: EXT, status: "new" });
  $("#hold-t").textContent = "saved · " + (dur / 1000).toFixed(1) + " s";
  render(); sendAll();
}

/* ------------------------------------------------------------ SET: long recording in parts */
let rec = null, wake = null;
async function keepAwake(){ try { if ("wakeLock" in navigator) wake = await navigator.wakeLock.request("screen"); } catch(e){} }
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && rec) keepAwake(); });

async function setStart(resumeId){
  if (rec || note) return;
  try { await mic(); } catch(err){ toast("Microphone blocked. Allow it in the browser settings."); return; }
  let it = resumeId ? await getItem(resumeId) : null;
  if (!it){
    it = { id: uid(), kind: "set", name: $("#set-name").value.trim(), setStarted: new Date().toISOString(), parts: [], state: "recording", ext: EXT, mime: MIME };
  }
  it.state = "recording"; await putItem(it);
  rec = { id: it.id, paused: false };
  S.stream.getAudioTracks().forEach(t => t.onended = () => interrupted("The microphone stopped"));
  await keepAwake();
  await nextPart();
  $("#booth").classList.add("on"); boothTick();
  render();
}
async function nextPart(){
  const it = await getItem(rec.id);
  const no = it.parts.length + 1;
  it.parts.push({ no, started: new Date().toISOString(), ended: null, sent: false, final: false });
  await putItem(it);
  const mr = new MediaRecorder(S.stream, MIME ? { mimeType: MIME, audioBitsPerSecond: 48000 } : undefined);
  let seq = 0;
  mr.ondataavailable = async ev => { if (ev.data.size){ await putChunk(`${rec ? rec.id : it.id}:${String(no).padStart(4, "0")}:${String(seq++).padStart(5, "0")}`, ev.data); $("#b-save").textContent = "saved " + new Date().toLocaleTimeString().slice(0, 5); } };
  mr.onerror = () => interrupted("The recorder failed");
  mr.start(SLICE_MS);
  rec.mr = mr; rec.no = no; rec.partT0 = Date.now();
  clearTimeout(rec.rot); rec.rot = setTimeout(rotate, PART_MS);
}
async function endPart(final){
  if (!rec || !rec.mr) return;
  clearTimeout(rec.rot);
  const mr = rec.mr; rec.mr = null;
  if (mr.state !== "inactive") await new Promise(ok => { mr.onstop = ok; mr.stop(); });
  const it = await getItem(rec.id);
  const p = it.parts.find(x => x.no === rec.no);
  if (p){ p.ended = new Date().toISOString(); p.final = !!final; }
  await putItem(it);
}
async function rotate(){ if (!rec || rec.paused) return; await endPart(false); await nextPart(); sendAll(); }
async function setPause(){
  if (!rec) return;
  if (!rec.paused){ rec.paused = true; await endPart(false); $("#b-pause").textContent = "RESUME"; $("#b-rec").textContent = "❚❚ PAUSED"; sendAll(); }
  else { rec.paused = false; await nextPart(); $("#b-pause").textContent = "PAUSE"; $("#b-rec").textContent = "● REC"; }
}
async function setStop(){
  if (!rec) return;
  if (!confirm("Stop the set?")) return;
  if (!rec.paused) await endPart(true);
  else { const it = await getItem(rec.id); const last = it.parts[it.parts.length - 1]; if (last) last.final = true; await putItem(it); }
  const it = await getItem(rec.id); it.state = "done"; await putItem(it);
  rec = null; try { wake && wake.release(); } catch(e){}
  $("#booth").classList.remove("on"); buzz([60, 60, 60]); toast("Set saved. Sending it to the PC…");
  render(); sendAll();
}
async function interrupted(why){
  if (!rec) return;
  const id = rec.id; try { await endPart(false); } catch(e){}
  const it = await getItem(id); it.state = "interrupted"; await putItem(it);
  rec = null; $("#booth").classList.remove("on"); buzz([200, 100, 200]);
  toast(why + ". Tap RESUME to continue the same set.", 5000); render(); sendAll();
}
function boothTick(){
  if (!rec) return;
  getItem(rec.id).then(it => {
    $("#b-clock").textContent = hms(Date.now() - new Date(it.setStarted).getTime());
    $("#b-part").textContent = "part " + (rec ? rec.no : "") + (it.name ? " · " + it.name : "");
  });
  setTimeout(boothTick, 1000);
}
/* a set that was still "recording" when the app died */
async function checkInterrupted(){
  const items = await allItems();
  for (const it of items) if (it.kind === "set" && it.state === "recording" && !rec){
    for (const p of it.parts) if (!p.ended) p.ended = new Date().toISOString();
    it.state = "interrupted"; await putItem(it);
  }
}

/* ------------------------------------------------------------ GitHub (private repo) */
function gh(path, init = {}, accept){
  const c = S.cfg;
  return fetch("https://api.github.com/repos/" + encodeURIComponent(c.owner) + "/" + encodeURIComponent(c.repo) + path, Object.assign({ cache: "no-store" }, init, {
    headers: Object.assign({ Authorization: "Bearer " + c.token, Accept: accept || "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28" }, init.headers || {})
  }));
}
async function ghJSON(path, init){ const r = await gh(path, init); if (!r.ok){ const e = new Error(r.status + " " + (await r.text()).slice(0, 120)); e.status = r.status; throw e; } return r.json(); }
async function ensureInbox(){
  if (S.inboxOk) return;
  const r = await gh("/branches/inbox");
  if (r.status === 404){
    const repo = await ghJSON("");
    const ref = await ghJSON("/git/ref/heads/" + repo.default_branch);
    const c = await gh("/git/refs", { method: "POST", body: JSON.stringify({ ref: "refs/heads/inbox", sha: ref.object.sha }) });
    if (!c.ok && c.status !== 422) throw new Error("Could not create the inbox (" + c.status + ")");
  } else if (!r.ok) throw new Error("GitHub " + r.status);
  S.inboxOk = true;
}
function b64(blob){ return new Promise((ok, no) => { const fr = new FileReader(); fr.onload = () => ok(String(fr.result).split(",")[1]); fr.onerror = () => no(fr.error); fr.readAsDataURL(blob); }); }
async function putFile(path, content64, msg){
  const r = await gh("/contents/" + path, { method: "PUT", body: JSON.stringify({ message: msg, content: content64, branch: "inbox" }) });
  if (r.ok || r.status === 422) return;            // 422 = already there (resend after a crash)
  const e = new Error("Upload failed (" + r.status + ")"); e.status = r.status; throw e;
}
const utf8b64 = s => btoa(unescape(encodeURIComponent(s)));
async function loadLegend(){
  const r = await gh("/contents/legend.json");
  if (!r.ok) throw new Error(r.status === 404 ? "legend.json is not in the private repo yet" : "GitHub " + r.status);
  const j = await r.json();
  const txt = new TextDecoder().decode(Uint8Array.from(atob(j.content.replace(/\n/g, "")), c => c.charCodeAt(0)));
  S.legend = JSON.parse(txt); save(LEG_KEY, S.legend); renderLegend();
}

/* ------------------------------------------------------------ sending */
async function sendAll(){
  if (S.sending || !S.cfg || !navigator.onLine) return;
  S.sending = true; $("#net").textContent = "sending…";
  try {
    await ensureInbox();
    for (const it of await allItems()){
      if (it.kind === "note" && it.status !== "sent"){
        const blob = new Blob(await chunksOf(it.id + ":"), { type: it.mime || "audio/webm" });
        const base = `inbox/note_${it.id}`;
        await putFile(`${base}.${it.ext}`, await b64(blob), "note");
        await putFile(`${base}.json`, utf8b64(JSON.stringify({ kind: "note", id: it.id, recordedAt: it.recordedAt, durMs: it.dur, app: APP_VERSION })), "note meta");
        it.status = "sent"; it.sentAt = new Date().toISOString(); await putItem(it); render();
      }
      if (it.kind === "set"){
        let changed = false;
        for (const p of it.parts){
          if (p.sent || !p.ended) continue;
          const prefix = `${it.id}:${String(p.no).padStart(4, "0")}:`;
          const blobs = await chunksOf(prefix);
          if (!blobs.length){ p.sent = true; changed = true; continue; }
          const base = `inbox/set_${it.id}_p${String(p.no).padStart(3, "0")}`;
          await putFile(`${base}.${it.ext}`, await b64(new Blob(blobs, { type: it.mime || "audio/webm" })), "set part");
          const meta = { kind: "set", set: it.id, name: it.name || "Phone set", part: p.no, setStarted: it.setStarted, partStarted: p.started,
            offsetSec: (new Date(p.started) - new Date(it.setStarted)) / 1000, final: !!p.final, app: APP_VERSION };
          await putFile(`${base}.json`, utf8b64(JSON.stringify(meta)), "set part meta");
          p.sent = true; changed = true; await putItem(it); render();
        }
        if (changed) await putItem(it);
      }
    }
    $("#net").textContent = "✓ sent";
  } catch(e){
    $("#net").textContent = "⚠ not sent";
    if (e.status === 401) toast("GitHub says the token is wrong or expired (SETUP).");
  } finally { S.sending = false; }
}
async function clearSent(){
  if (!confirm("Delete sent recordings from the phone? They're safe on the PC once Crate Boy pulled them.")) return;
  for (const it of await allItems()){
    const done = it.kind === "note" ? it.status === "sent" : (it.state === "done" && it.parts.every(p => p.sent));
    if (done){ await delChunks(it.id + ":"); await delItem(it.id); }
  }
  render();
}
async function share(id){
  const it = await getItem(id); const files = [];
  if (it.kind === "note") files.push(new File(await chunksOf(id + ":"), `note_${id}.${it.ext}`, { type: it.mime }));
  else for (const p of it.parts){ const b = await chunksOf(`${id}:${String(p.no).padStart(4, "0")}:`); if (b.length) files.push(new File(b, `set_${id}_p${String(p.no).padStart(3, "0")}.${it.ext}`, { type: it.mime })); }
  if (navigator.canShare && navigator.canShare({ files })) navigator.share({ files }).catch(() => {});
  else toast("Sharing files isn't supported in this browser.");
}

/* ------------------------------------------------------------ render */
function show(v){ S.view = v; document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.v === v)); document.querySelectorAll(".view").forEach(e => e.classList.toggle("on", e.id === "v-" + v)); render(); }
async function render(){
  const items = (await allItems()).sort((a, b) => (b.recordedAt || b.setStarted).localeCompare(a.recordedAt || a.setStarted));
  const notes = items.filter(i => i.kind === "note"), sets = items.filter(i => i.kind === "set");
  const unsent = notes.filter(n => n.status !== "sent").length + sets.reduce((a, s) => a + s.parts.filter(p => !p.sent).length, 0);
  $("#qn").textContent = unsent ? "(" + unsent + ")" : "";
  const when = iso => { const d = new Date(iso); return d.toLocaleDateString([], { day: "2-digit", month: "2-digit" }) + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };
  const today = new Date().toDateString(), nToday = notes.filter(n => new Date(n.recordedAt).toDateString() === today);
  $("#last-note").textContent = nToday.length ? `${nToday.length} today · ${nToday.filter(n => n.status !== "sent").length ? "sending…" : "all sent ✓"}` : "";
  $("#sets").innerHTML = sets.map(s => {
    const sent = s.parts.filter(p => p.sent).length;
    const act = s.state === "interrupted" ? `<button class="btn sm dark" onclick="setStart('${s.id}')">RESUME</button>` : "";
    return `<div class="it"><span class="k">SET</span><span class="m">${esc(s.name || "set")} · ${when(s.setStarted)}<br><span class="muted">${s.parts.length} part(s) · ${sent} sent · ${s.state}</span></span>${act}<button class="btn sm" onclick="share('${s.id}')">SHARE</button></div>`;
  }).join("") || '<p class="muted">No sets yet.</p>';
  $("#queue").innerHTML = items.map(i => {
    const ok = i.kind === "note" ? i.status === "sent" : i.parts.every(p => p.sent) && i.state === "done";
    return `<div class="it"><span class="k">${i.kind.toUpperCase()}</span><span class="m">${when(i.recordedAt || i.setStarted)}${i.kind === "set" ? " · " + i.parts.length + " parts" : ""}</span><span class="s">${ok ? "SENT ✓" : "WAITING"}</span><button class="btn sm" onclick="share('${i.id}')">SHARE</button></div>`;
  }).join("") || '<p class="muted">Nothing recorded yet.</p>';
  const intr = sets.find(s => s.state === "interrupted");
  $("#resume").innerHTML = intr && !rec ? `<div class="banner">A set was interrupted. <button class="btn sm" onclick="setStart('${intr.id}')">RESUME IT</button></div>` : "";
  if (!S.cfg) $("#resume").innerHTML += '<div class="banner">Not connected yet: open SETUP. Recording works anyway, it sends later.</div>';
}
function renderLegend(){
  const L = S.legend;
  if (!L){ $("#legend").innerHTML = "<h2>LEGEND</h2><p>Connect in SETUP to load your legend from the private repo.</p>"; $("#b-legend").innerHTML = "";
    $("#t-legend").innerHTML = '<p class="muted" style="font-size:14px">Your legend shows up here once you\'re connected (SETUP).</p>'; return; }
  const groups = [["ENERGY", L.order.energy], ["VIBE", L.order.vibe], ["ELEMENTS", L.order.elem], ["ROLE", L.order.role]];
  $("#legend").innerHTML = "<h2>LEGEND</h2>" + groups.map(([g, o]) => `<h3>${g}</h3>` + [...o].map(c => `<div class="l"><b>${esc(c)}</b><span>${esc(L.legend[c] || "")}</span></div>`).join("")).join("") +
    `<h3>WRITING</h3><p>- = from … to … · &amp; = both at once · + = stronger · ! = very (rare)</p>`;
  $("#t-legend").innerHTML = groups.map(([g, o]) => `<h3>${g}</h3>` + [...o].map(c => `<div class="l" onclick="toast('${esc(c)} = ${esc((L.legend[c] || "").replace(/'/g, "’"))}')"><b>${esc(c)}</b><span>${esc(L.legend[c] || "")}</span></div>`).join("")).join("") +
    `<p>- from … to … · &amp; both at once · + stronger · ! very (rare)</p>`;
  $("#b-legend").innerHTML = groups.map(([g, o]) => [...o].map(c => `<div><b>${esc(c)}</b>${esc(L.short && L.short[c] || L.legend[c] || "")}</div>`).join("")).join("");
}

/* ------------------------------------------------------------ setup */
async function saveSetup(){
  const owner = $("#s-owner").value.trim(), repo = $("#s-repo").value.trim(), token = $("#s-token").value.trim() || (S.cfg && S.cfg.token);
  if (!owner || !repo || !token){ $("#s-status").textContent = "Fill in all three."; return; }
  S.cfg = { owner, repo, token }; S.inboxOk = false; $("#s-status").textContent = "Testing…";
  try {
    const info = await ghJSON("");
    if (!info.private){ $("#s-status").textContent = "That repo is PUBLIC. Use the private one."; return; }
    save(CFG_KEY, S.cfg); $("#s-token").value = "";
    try { await loadLegend(); $("#s-status").textContent = "Connected ✓ Legend loaded."; }
    catch(e){ $("#s-status").textContent = "Connected ✓ but: " + e.message; }
    render(); sendAll();
  } catch(e){ $("#s-status").textContent = e.status === 401 ? "Token wrong or expired." : e.status === 404 ? "Repo not found, or the token has no access to it." : "Failed: " + e.message; }
}

/* ------------------------------------------------------------ boot */
$("#tabs").onclick = e => { const b = e.target.closest("button"); if (b) show(b.dataset.v); };
const hb = $("#hold");
hb.addEventListener("pointerdown", holdStart); hb.addEventListener("pointerup", holdEnd); hb.addEventListener("pointercancel", holdEnd); hb.addEventListener("pointerleave", holdEnd);
hb.addEventListener("contextmenu", e => e.preventDefault());
$("#set-start").onclick = () => setStart();
$("#b-pause").onclick = setPause; $("#b-stop").onclick = setStop;
$("#send-now").onclick = () => { sendAll(); toast("Sending…"); };
$("#clear-sent").onclick = clearSent;
$("#s-save").onclick = saveSetup;
/* The grow app (tank-log) runs on the same site, so its saved key can be reused without copying it. */
const growKey = (load("tanklog-cfg") || {}).token;
if (growKey){ $("#s-grow").style.display = "block";
  $("#s-grow").onclick = () => { $("#s-token").value = growKey; saveSetup(); }; }
if (S.cfg){ $("#s-owner").value = S.cfg.owner; $("#s-repo").value = S.cfg.repo; $("#s-token").placeholder = "saved · paste a new one to replace"; }
$("#s-ver").textContent = "Crate Boy Pocket " + APP_VERSION + " · " + (MIME || "no recorder support in this browser");
window.addEventListener("online", sendAll);
window.addEventListener("beforeunload", e => { if (rec){ e.preventDefault(); e.returnValue = ""; } });
setInterval(sendAll, 20000);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
renderLegend();
checkInterrupted().then(render).then(sendAll);
if (S.cfg) loadLegend().catch(() => {});
