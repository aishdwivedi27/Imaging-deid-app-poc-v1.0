"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let state = { settings: null, records: [], job: null, es: null, totals: null };

// ---------------------------------------------------------------- helpers
function toast(msg, isError = false) {
  const t = $("toast");
  t.textContent = msg; t.className = "toast" + (isError ? " error" : ""); t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 4200);
}
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
}
const fmtTime = (iso) => { try { return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); } catch (_) { return iso; } };
const fmtDate = (d) => (/^\d{8}$/.test(d) ? `${d.slice(6)}/${d.slice(4, 6)}/${d.slice(0, 4)}` : d || "—");

// ---------------------------------------------------------------- status / settings
async function loadStatus() {
  const s = await api("/api/status");
  state.settings = s.settings;
  $("out-dir").value = s.settings.output_dir;
  $("out-abs").textContent = s.output_dir_abs;
  $("opt-ocr").checked = s.settings.ocr_enabled && s.ocr_available;
  $("opt-ocr").disabled = !s.ocr_available;
  $("opt-ner").checked = s.settings.ner_enabled && s.ner_available;
  $("opt-ner").disabled = !s.ner_available;
  setPill("pill-ocr", s.ocr_available, s.ocr_available ? "OCR ready" : "OCR not installed");
  setPill("pill-ner", s.ner_available, s.ner_available ? "Name detection ready" : "Name detection off");
  const oh = $("pill-ohif");
  if (s.ohif_url) { oh.href = s.ohif_url; oh.textContent = "Open OHIF viewer ↗"; oh.className = "pill ok"; }
  else { oh.removeAttribute("href"); oh.textContent = "OHIF not running"; oh.className = "pill off"; }
  document.querySelectorAll("#seg-date button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.v === s.settings.date_mode)));
  if (s.running_job && !state.job) follow(s.running_job);
}
function setPill(id, ok, text) { const p = $(id); p.textContent = text; p.className = "pill " + (ok ? "ok" : "off"); }
async function saveSettings(patch) {
  try {
    const s = await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
    $("out-abs").textContent = s.output_dir_abs;
    state.settings = s.settings;
    return true;
  } catch (e) { toast(e.message, true); return false; }
}
$("opt-ocr").addEventListener("change", (e) => saveSettings({ ocr_enabled: e.target.checked }));
$("opt-ner").addEventListener("change", (e) => saveSettings({ ner_enabled: e.target.checked }));
$("btn-save-out").addEventListener("click", async () => { if (await saveSettings({ output_dir: $("out-dir").value.trim() })) { toast("Output folder saved"); loadRecords(); } });
$("seg-date").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  document.querySelectorAll("#seg-date button").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
  saveSettings({ date_mode: b.dataset.v });
});

// ---------------------------------------------------------------- drag & drop / browse
const drop = $("drop");
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());
drop.addEventListener("drop", async (e) => {
  const items = [...(e.dataTransfer.items || [])].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  const files = items.length ? (await Promise.all(items.map((en) => walk(en, "")))).flat()
                             : [...e.dataTransfer.files].map((f) => ({ file: f, path: f.name }));
  upload(files);
});
drop.addEventListener("click", (e) => { if (!e.target.closest("button")) $("in-files").click(); });
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("in-files").click(); } });
$("btn-files").addEventListener("click", () => $("in-files").click());
$("btn-folder").addEventListener("click", () => $("in-folder").click());
$("in-files").addEventListener("change", (e) => { upload([...e.target.files].map((f) => ({ file: f, path: f.name }))); e.target.value = ""; });
$("in-folder").addEventListener("change", (e) => { upload([...e.target.files].map((f) => ({ file: f, path: f.webkitRelativePath || f.name }))); e.target.value = ""; });

function walk(entry, prefix) {
  return new Promise((resolve) => {
    if (entry.isFile) {
      entry.file((f) => resolve([{ file: f, path: prefix + f.name }]), () => resolve([]));
    } else if (entry.isDirectory) {
      const reader = entry.createReader(), all = [];
      const read = () => reader.readEntries(async (batch) => {
        if (!batch.length) {
          resolve((await Promise.all(all.map((c) => walk(c, prefix + entry.name + "/")))).flat());
        } else { all.push(...batch); read(); }
      }, () => resolve([]));
      read();
    } else resolve([]);
  });
}

function upload(items) {
  items = items.filter((i) => !i.file.name.startsWith("."));
  if (!items.length) return toast("No files found", true);
  const fd = new FormData();
  let bytes = 0;
  items.forEach((i) => { fd.append("files", i.file, i.file.name); fd.append("paths", i.path); bytes += i.file.size; });
  const bar = $("upload-bar"), fill = bar.querySelector("div"), label = bar.querySelector("span");
  bar.hidden = false; fill.style.width = "0";
  label.textContent = `Reading ${items.length} file${items.length > 1 ? "s" : ""} (${(bytes / 1048576).toFixed(1)} MB)…`;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) fill.style.width = (100 * e.loaded / e.total).toFixed(1) + "%"; };
  xhr.onload = () => {
    bar.hidden = true;
    if (xhr.status >= 200 && xhr.status < 300) follow(JSON.parse(xhr.responseText).job_id);
    else { let m = xhr.statusText; try { m = JSON.parse(xhr.responseText).detail; } catch (_) {} toast(m, true); }
  };
  xhr.onerror = () => { bar.hidden = true; toast("Upload failed — is the app still running?", true); };
  xhr.send(fd);
}

