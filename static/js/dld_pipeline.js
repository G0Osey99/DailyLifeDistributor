/* Browser-streaming media pipeline.
 *
 * The browser holds the picked files (webkitdirectory File refs), matches
 * dates against the server (filenames only), then orchestrates a batched,
 * chunked upload: per batch of 4 dates it uploads the distinct physical files,
 * triggers a server batch-run, streams progress over SSE, and moves on. The
 * server deletes each batch's temp files, keeping the VPS disk bounded.
 */
(function () {
    "use strict";

    const BATCH_SIZE = 4;
    const CHUNK_BYTES = 90 * 1024 * 1024; // < Cloudflare's ~100 MB body cap

    // category -> Map(filename -> File)
    const fileStore = {};
    // iso date -> { categories: {cat: [names]}, metadata: {...} }
    let scanResults = {};
    const completedDates = new Set();
    let runActive = false;

    function $(sel) { return document.querySelector(sel); }
    function $all(sel) { return Array.from(document.querySelectorAll(sel)); }
    function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }

    // ── Folder pickers ───────────────────────────────────────────────────
    $all(".folder-picker").forEach((input) => {
        input.addEventListener("change", () => {
            const cat = input.dataset.category;
            const map = new Map();
            for (const f of input.files) {
                // webkitRelativePath includes the folder; key by basename.
                const name = (f.name || "").trim();
                if (name) map.set(name, f);
            }
            fileStore[cat] = map;
            const countEl = $(`[data-count="${cat}"]`);
            if (countEl) {
                countEl.textContent = map.size ? `${map.size} files` : "—";
                countEl.classList.toggle("ok", map.size > 0);
            }
        });
    });

    // ── Spreadsheet upload + column mapping ──────────────────────────────
    const sheetSel = $("#map-sheet");

    $("#spreadsheet-picker")?.addEventListener("change", async (e) => {
        const file = e.target.files[0];
        const status = $("#spreadsheet-status");
        if (!file) return;
        status.textContent = "Uploading…";
        const fd = new FormData();
        fd.append("file", file, file.name);
        try {
            const r = await fetch("/media/spreadsheet", { method: "POST", body: fd });
            const data = await r.json();
            if (!r.ok) { status.textContent = data.error || "Upload failed"; return; }
            status.textContent = `✓ ${file.name}`;
            sheetSel.innerHTML = "";
            (data.sheets || []).forEach((s) => {
                const o = document.createElement("option");
                o.value = s; o.textContent = s; sheetSel.appendChild(o);
            });
            $("#mapping-section").style.display = "block";
            await loadColumns();
            await loadExistingMapping();
        } catch (err) {
            status.textContent = "Upload failed";
        }
    });

    sheetSel?.addEventListener("change", loadColumns);

    async function loadColumns() {
        const sheet = sheetSel.value;
        if (!sheet) return;
        try {
            const r = await fetch(`/media/spreadsheet/columns?sheet=${encodeURIComponent(sheet)}`);
            const data = await r.json();
            const cols = data.columns || [];
            $all(".mapping-column").forEach((sel) => {
                const cur = sel.value;
                sel.innerHTML = '<option value="">—</option>';
                cols.forEach((c) => {
                    const o = document.createElement("option");
                    o.value = c; o.textContent = c; sel.appendChild(o);
                });
                if (cols.includes(cur)) sel.value = cur;
            });
        } catch (err) { /* ignore */ }
    }

    async function loadExistingMapping() {
        try {
            const r = await fetch("/media/mapping");
            const data = await r.json();
            const m = data.mapping || {};
            if (m.sheet_name && sheetSel.querySelector(`option[value="${CSS.escape(m.sheet_name)}"]`)) {
                sheetSel.value = m.sheet_name;
                await loadColumns();
            }
            $all(".mapping-column").forEach((sel) => {
                const v = m[sel.dataset.key];
                if (v) sel.value = v;
            });
        } catch (err) { /* ignore */ }
    }

    function buildMapping() {
        const mapping = { sheet_name: sheetSel.value };
        $all(".mapping-column").forEach((sel) => {
            if (sel.value) mapping[sel.dataset.key] = sel.value;
        });
        return mapping;
    }

    $("#save-mapping-btn")?.addEventListener("click", async () => {
        const mapping = buildMapping();
        try {
            await fetch("/media/mapping", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(mapping),
            });
            const saved = $("#mapping-saved");
            saved.textContent = "✓ saved";
            setTimeout(() => { saved.textContent = ""; }, 2000);
        } catch (err) { /* ignore */ }
    });

    // ── Scan: filenames → matched dates ──────────────────────────────────
    $("#scan-btn")?.addEventListener("click", async () => {
        // Persist the current mapping first so /media/scan can attach metadata.
        if ($("#mapping-section").style.display !== "none") {
            await fetch("/media/mapping", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(buildMapping()),
            }).catch(() => {});
        }
        const btn = $("#scan-btn");
        const spinner = $("#scan-spinner");
        btn.disabled = true; spinner.classList.add("visible");
        const categories = {};
        for (const [cat, map] of Object.entries(fileStore)) {
            categories[cat] = Array.from(map.keys());
        }
        try {
            const r = await fetch("/media/scan", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ categories }),
            });
            const data = await r.json();
            scanResults = data.dates || {};
            renderDates();
        } catch (err) {
            $("#date-results").innerHTML = '<div class="card"><p class="text-error">Match failed. Try again.</p></div>';
        } finally {
            btn.disabled = false; spinner.classList.remove("visible");
        }
    });

    function renderDates() {
        const container = $("#date-results");
        const dates = Object.keys(scanResults).sort();
        if (!dates.length) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">📭</div><p>No dated media matched. Check your folders and filenames.</p></div>';
            $("#select-area").style.display = "none";
            $("#customize-area").style.display = "none";
            return;
        }
        let html = '<div class="card"><h3 class="section-header">Matched dates</h3>';
        html += '<p class="text-muted text-sm mb-md">Select the dates to upload. Already-uploaded dates show as done.</p>';
        html += '<table><thead><tr><th style="width:40px;"><input type="checkbox" id="select-all"></th><th>Date</th><th>Media</th><th>Title</th></tr></thead><tbody>';
        for (const iso of dates) {
            const res = scanResults[iso];
            const cats = Object.keys(res.categories || {});
            const meta = res.metadata || {};
            const done = completedDates.has(iso);
            const title = meta.youtube_title || meta.episode_title || meta.podcast_title || "";
            html += `<tr>`;
            html += `<td>${done ? '✓' : `<input type="checkbox" class="date-cb" value="${esc(iso)}">`}</td>`;
            html += `<td><strong>${esc(iso)}</strong></td>`;
            html += `<td>${cats.map((c) => `<span class="badge badge-ok">${esc(c)}</span>`).join(" ")}</td>`;
            html += `<td class="text-sm">${esc(title)}</td>`;
            html += `</tr>`;
        }
        html += "</tbody></table></div>";
        container.innerHTML = html;
        $("#select-all")?.addEventListener("change", function () {
            $all(".date-cb").forEach((cb) => { cb.checked = this.checked; });
        });
        $("#select-area").style.display = "block";
        $("#customize-area").style.display = "none";  // re-scan resets the step
    }

    function selectedDates() {
        return $all(".date-cb").filter((cb) => cb.checked).map((cb) => cb.value);
    }

    function enabledPlatforms() {
        return $all(".platform-toggle").filter((cb) => cb.checked).map((cb) => cb.dataset.platform);
    }

    // ── Review & customize step (reuses the Review look in-page so the
    //    picked File objects stay in memory for the chunked upload) ───────
    function selById(iso, cls) {
        return $(`.${cls}[data-iso="${CSS.escape(iso)}"]`);
    }

    function buildCustomizeCards(dates) {
        const list = $("#customize-list");
        list.innerHTML = "";
        for (const iso of dates) {
            const meta = (scanResults[iso] && scanResults[iso].metadata) || {};
            const title = meta.shorts_title || meta.youtube_title || "";
            const desc = meta.description || "";
            const card = document.createElement("div");
            card.className = "card";
            card.style.marginBottom = "0";
            card.innerHTML =
                `<div style="font-weight:600;margin-bottom:8px;">${esc(iso)}</div>` +
                `<div class="mapping-field"><label>Title</label>` +
                `<input type="text" class="cust-title" data-iso="${esc(iso)}"></div>` +
                `<div class="cust-suggestions" data-iso="${esc(iso)}" style="display:flex;gap:6px;flex-wrap:wrap;margin:6px 0;"></div>` +
                `<div class="mapping-field"><label>Description</label>` +
                `<textarea class="cust-desc" data-iso="${esc(iso)}" rows="2"></textarea></div>`;
            list.appendChild(card);
            // Set values via .value (not innerHTML) so user content can't inject markup.
            selById(iso, "cust-title").value = title;
            selById(iso, "cust-desc").value = desc;
        }
    }

    async function autofillTitles(dates, platforms) {
        if (!platforms.includes("youtube_shorts")) return;  // only Shorts dates
        for (const iso of dates) {
            const input = selById(iso, "cust-title");
            if (!input || input.value.trim()) continue;       // only-if-blank
            const meta = (scanResults[iso] && scanResults[iso].metadata) || {};
            const transcript = (meta.transcript || "").trim();
            const box = selById(iso, "cust-suggestions");
            if (!transcript) continue;
            if (box) box.textContent = "suggesting title…";
            try {
                const r = await postJSON("/media/suggest-titles", { transcript, count: 5 });
                const sug = (r.ok && r.data.suggestions) || [];
                if (box) box.innerHTML = "";
                if (!sug.length) { if (box) box.textContent = "no suggestions"; continue; }
                if (!input.value.trim()) input.value = sug[0];   // fill the blank
                sug.forEach((s) => {
                    const chip = document.createElement("button");
                    chip.type = "button";
                    chip.className = "btn btn-sm btn-secondary";
                    chip.textContent = s;                         // textContent = safe
                    chip.addEventListener("click", () => { input.value = s; });
                    if (box) box.appendChild(chip);
                });
            } catch (_) {
                if (box) box.textContent = "suggestion unavailable";
            }
        }
    }

    function collectOverrides(platforms) {
        const ov = {};
        $all(".cust-title").forEach((inp) => {
            const v = inp.value.trim();
            if (!v) return;
            const iso = inp.dataset.iso;
            ov[iso] = ov[iso] || {};
            if (platforms.includes("youtube_shorts")) ov[iso].youtube_shorts_title = v;
            if (platforms.includes("youtube_video")) ov[iso].youtube_title = v;
        });
        $all(".cust-desc").forEach((t) => {
            const v = t.value.trim();
            if (!v) return;
            const iso = t.dataset.iso;
            ov[iso] = ov[iso] || {};
            ov[iso].description = v;
        });
        return ov;
    }

    $("#review-btn")?.addEventListener("click", () => {
        const dates = selectedDates();
        const platforms = enabledPlatforms();
        if (!dates.length) { alert("Select at least one date."); return; }
        if (!platforms.length) { alert("Enable at least one platform."); return; }
        buildCustomizeCards(dates);
        $("#select-area").style.display = "none";
        $("#customize-area").style.display = "block";
        // Fire-and-forget; fills blanks as suggestions return. Guard the
        // rejection so an unawaited failure can't become an unhandled rejection.
        autofillTitles(dates, platforms).catch((e) => console.error("autofill titles failed:", e));
    });

    // ── Upload orchestration (batched, chunked, SSE) ─────────────────────
    async function postJSON(url, body) {
        const r = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {}),
        });
        return { ok: r.ok, status: r.status, data: await r.json().catch(() => ({})) };
    }

    async function uploadFileChunks(runId, fileId, file) {
        const total = Math.max(1, Math.ceil(file.size / CHUNK_BYTES));
        for (let i = 0; i < total; i++) {
            const blob = file.slice(i * CHUNK_BYTES, Math.min(file.size, (i + 1) * CHUNK_BYTES));
            const fd = new FormData();
            fd.append("run_id", runId);
            fd.append("file_id", fileId);
            fd.append("chunk_index", String(i));
            fd.append("total_chunks", String(total));
            fd.append("data", blob, "chunk");
            const r = await fetch("/media/upload/chunk", { method: "POST", body: fd });
            if (!r.ok) {
                const data = await r.json().catch(() => ({}));
                throw new Error(data.error || `chunk ${i} failed (${r.status})`);
            }
        }
    }

    function logProgress(html) {
        const el = $("#upload-progress");
        if (el) el.insertAdjacentHTML("beforeend", html);
    }

    // Resolves to {ok}. ok is false on a stream/connection error or a
    // batch-level crash (an `error` event with no date/platform). Per-row
    // errors are logged but don't fail the batch (continue-and-report).
    function consumeStream(jobId) {
        return new Promise((resolve) => {
            let batchError = false;
            const es = new EventSource(`/upload/stream?job_id=${encodeURIComponent(jobId)}`);
            es.onmessage = (e) => {
                let d;
                try { d = JSON.parse(e.data); } catch (_) { return; }
                if (d.type === "success") {
                    logProgress(`<div class="text-sm" style="color:var(--ok)">✓ ${esc(d.date)} — ${esc(d.platform)}</div>`);
                } else if (d.type === "error") {
                    logProgress(`<div class="text-sm" style="color:var(--err)">✗ ${esc(d.date || "")} — ${esc(d.platform || "")}: ${esc(d.message || "")}</div>`);
                    if (!d.date && !d.platform) batchError = true;  // batch-level crash
                } else if (d.type === "skip") {
                    logProgress(`<div class="text-sm text-dim">↷ ${esc(d.date || "")} — ${esc(d.platform || "")} (skipped)</div>`);
                } else if (d.type === "needs_manual") {
                    logProgress(`<div class="text-sm" style="color:var(--warn)">⚠ ${esc(d.date || "")} — ${esc(d.platform || "")} needs manual action</div>`);
                }
                if (d.type === "done") { es.close(); resolve({ ok: !batchError }); }
            };
            es.onerror = () => {
                es.close();  // close immediately so it can't auto-reconnect
                logProgress('<div class="text-sm" style="color:var(--err)">⚠ Lost connection to the upload stream.</div>');
                resolve({ ok: false });
            };
        });
    }

    async function uploadBatch(runId, batchDates, platforms, overrides) {
        // Gather the distinct physical files needed for this batch.
        const fileIdByFile = new Map();   // File -> file_id
        const filesMap = {};              // file_id -> [{category, date}]
        for (const date of batchDates) {
            const res = scanResults[date];
            if (!res) continue;
            for (const [category, names] of Object.entries(res.categories || {})) {
                for (const name of names) {
                    const file = fileStore[category] && fileStore[category].get(name);
                    if (!file) continue;
                    let fid = fileIdByFile.get(file);
                    if (!fid) {
                        const res2 = await postJSON(`/media/file/new?run_id=${runId}`, {});
                        fid = res2.data.file_id;
                        fileIdByFile.set(file, fid);
                        logProgress(`<div class="text-sm text-dim">⬆ uploading ${esc(name)}…</div>`);
                        await uploadFileChunks(runId, fid, file);
                    }
                    (filesMap[fid] = filesMap[fid] || []).push({ category, date });
                }
            }
        }
        // Only this batch's date overrides.
        const batchOverrides = {};
        for (const d of batchDates) if (overrides && overrides[d]) batchOverrides[d] = overrides[d];
        const res = await postJSON("/media/batch/run", {
            run_id: runId, dates: batchDates, platforms, files: filesMap,
            overrides: batchOverrides,
        });
        if (!res.ok) {
            logProgress(`<div class="text-sm" style="color:var(--err)">Batch failed: ${esc(res.data.error || res.status)}</div>`);
            return { ok: false };
        }
        const result = await consumeStream(res.data.job_id);
        // Only mark dates done when the batch actually finished cleanly, so a
        // re-run retries them (the server's idempotent skip avoids dupes).
        if (result.ok) batchDates.forEach((d) => completedDates.add(d));
        return result;
    }

    $("#upload-btn")?.addEventListener("click", async () => {
        const dates = selectedDates();
        const platforms = enabledPlatforms();
        if (!dates.length) { alert("Select at least one date."); return; }
        if (!platforms.length) { alert("Enable at least one platform."); return; }
        const overrides = collectOverrides(platforms);

        const btn = $("#upload-btn");
        btn.disabled = true;
        $("#keep-open-warn").classList.add("visible");
        runActive = true;
        window.onbeforeunload = () => "An upload is in progress. Closing this tab will stop it.";

        let runId = null;
        try {
            const init = await postJSON("/media/run/init", {});
            if (!init.ok) {
                logProgress(`<div class="text-sm" style="color:var(--err)">${esc(init.data.error || "Could not start run")}</div>`);
                return;
            }
            runId = init.data.run_id;
            let aborted = false;
            for (let i = 0; i < dates.length; i += BATCH_SIZE) {
                const batch = dates.slice(i, i + BATCH_SIZE);
                logProgress(`<div class="text-sm" style="margin-top:8px;font-weight:600;">Batch ${Math.floor(i / BATCH_SIZE) + 1}: ${batch.join(", ")}</div>`);
                const result = await uploadBatch(runId, batch, platforms, overrides);
                if (!result || !result.ok) {
                    aborted = true;
                    logProgress('<div class="text-sm" style="color:var(--err);margin-top:8px;font-weight:600;">Upload stopped — fix the issue above and re-run; finished dates are skipped automatically.</div>');
                    break;
                }
            }
            if (!aborted) {
                logProgress('<div class="text-sm" style="color:var(--ok);margin-top:8px;font-weight:600;">All batches complete.</div>');
            }
        } catch (err) {
            logProgress(`<div class="text-sm" style="color:var(--err)">Upload error: ${esc(err.message || err)}</div>`);
        } finally {
            if (runId) await postJSON("/media/run/finish", { run_id: runId }).catch(() => {});
            runActive = false;
            window.onbeforeunload = null;
            btn.disabled = false;
            $("#keep-open-warn").classList.remove("visible");
            // Keep the customize view + progress log visible so the user sees
            // the outcome. Mark finished dates done in the (hidden) matched
            // table so a later "Match dates" / re-review reflects them, and
            // disable re-uploading the same batch from this view.
            $all(".date-cb").forEach((cb) => {
                if (completedDates.has(cb.value)) { cb.checked = false; cb.disabled = true; }
            });
        }
    });
})();
