// Jobs & Runs panels — list/create/trigger jobs and watch run status.
// Vanilla JS, same conventions as app.js: fetch the /api jobs+runs REST API,
// render into the existing glass-card shell. ponytail: poll the runs list on a
// timer instead of websockets — runs are low-frequency and the API is cheap.
document.addEventListener('DOMContentLoaded', () => {
    const viewScrape = document.getElementById('viewScrape');
    const viewJobs = document.getElementById('viewJobs');
    const viewLibrary = document.getElementById('viewLibrary');
    const viewCorpus = document.getElementById('viewCorpus');
    const scrapeView = document.getElementById('scrapeView');
    const jobsView = document.getElementById('jobsView');
    const libraryView = document.getElementById('libraryView');
    const corpusView = document.getElementById('corpusView');

    const jobName = document.getElementById('jobName');
    const jobUrl = document.getElementById('jobUrl');
    const jobKind = document.getElementById('jobKind');
    const jobSchedule = document.getElementById('jobSchedule');
    const createJobBtn = document.getElementById('createJobBtn');
    const createJobSpinner = document.getElementById('createJobSpinner');
    const jobFormMsg = document.getElementById('jobFormMsg');

    const jobsList = document.getElementById('jobsList');
    const jobsCount = document.getElementById('jobsCount');
    const refreshJobsBtn = document.getElementById('refreshJobsBtn');
    const runsList = document.getElementById('runsList');
    const runsFilterLabel = document.getElementById('runsFilterLabel');
    const clearRunsFilterBtn = document.getElementById('clearRunsFilterBtn');

    let runFilterJobId = null;   // null = all jobs
    let pollTimer = null;

    // --- view switching ------------------------------------------------------
    // Three-way toggle across Scrape/Crawl, Jobs & Runs, and Library. This
    // module still owns showView; library.js reacts via the 'viewchange' event.
    function showView(which) {
        const views = { scrape: scrapeView, jobs: jobsView, library: libraryView, corpus: corpusView };
        const btns = { scrape: viewScrape, jobs: viewJobs, library: viewLibrary, corpus: viewCorpus };
        Object.keys(views).forEach(k => {
            if (views[k]) views[k].classList.toggle('hidden', k !== which);
            if (btns[k]) btns[k].classList.toggle('active', k === which);
        });
        if (which === 'jobs') {
            loadJobs();
            loadRuns();
            startPolling();
        } else {
            stopPolling();
        }
        document.dispatchEvent(new CustomEvent('viewchange', { detail: { view: which } }));
    }
    viewScrape.addEventListener('click', () => showView('scrape'));
    viewJobs.addEventListener('click', () => showView('jobs'));
    if (viewLibrary) viewLibrary.addEventListener('click', () => showView('library'));
    if (viewCorpus) viewCorpus.addEventListener('click', () => showView('corpus'));

    function startPolling() {
        stopPolling();
        pollTimer = setInterval(loadRuns, 2500);  // refresh run statuses live
    }
    function stopPolling() {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = null;
    }

    // --- jobs ----------------------------------------------------------------
    async function loadJobs() {
        try {
            const res = await fetch('/api/jobs');
            if (!res.ok) throw new Error((await res.json()).detail || 'Failed to load jobs');
            renderJobs((await res.json()).jobs || []);
        } catch (e) {
            jobsList.innerHTML = `<li class="empty-row text-error">${esc(e.message)}</li>`;
        }
    }

    function renderJobs(jobs) {
        jobsCount.textContent = jobs.length;
        if (!jobs.length) {
            jobsList.innerHTML = '<li class="empty-row">No jobs yet — create one on the left.</li>';
            return;
        }
        jobsList.innerHTML = '';
        jobs.forEach(job => {
            const li = document.createElement('li');
            li.className = 'crawled-item job-item' + (job.id === runFilterJobId ? ' selected' : '');
            const sched = job.schedule ? `<span class="job-sched"><i class="fa-solid fa-clock"></i> ${esc(job.schedule)}</span>` : '';
            li.innerHTML = `
                <div class="crawled-item-info">
                    <span class="crawled-item-title">${esc(job.name || '(unnamed)')} <span class="job-kind">${esc(job.kind)}</span></span>
                    <span class="crawled-item-url">${esc(job.targetUrl || '')}</span>
                </div>
                <div class="crawled-item-meta">
                    ${sched}
                    <button class="action-btn-secondary btn-sm run-now" data-id="${job.id}"><i class="fa-solid fa-play"></i> Run</button>
                </div>`;
            // Click a row (not the Run button) to filter runs to this job.
            li.addEventListener('click', (ev) => {
                if (ev.target.closest('.run-now')) return;
                runFilterJobId = (runFilterJobId === job.id) ? null : job.id;
                applyRunFilter(job);
                renderJobs(jobs);
                loadRuns();
            });
            li.querySelector('.run-now').addEventListener('click', () => triggerRun(job.id));
            jobsList.appendChild(li);
        });
    }

    function applyRunFilter(job) {
        if (runFilterJobId === null) {
            runsFilterLabel.classList.add('hidden');
            clearRunsFilterBtn.classList.add('hidden');
        } else {
            runsFilterLabel.textContent = `· ${job.name || 'job ' + job.id}`;
            runsFilterLabel.classList.remove('hidden');
            clearRunsFilterBtn.classList.remove('hidden');
        }
    }

    clearRunsFilterBtn.addEventListener('click', () => {
        runFilterJobId = null;
        runsFilterLabel.classList.add('hidden');
        clearRunsFilterBtn.classList.add('hidden');
        loadJobs();
        loadRuns();
    });

    refreshJobsBtn.addEventListener('click', () => { loadJobs(); loadRuns(); });

    async function triggerRun(jobId) {
        try {
            const res = await fetch(`/api/jobs/${jobId}/run`, { method: 'POST' });
            if (!res.ok) throw new Error((await res.json()).detail || 'Failed to trigger run');
            loadRuns();  // the new pending run shows up immediately, then polls to completion
        } catch (e) {
            flashMsg(e.message, true);
        }
    }

    // --- runs ----------------------------------------------------------------
    async function loadRuns() {
        try {
            const qs = runFilterJobId !== null ? `?jobId=${runFilterJobId}` : '';
            const res = await fetch(`/api/runs${qs}`);
            if (!res.ok) throw new Error((await res.json()).detail || 'Failed to load runs');
            renderRuns((await res.json()).runs || []);
        } catch (e) {
            runsList.innerHTML = `<li class="empty-row text-error">${esc(e.message)}</li>`;
        }
    }

    function renderRuns(runs) {
        if (!runs.length) {
            runsList.innerHTML = '<li class="empty-row">No runs yet — trigger a job to see one here.</li>';
            return;
        }
        runsList.innerHTML = '';
        runs.forEach(run => {
            const li = document.createElement('li');
            li.className = 'crawled-item run-item';
            li.innerHTML = `
                <div class="crawled-item-info">
                    <span class="crawled-item-title">Run #${run.id} <span class="run-trigger">${esc(run.trigger || '')}</span></span>
                    <span class="crawled-item-url">${run.jobId ? 'job ' + run.jobId : 'ad-hoc'} · ${run.pagesCount || 0} pages · ${fmtTime(run.startedAt || run.createdAt)}</span>
                </div>
                <span class="crawled-item-badge run-badge run-${esc(run.status)}">${esc(run.status)}</span>`;
            runsList.appendChild(li);
        });
    }

    // --- create job ----------------------------------------------------------
    createJobBtn.addEventListener('click', async () => {
        const url = jobUrl.value.trim();
        if (!url) { flashMsg('Target URL is required.', true); return; }
        createJobBtn.disabled = true;
        createJobSpinner.classList.remove('hidden');
        try {
            const res = await fetch('/api/jobs', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: jobName.value.trim() || null,
                    kind: jobKind.value,
                    targetUrl: url,
                    schedule: jobSchedule.value.trim() || null,
                })
            });
            if (!res.ok) throw new Error((await res.json()).detail || 'Failed to create job');
            jobName.value = ''; jobUrl.value = ''; jobSchedule.value = '';
            flashMsg('Job created.', false);
            loadJobs();
        } catch (e) {
            flashMsg(e.message, true);
        } finally {
            createJobBtn.disabled = false;
            createJobSpinner.classList.add('hidden');
        }
    });

    function flashMsg(text, isError) {
        jobFormMsg.textContent = text;
        jobFormMsg.className = 'form-msg' + (isError ? ' text-error' : ' text-success');
        setTimeout(() => jobFormMsg.classList.add('hidden'), 4000);
    }

    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
            ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    }
    function fmtTime(iso) {
        if (!iso) return '—';
        const d = new Date(iso);
        return isNaN(d) ? iso : d.toLocaleTimeString();
    }
});