$("form-folder").addEventListener("submit", async (e) => {
  e.preventDefault();
  const path = $("folder-path").value.trim();
  if (!path) return toast("Enter a folder path", true);
  try { follow((await api("/api/process-folder", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) })).job_id); }
  catch (err) { toast(err.message, true); }
});

// ---------------------------------------------------------------- live job progress (SSE)
function follow(jobId) {
  if (state.es) state.es.close();
  state.job = jobId;
  state.totals = { records: 0, images: 0, ocr: 0, red: 0, review: 0, total: 0 };
  $("log").innerHTML = "";
  setJob("Starting…", "Indexing files by study", null);
  $("btn-cancel").hidden = false;
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  state.es = es;
  es.onmessage = (m) => handle(JSON.parse(m.data));
  es.onerror = () => { es.close(); };
}
function setJob(title, sub, pct) {
  $("job-title").textContent = title; $("job-sub").textContent = sub;
  const p = $("progress").parentElement;
  p.classList.toggle("indeterminate", pct === null);
  $("progress").style.width = pct === null ? "" : pct + "%";
}
function log(text, cls = "", at) {
  const li = document.createElement("li");
  li.className = cls;
  li.innerHTML = `<time>${fmtTime(at || new Date().toISOString())}</time>${esc(text)}`;
  $("log").prepend(li);
}
function renderStats() {
  const t = state.totals;
  $("st-records").textContent = t.records; $("st-images").textContent = t.images;
  $("st-ocr").textContent = t.ocr; $("st-red").textContent = t.red; $("st-review").textContent = t.review;
  $("st-review").parentElement.classList.toggle("on", t.review > 0);
}
function handle(ev) {
  const t = state.totals;
  switch (ev.type) {
    case "started":
      if (ev.ocr_note) log(ev.ocr_note, "rev", ev.at);
      if (ev.ner_note) log(ev.ner_note, "rev", ev.at);
      break;
    case "indexed": {
      t.total = ev.studies;
      const sk = Object.entries(ev.skipped || {}).map(([k, v]) => `${v} ${k}`).join(", ");
      log(`Found ${ev.files} files → ${ev.studies} studies, ${ev.reports} reports${sk ? " · skipped " + sk : ""}`, "", ev.at);
      setJob(ev.studies ? `Processing 0 of ${ev.studies}` : "Nothing new to process", ev.studies ? "One study at a time" : "All files were already processed or were not DICOM", ev.studies ? 0 : 100);
      break;
    }
    case "record_start":
      setJob(`Processing ${t.records + 1} of ${t.total}`, `De-identifying ${ev.record_id} · ${ev.images} file${ev.images > 1 ? "s" : ""}`, (100 * t.records / Math.max(t.total, 1)));
      break;
    case "record": {
      const r = ev.record;
      t.records = ev.done; t.images += r.n_images; t.ocr += r.ocr_regions_masked; t.red += r.report_redactions; t.review += r.qa_status !== "pass";
      renderStats();
      setJob(`Processing ${ev.done} of ${ev.total}`, `Last: ${r.record_id}`, 100 * ev.done / Math.max(ev.total, 1));
      const bits = [`${r.n_images} image${r.n_images !== 1 ? "s" : ""}`];
      if (r.ocr_regions_masked) bits.push(`${r.ocr_regions_masked} text region${r.ocr_regions_masked > 1 ? "s" : ""} masked`);
      if (r.report_present) bits.push(`report · ${r.report_redactions} redaction${r.report_redactions !== 1 ? "s" : ""}`);
      if (r.images_excluded) bits.push(`${r.images_excluded} excluded`);
      log(`${r.record_id}  ${r.modality} ${r.body_part || ""} — ${bits.join(", ")} — QA ${r.qa_status}`, r.qa_status === "pass" ? "ok" : "rev", ev.at);
      upsertRow(r, true);
      break;
    }
    case "record_error":
      log(`Error in one study: ${ev.error}`, "err", ev.at); break;
    case "cancelled":
      finish(`Stopped after ${ev.done} record${ev.done !== 1 ? "s" : ""}`, "You can run the same input again; finished files are skipped."); break;
    case "failed":
      log(ev.error, "err", ev.at); finish("Job failed", ev.error); break;
    case "finished": {
      const um = ev.unmatched_reports ? ` · ${ev.unmatched_reports} report(s) not matched to a study` : "";
      finish(ev.done ? `Done — ${ev.done} record${ev.done !== 1 ? "s" : ""} de-identified` : "Done", `Saved to ${ev.output_dir}${um}`);
      loadRecords();
      break;
    }
  }
}
function finish(title, sub) {
  setJob(title, sub, 100);
  $("btn-cancel").hidden = true;
  if (state.es) state.es.close();
}
$("btn-cancel").addEventListener("click", async () => {
  if (!state.job) return;
  await api(`/api/jobs/${state.job}/cancel`, { method: "POST" });
  $("btn-cancel").hidden = true; toast("Stopping after the current record");
});

