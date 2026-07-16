// Library view — research runs + saved artifacts. Same conventions as jobs.js:
// vanilla JS, esc() on every interpolated value, a 2.5s poll timer that only
// runs while the view is visible. jobs.js owns showView and dispatches a
// 'viewchange' CustomEvent; this module reacts to it (no cross-file globals).
document.addEventListener('DOMContentLoaded', () => {
    const libraryView = document.getElementById('libraryView');
    if (!libraryView) return;

    const researchList = document.getElementById('researchList');
    const researchCount = document.getElementById('researchCount');
    const refreshResearchBtn = document.getElementById('refreshResearchBtn');
    const libraryMsg = document.getElementById('libraryMsg');

    const artifactsList = document.getElementById('artifactsList');
    const artifactsCount = document.getElementById('artifactsCount');
    const refreshArtifactsBtn = document.getElementById('refreshArtifactsBtn');
    const artifactFilters = document.getElementById('artifactFilters');

    // Research statuses: terminal ones never change again; "interrupted" is
    // resumable; everything else (queued/planning/searching/reading/
    // synthesizing — or any status we don't know yet) is active → cancellable.
    const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled', 'interrupted'];

    let pollTimer = null;
    let artifactFilter = 'all';
    let artifactsCache = [];

    // --- view lifecycle (driven by jobs.js's showView) -------------------------
    document.addEventListener('viewchange', (ev) => {
        if (ev.detail && ev.detail.view === 'library') {
            loadResearch();
            loadArtifacts();
            startPolling();
        } else {
            stopPolling();
        }
    });

    function startPolling() {
        stopPolling();
        pollTimer = setInterval(loadResearch, 2500);   // live statuses while visible
    }
    function stopPolling() {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = null;
    }

    refreshResearchBtn.addEventListener('click', loadResearch);
    refreshArtifactsBtn.addEventListener('click', loadArtifacts);

    // --- research runs ---------------------------------------------------------
    async function loadResearch() {
        try {
            const res = await fetch('/api/research');
            if (!res.ok) throw new Error(await detailOf(res, 'Failed to load research runs'));
            renderResearch((await res.json()).jobs || []);
        } catch (e) {
            researchCount.textContent = '0';
            researchList.innerHTML = `<li class="empty-row text-error">${esc(e.message)}</li>`;
        }
    }

    function renderResearch(jobs) {
        researchCount.textContent = jobs.length;
        if (!jobs.length) {
            researchList.innerHTML = '<li class="empty-row">No research runs yet — POST /api/research to start one.</li>';
            return;
        }
        researchList.innerHTML = '';
        jobs.forEach(job => {
            const status = String(job.status || 'unknown');
            const li = document.createElement('li');
            li.className = 'crawled-item research-item';

            const counters =
                `${num(job.rounds_run)} rounds · ${num(job.pages_scraped)} pages · ${num(job.sources_count)} sources`;
            const when = fmtDate(job.start_time);

            const actions = [];
            if (status === 'interrupted') {
                actions.push(`<button class="action-btn-secondary btn-sm research-resume" data-id="${esc(job.job_id)}"><i class="fa-solid fa-play"></i> Resume</button>`);
            } else if (!TERMINAL_STATUSES.includes(status)) {
                actions.push(`<button class="action-btn-secondary btn-sm research-cancel" data-id="${esc(job.job_id)}"><i class="fa-solid fa-ban"></i> Cancel</button>`);
            }
            if (job.artifact_stem) {
                actions.push(`<a class="export-link" href="/data/research/${encodeURIComponent(job.artifact_stem)}.md" target="_blank" rel="noopener"><i class="fa-brands fa-markdown"></i> Report</a>`);
            }

            li.innerHTML = `
                <div class="crawled-item-info">
                    <span class="crawled-item-title">${esc(job.query || '(no query)')}</span>
                    <span class="crawled-item-url">${esc(counters)}${when ? ' · ' + esc(when) : ''}</span>
                </div>
                <div class="crawled-item-meta">
                    ${actions.join('')}
                    <span class="crawled-item-badge run-badge run-${esc(status)}">${esc(status)}</span>
                </div>`;

            const resumeBtn = li.querySelector('.research-resume');
            if (resumeBtn) resumeBtn.addEventListener('click', () => researchAction(job.job_id, 'resume'));
            const cancelBtn = li.querySelector('.research-cancel');
            if (cancelBtn) cancelBtn.addEventListener('click', () => researchAction(job.job_id, 'cancel'));

            researchList.appendChild(li);
        });
    }

    async function researchAction(jobId, action) {
        try {
            const res = await fetch(`/api/research/${encodeURIComponent(jobId)}/${action}`, { method: 'POST' });
            if (!res.ok) throw new Error(await detailOf(res, `Failed to ${action} research run`));
            flashMsg(`Research run ${action === 'resume' ? 'resumed' : 'cancellation requested'}.`, false);
            loadResearch();
        } catch (e) {
            flashMsg(e.message, true);
        }
    }

    // --- artifacts -------------------------------------------------------------
    async function loadArtifacts() {
        try {
            const res = await fetch('/api/artifacts');
            if (!res.ok) throw new Error(await detailOf(res, 'Failed to load artifacts'));
            artifactsCache = (await res.json()).artifacts || [];
            renderArtifacts();
        } catch (e) {
            artifactsCount.textContent = '0';
            artifactsList.innerHTML = `<li class="empty-row text-error">${esc(e.message)}</li>`;
        }
    }

    artifactFilters.addEventListener('click', (ev) => {
        const chip = ev.target.closest('.filter-chip');
        if (!chip) return;
        artifactFilter = chip.getAttribute('data-kind') || 'all';
        artifactFilters.querySelectorAll('.filter-chip').forEach(c =>
            c.classList.toggle('active', c === chip));
        renderArtifacts();
    });

    function renderArtifacts() {
        const items = artifactFilter === 'all'
            ? artifactsCache
            : artifactsCache.filter(a => a.kind === artifactFilter);
        artifactsCount.textContent = items.length;
        if (!items.length) {
            artifactsList.innerHTML = '<li class="empty-row">No artifacts' +
                (artifactFilter === 'all' ? ' yet — run a scrape or crawl.' : ' of this kind.') + '</li>';
            return;
        }
        artifactsList.innerHTML = items.map(a => {
            const links = [];
            if (a.md) links.push(`<a class="export-link" href="${esc(a.md)}" target="_blank" rel="noopener">md</a>`);
            if (a.json) links.push(`<a class="export-link" href="${esc(a.json)}" target="_blank" rel="noopener">json</a>`);
            const pages = (a.kind !== 'scrape' && a.pages != null) ? `${num(a.pages)} pages · ` : '';
            return `
                <li class="crawled-item artifact-item">
                    <div class="crawled-item-info">
                        <span class="crawled-item-title">${esc(a.title || a.url || a.stem)}</span>
                        <span class="crawled-item-url">${esc(a.url || '')}</span>
                    </div>
                    <div class="crawled-item-meta">
                        <span class="artifact-meta">${esc(pages + fmtBytes(a.bytes) + ' · ' + fmtDate(a.mtime))}</span>
                        ${links.join('')}
                        <span class="crawled-item-badge kind-badge kind-${esc(a.kind)}">${esc(a.kind)}</span>
                    </div>
                </li>`;
        }).join('');
    }

    // --- helpers ---------------------------------------------------------------
    async function detailOf(res, fallback) {
        try {
            const d = (await res.json()).detail;
            if (typeof d === 'string' && d) return d;
            if (d) return JSON.stringify(d);
        } catch (e) { /* non-JSON error body */ }
        return `${fallback} (HTTP ${res.status})`;
    }

    function flashMsg(text, isError) {
        libraryMsg.textContent = text;
        libraryMsg.className = 'form-msg' + (isError ? ' text-error' : ' text-success');
        setTimeout(() => libraryMsg.classList.add('hidden'), 4000);
    }

    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
            ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    }

    function num(v) {
        return (typeof v === 'number' && isFinite(v)) ? v : 0;
    }

    function fmtBytes(b) {
        if (typeof b !== 'number' || !isFinite(b)) return '—';
        if (b >= 1024 * 1024) return (b / (1024 * 1024)).toFixed(1) + ' MB';
        return (b / 1024).toFixed(1) + ' KB';
    }

    // Accepts an ISO string (research start_time) or a unix-seconds number
    // (artifact mtime); returns a locale-formatted date or '' when absent.
    function fmtDate(v) {
        if (v == null || v === '') return '';
        const d = typeof v === 'number' ? new Date(v * 1000) : new Date(v);
        return isNaN(d) ? String(v) : d.toLocaleString();
    }
});