// ---------------------------------------------------------------- records table
async function loadRecords() {
  try { state.records = await api("/api/records"); } catch (_) { state.records = []; }
  renderRows();
}
function rowHtml(r) {
  const ts = Date.now();
  return `<td class="thumb-cell"><img loading="lazy" src="/api/records/${r.record_id}/preview?t=${ts}" alt=""></td>
    <td><span class="rid">${esc(r.record_id)}</span><span class="sub mono">${esc(r.patient_id)}</span></td>
    <td><span class="chip">${esc(r.modality)}</span>${esc(r.body_part || "")}<span class="sub">${esc(r.study_description || "")} · ${esc(r.patient_age || "")} ${esc(r.patient_sex || "")}</span></td>
    <td class="num">${r.n_images}${r.images_excluded ? `<span class="sub">${r.images_excluded} excluded</span>` : ""}</td>
    <td>${r.report_present ? "Yes" : '<span class="muted">—</span>'}</td>
    <td class="num">${r.report_redactions}</td>
    <td class="num">${r.ocr_regions_masked}</td>
    <td><span class="badge ${r.qa_status === "pass" ? "pass" : "review"}">${r.qa_status === "pass" ? "Pass" : "Review"}</span></td>
    <td class="muted">${fmtTime(r.processed_at)}</td>`;
}
function matches(r, q) {
  return !q || [r.record_id, r.patient_id, r.modality, r.body_part, r.study_description, r.qa_status].join(" ").toLowerCase().includes(q);
}
function renderRows() {
  const q = $("filter").value.trim().toLowerCase();
  const rows = state.records.filter((r) => matches(r, q));
  $("rows").innerHTML = rows.map((r) => `<tr data-id="${esc(r.record_id)}">${rowHtml(r)}</tr>`).join("");
  $("rec-count").textContent = state.records.length;
  $("empty").hidden = rows.length > 0;
  $("empty").textContent = state.records.length ? "No records match the filter." : "No records yet. Processed studies appear here one by one.";
}
function upsertRow(r, fresh) {
  const i = state.records.findIndex((x) => x.record_id === r.record_id);
  if (i >= 0) state.records.splice(i, 1);
  state.records.unshift(r);
  renderRows();
  if (fresh) $("rows").querySelector(`tr[data-id="${r.record_id}"]`)?.classList.add("fresh");
}
$("filter").addEventListener("input", renderRows);
$("rows").addEventListener("click", (e) => { const tr = e.target.closest("tr"); if (tr) openDrawer(tr.dataset.id); });
$("btn-open").addEventListener("click", async () => {
  const r = await api("/api/open-output", { method: "POST" });
  if (!r.ok) toast(`Output folder: ${r.path}`);
});

// ---------------------------------------------------------------- drawer
async function openDrawer(id) {
  const r = state.records.find((x) => x.record_id === id); if (!r) return;
  $("d-id").textContent = r.record_id;
  $("d-img").src = `/api/records/${id}/preview?t=${Date.now()}`;
  const facts = [
    ["Coded patient", r.patient_id], ["Study", `${r.modality} · ${r.body_part || "—"}`], ["Description", r.study_description || "—"],
    ["Age / sex", `${r.patient_age || "—"} / ${r.patient_sex || "—"}`], ["Study date", fmtDate(r.study_date) + (state.settings?.date_mode === "shift" ? " (shifted)" : "")],
    ["Images", `${r.n_images}${r.images_excluded ? ` (${r.images_excluded} excluded: ${r.excluded_reasons})` : ""}`],
    ["Text masked", `${r.ocr_regions_masked} region(s)`],
    ["Report", r.report_present ? `${r.report_redactions} redaction(s) — ${Object.entries(r.report_redaction_breakdown || {}).map(([k, v]) => `${k.toLowerCase()} ${v}`).join(", ")}` : "none matched"],
    ["Folder", r.folder], ["Files", (r.images || []).join(", ")],
  ];
  $("d-facts").innerHTML = facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd class="${k === "Folder" || k === "Files" ? "mono" : ""}">${esc(v)}</dd>`).join("");
  $("d-findings").innerHTML = r.qa_findings ? `<div class="findings"><b>Held for review:</b> ${esc(r.qa_findings)}</div>` : "";
  $("d-report").textContent = r.report_present ? await api(`/api/records/${id}/report`) : "No report was matched to this study.";
  $("drawer").classList.add("open"); $("drawer").setAttribute("aria-hidden", "false"); $("scrim").hidden = false;
  $("d-close").focus();
}
function closeDrawer() { $("drawer").classList.remove("open"); $("drawer").setAttribute("aria-hidden", "true"); $("scrim").hidden = true; }
$("d-close").addEventListener("click", closeDrawer);
$("scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

// ---------------------------------------------------------------- boot
loadStatus().catch((e) => toast("Cannot reach the app: " + e.message, true));
loadRecords();
