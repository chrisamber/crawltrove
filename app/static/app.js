document.addEventListener('DOMContentLoaded', () => {
    // Mode tabs & UI components
    const modeScrape = document.getElementById('modeScrape');
    const modeCrawl = document.getElementById('modeCrawl');
    const modeExtract = document.getElementById('modeExtract');
    const scrapeOptions = document.getElementById('scrapeOptions');
    const crawlOptions = document.getElementById('crawlOptions');
    const extractOptions = document.getElementById('extractOptions');
    const actionBtn = document.getElementById('actionBtn');
    const btnText = document.getElementById('btnText');
    const btnSpinner = document.getElementById('btnSpinner');
    
    // Inputs
    const urlInput = document.getElementById('urlInput');
    const waitSlider = document.getElementById('waitSlider');
    const waitVal = document.getElementById('waitVal');
    const limitSlider = document.getElementById('limitSlider');
    const limitVal = document.getElementById('limitVal');
    const depthSlider = document.getElementById('depthSlider');
    const depthVal = document.getElementById('depthVal');
    const cleanContentToggle = document.getElementById('cleanContentToggle');
    const engineSelect = document.getElementById('engineSelect');
    const useSitemapToggle = document.getElementById('useSitemapToggle');
    const screenshotsToggle = document.getElementById('screenshotsToggle');
    const schemaInput = document.getElementById('schemaInput');
    const promptInput = document.getElementById('promptInput');
    const schemaError = document.getElementById('schemaError');

    // View States
    const idleState = document.getElementById('idleState');
    const crawlState = document.getElementById('crawlState');
    const outputState = document.getElementById('outputState');
    const errorState = document.getElementById('errorState');
    
    // Output content components
    const markdownOutput = document.getElementById('markdownOutput');
    const htmlOutput = document.getElementById('htmlOutput');
    const signalBar = document.getElementById('signalBar');
    const signalsTab = document.getElementById('signalsTab');
    const outputTitle = document.getElementById('outputTitle');
    const outputUrl = document.getElementById('outputUrl');
    
    // Crawl specific components
    const crawlStatusUrl = document.getElementById('crawlStatusUrl');
    const crawlPagesCount = document.getElementById('crawlPagesCount');
    const crawlErrorsCount = document.getElementById('crawlErrorsCount');
    const crawlProgressBar = document.getElementById('crawlProgressBar');
    const crawlProgressPercent = document.getElementById('crawlProgressPercent');
    const crawlProgressJobs = document.getElementById('crawlProgressJobs');
    const crawledList = document.getElementById('crawledList');
    const crawledCountHeader = document.getElementById('crawledCountHeader');
    const crawlAggregate = document.getElementById('crawlAggregate');
    const crawlExportRow = document.getElementById('crawlExportRow');

    // Action buttons & tabs
    const copyBtn = document.getElementById('copyBtn');
    const downloadBtn = document.getElementById('downloadBtn');
    const backBtn = document.getElementById('backBtn');
    const tabBtns = document.querySelectorAll('.tab-btn');
    const tabContents = document.querySelectorAll('.tab-content');
    const extractTabBtn = document.getElementById('extractTabBtn');
    const extractOutput = document.getElementById('extractOutput');

    let activeMode = 'scrape';
    let currentCrawlInterval = null;
    let crawlResultsData = []; // Store results of current crawl
    let currentScrapedData = null; // Store current page data (for copy/download)

    // Check API server health
    checkServerHealth();
    setInterval(checkServerHealth, 10000);

    async function checkServerHealth() {
        const statusIndicator = document.getElementById('statusIndicator');
        const statusText = document.getElementById('statusText');
        
        statusIndicator.className = 'status-indicator checking';
        statusText.textContent = 'Reconnecting...';

        try {
            const res = await fetch('/api/health');
            if (res.ok) {
                statusIndicator.className = 'status-indicator online';
                statusText.textContent = 'API Server Online';
            } else {
                throw new Error();
            }
        } catch (e) {
            statusIndicator.className = 'status-indicator offline';
            statusText.textContent = 'API Server Offline';
        }
    }

    // Toggle between Scrape / Crawl / Extract modes
    const MODES = {
        scrape:  { btn: modeScrape,  options: scrapeOptions,  label: 'Scrape Page' },
        crawl:   { btn: modeCrawl,   options: crawlOptions,   label: 'Crawl Site' },
        extract: { btn: modeExtract, options: extractOptions, label: 'Extract Data' },
    };
    function setMode(mode) {
        activeMode = mode;
        Object.keys(MODES).forEach(m => {
            MODES[m].btn.classList.toggle('active', m === mode);
            MODES[m].options.classList.toggle('hidden', m !== mode);
        });
        btnText.textContent = MODES[mode].label;
    }
    modeScrape.addEventListener('click', () => setMode('scrape'));
    modeCrawl.addEventListener('click', () => setMode('crawl'));
    modeExtract.addEventListener('click', () => setMode('extract'));

    // Schema presets for Extract mode
    const SCHEMA_PRESETS = {
        article: {
            type: 'object',
            properties: {
                title: { type: 'string' },
                author: { type: ['string', 'null'] },
                date: { type: ['string', 'null'] }
            },
            required: ['title', 'author', 'date']
        },
        product: {
            type: 'object',
            properties: {
                name: { type: 'string' },
                price: { type: ['number', 'null'] },
                currency: { type: ['string', 'null'] }
            },
            required: ['name', 'price', 'currency']
        },
        contact: {
            type: 'object',
            properties: {
                name: { type: 'string' },
                email: { type: ['string', 'null'] },
                phone: { type: ['string', 'null'] }
            },
            required: ['name', 'email', 'phone']
        }
    };
    document.querySelectorAll('.schema-preset').forEach(tag => {
        tag.addEventListener('click', (e) => {
            const preset = SCHEMA_PRESETS[e.target.getAttribute('data-preset')];
            if (!preset) return;
            schemaInput.value = JSON.stringify(preset, null, 2);
            clearSchemaError();
        });
    });

    function clearSchemaError() {
        schemaInput.classList.remove('input-error');
        schemaError.textContent = '';
        schemaError.classList.add('hidden');
    }
    schemaInput.addEventListener('input', clearSchemaError);

    // Real-time slider labels
    waitSlider.addEventListener('input', (e) => {
        waitVal.textContent = `${e.target.value} ms`;
    });

    limitSlider.addEventListener('input', (e) => {
        limitVal.textContent = `${e.target.value} pages`;
    });

    depthSlider.addEventListener('input', (e) => {
        depthVal.textContent = `${e.target.value} ${e.target.value === '1' ? 'hop' : 'hops'}`;
    });

    // Quick presets click handler (URL presets only — schema presets and
    // library filter chips share the .preset-tag look but not this behavior)
    document.querySelectorAll('.preset-tag[data-url]').forEach(tag => {
        tag.addEventListener('click', (e) => {
            urlInput.value = e.target.getAttribute('data-url');
        });
    });

    // Tab switcher
    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            tabBtns.forEach(b => b.classList.remove('active'));
            tabContents.forEach(c => c.classList.remove('active'));
            
            btn.classList.add('active');
            const tabId = btn.getAttribute('data-tab');
            document.getElementById(tabId).classList.add('active');
        });
    });

    // Clicking a signal chip jumps to its card in the Signals tab
    signalBar.addEventListener('click', (e) => {
        const chipEl = e.target.closest('.signal-chip');
        if (!chipEl) return;
        const signalsBtn = document.querySelector('.tab-btn[data-tab="signalsTab"]');
        if (signalsBtn) signalsBtn.click();
        const targetId = chipEl.getAttribute('data-target');
        const card = targetId && document.getElementById(targetId);
        if (card) {
            card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            card.classList.add('flash');
            setTimeout(() => card.classList.remove('flash'), 1200);
        }
    });

    // Main action button click handler
    actionBtn.addEventListener('click', async () => {
        const url = urlInput.value.trim();
        if (!url) {
            alert('Please enter a target URL');
            return;
        }

        // Clean UI state before running
        setLoading(true);
        showState('idle'); // clear previous content during request

        if (activeMode === 'scrape') {
            await handleScrape(url);
        } else if (activeMode === 'extract') {
            await handleExtract(url);
        } else {
            await handleCrawl(url);
        }
    });

    // Set Loading State
    function setLoading(isLoading) {
        if (isLoading) {
            actionBtn.disabled = true;
            btnSpinner.classList.remove('hidden');
            btnText.style.opacity = '0.7';
        } else {
            actionBtn.disabled = false;
            btnSpinner.classList.add('hidden');
            btnText.style.opacity = '1';
        }
    }

    // Switch view panel states
    function showState(state) {
        idleState.classList.add('hidden');
        crawlState.classList.add('hidden');
        outputState.classList.add('hidden');
        errorState.classList.add('hidden');

        if (state === 'idle') idleState.classList.remove('hidden');
        else if (state === 'crawl') crawlState.classList.remove('hidden');
        else if (state === 'output') outputState.classList.remove('hidden');
        else if (state === 'error') errorState.classList.remove('hidden');
    }

    // Scrape Operation
    async function handleScrape(url) {
        try {
            const response = await fetch('/api/scrape', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    url: url,
                    waitForMs: parseInt(waitSlider.value),
                    onlyMainContent: cleanContentToggle.checked,
                    engine: engineSelect.value
                })
            });

            const data = await response.json();
            
            if (!response.ok) {
                throw new Error(data.detail || 'Failed to scrape URL');
            }

            displayScrapedPage(data);
            showState('output');
        } catch (error) {
            showError('Scraping Error', error.message);
        } finally {
            setLoading(false);
        }
    }

    // Extract Operation (schema-constrained LLM extraction)
    async function handleExtract(url) {
        // Client-side schema validation — never send an unparsable schema.
        let schemaObj;
        try {
            schemaObj = JSON.parse(schemaInput.value);
            if (!schemaObj || typeof schemaObj !== 'object' || Array.isArray(schemaObj)) {
                throw new Error('schema must be a JSON object');
            }
            clearSchemaError();
        } catch (e) {
            schemaInput.classList.add('input-error');
            schemaError.textContent = `Invalid JSON schema: ${e.message}`;
            schemaError.classList.remove('hidden');
            setLoading(false);
            return;
        }

        try {
            const response = await fetch('/api/extract', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    url: url,
                    schema: schemaObj,
                    prompt: promptInput.value.trim(),
                    engine: engineSelect.value,
                    waitForMs: parseInt(waitSlider.value)
                })
            });

            const data = await response.json();

            if (!response.ok) {
                const detail = typeof data.detail === 'string'
                    ? data.detail
                    : JSON.stringify(data.detail || 'Failed to extract structured data');
                if (response.status === 501) {
                    showError('LLM backend not configured', detail);
                    return;
                }
                throw new Error(detail);
            }

            displayExtractResult(data);
            showState('output');
        } catch (error) {
            showError('Extraction Error', error.message);
        } finally {
            setLoading(false);
        }
    }

    // Display Extract Outcome (reuses the output viewer, "Extracted JSON" tab)
    function displayExtractResult(data) {
        const pretty = JSON.stringify(data.data, null, 2);
        // Copy/Save reuse the markdown path — hand them the pretty JSON.
        currentScrapedData = { title: 'extracted-data', markdown: pretty };

        outputTitle.textContent = 'Structured Data Extracted';
        outputUrl.innerHTML = `<i class="fa-solid fa-link"></i> ${esc(data.url || '')}`;

        extractOutput.textContent = pretty;
        markdownOutput.textContent = '*Extract mode — structured output is in the Extracted JSON tab.*';
        htmlOutput.textContent = '<!-- Extract mode: /api/extract does not return raw HTML -->';

        // Signal bar + Signals tab from response metadata (same path as scrape)
        const sig = Signals.normalizeSignals({ url: data.url, metadata: data.metadata || {} });
        signalBar.innerHTML = Signals.renderSignalBar(sig);
        signalsTab.innerHTML = Signals.renderSignalsTab(sig, { url: data.url });

        extractTabBtn.classList.remove('hidden');
        extractTabBtn.click();
    }

    // Crawl Operation
    async function handleCrawl(url) {
        // Reset crawl stats
        crawlPagesCount.textContent = '0';
        crawlErrorsCount.textContent = '0';
        crawlProgressBar.style.width = '0%';
        crawlProgressPercent.textContent = '0% Complete';
        crawlProgressJobs.textContent = 'Job ID: Init...';
        crawlStatusUrl.textContent = `Target: ${url}`;
        crawledList.innerHTML = '';
        crawledCountHeader.textContent = '0';
        crawlAggregate.innerHTML = '';
        crawlAggregate.classList.add('hidden');
        crawlExportRow.innerHTML = '';
        crawlExportRow.classList.add('hidden');
        crawlResultsData = [];
        
        showState('crawl');

        try {
            const response = await fetch('/api/crawl', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    url: url,
                    limit: parseInt(limitSlider.value),
                    maxDepth: parseInt(depthSlider.value),
                    onlyMainContent: cleanContentToggle.checked,
                    engine: engineSelect.value,
                    useSitemap: useSitemapToggle.checked,
                    screenshots: screenshotsToggle.checked
                })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.detail || 'Failed to start crawling');
            }

            const jobId = data.jobId;
            crawlProgressJobs.textContent = `Job ID: ${jobId.substring(0, 8)}...`;
            
            // Poll job status
            pollCrawlStatus(jobId);
        } catch (error) {
            showError('Crawling Initialization Failed', error.message);
            setLoading(false);
        }
    }

    const terminalCrawlStatuses = new Set([
        'completed', 'partial', 'failed', 'cancelled', 'timed_out', 'interrupted'
    ]);
    const activeCrawlPageStates = new Set([
        'pending', 'leased', 'retry_wait', 'waiting_input'
    ]);

    function isTerminalCrawlStatus(status) {
        return terminalCrawlStatuses.has(status);
    }

    function normalizeCrawlPage(page) {
        const metadata = page && page.metadata && typeof page.metadata === 'object'
            ? page.metadata
            : {};
        return {
            ...page,
            url: page.final_url || page.original_url || page.normalized_url ||
                page.url || metadata.url || '',
            title: page.title || metadata.title || '',
            markdown: typeof page.markdown === 'string' ? page.markdown : '',
            html: typeof page.html === 'string' ? page.html : '',
            metadata: metadata
        };
    }

    function crawlPageKey(page) {
        return Number.isFinite(page.discovery_seq)
            ? `sequence:${page.discovery_seq}`
            : `url:${page.final_url || page.original_url || page.normalized_url || page.url || ''}`;
    }

    function mergeCrawlPages(existing, incoming) {
        const pages = new Map(existing.map(page => [crawlPageKey(page), page]));
        incoming.forEach(update => {
            const key = crawlPageKey(update);
            const previous = pages.get(key);
            const combined = {
                ...(previous || {}),
                ...update,
                metadata: {
                    ...((previous && previous.metadata) || {}),
                    ...((update && update.metadata) || {})
                }
            };
            if (previous && (update.markdown === undefined || update.markdown === null)) {
                combined.markdown = previous.markdown;
            }
            if (previous && (update.html === undefined || update.html === null)) {
                combined.html = previous.html;
            }
            pages.set(key, normalizeCrawlPage(combined));
        });
        return Array.from(pages.values()).sort((left, right) => {
            const leftSequence = Number.isFinite(left.discovery_seq)
                ? left.discovery_seq
                : Number.MAX_SAFE_INTEGER;
            const rightSequence = Number.isFinite(right.discovery_seq)
                ? right.discovery_seq
                : Number.MAX_SAFE_INTEGER;
            return leftSequence - rightSequence || left.url.localeCompare(right.url);
        });
    }

    function nextCrawlPageCursor(pages, serverCursor) {
        const unresolved = pages
            .filter(page => Number.isFinite(page.discovery_seq) && activeCrawlPageStates.has(page.state))
            .map(page => page.discovery_seq);
        if (unresolved.length) {
            const first = Math.min(...unresolved);
            return first <= 0 ? null : first - 1;
        }
        return Number.isFinite(serverCursor) ? serverCursor : null;
    }

    function nextCrawlPageDrainCursor(requestedAfter, nextAfter, batchSize,
                                      capturedResults, expectedResults) {
        if (batchSize === 0) return null;
        if (expectedResults !== null && capturedResults >= expectedResults) return null;
        if (!Number.isFinite(nextAfter)) return null;
        if (requestedAfter !== null && nextAfter <= requestedAfter) return null;
        return nextAfter;
    }

    function isCapturedCrawlPage(page) {
        return page.state === 'succeeded' || page.markdown.length > 0;
    }

    function crawlProgress(job) {
        if (isTerminalCrawlStatus(job.status)) return 1;
        if (Number.isFinite(job.progress)) {
            return Math.max(0, Math.min(1, job.progress > 1 ? job.progress / 100 : job.progress));
        }
        const discovered = Number(job.discovered_count) || 0;
        const terminal = Number(job.terminal_count) || 0;
        return discovered > 0 ? Math.max(0, Math.min(1, terminal / discovered)) : 0;
    }

    async function fetchCrawlPageBatch(jobId, after) {
        const query = after === null ? '?limit=100' : `?after=${after}&limit=100`;
        const response = await fetch(`/api/crawl/${encodeURIComponent(jobId)}/pages${query}`);
        if (!response.ok) throw new Error('Failed to retrieve crawled pages');
        return response.json();
    }

    // Poll crawl status and page bodies independently. Durable status responses
    // intentionally contain counters, not an ever-growing inline results array.
    function pollCrawlStatus(jobId) {
        if (currentCrawlInterval) clearTimeout(currentCrawlInterval);
        let pageCursor = null;
        let pageRows = [];

        const poll = async () => {
            let continuePolling = true;
            currentCrawlInterval = null;
            try {
                const response = await fetch(`/api/crawl/${encodeURIComponent(jobId)}`);
                if (!response.ok) {
                    throw new Error('Failed to retrieve crawl job state');
                }

                const job = await response.json();
                const requestedAfter = pageCursor;
                const pageBatch = await fetchCrawlPageBatch(jobId, requestedAfter);
                const batchPages = Array.isArray(pageBatch.pages) ? pageBatch.pages : [];
                const inlineFallback = pageRows.length === 0 && batchPages.length === 0 && Array.isArray(job.results)
                    ? job.results
                    : [];
                pageRows = mergeCrawlPages(pageRows, batchPages.length ? batchPages : inlineFallback);

                let results = pageRows.filter(isCapturedCrawlPage);
                const expectedResults = Number.isFinite(job.resultCount) ? job.resultCount : null;
                if (isTerminalCrawlStatus(job.status)) {
                    let drainAfter = null;
                    let drainBatch = requestedAfter === null
                        ? pageBatch
                        : await fetchCrawlPageBatch(jobId, null);
                    let drainedPages = [];
                    for (let batchIndex = 0; batchIndex < 100; batchIndex += 1) {
                        const rows = Array.isArray(drainBatch.pages) ? drainBatch.pages : [];
                        drainedPages = mergeCrawlPages(drainedPages, rows);
                        const nextAfter = nextCrawlPageDrainCursor(
                            drainAfter,
                            drainBatch.nextAfter,
                            rows.length,
                            drainedPages.filter(isCapturedCrawlPage).length,
                            expectedResults
                        );
                        if (nextAfter === null) break;
                        drainAfter = nextAfter;
                        drainBatch = await fetchCrawlPageBatch(jobId, drainAfter);
                    }
                    pageRows = mergeCrawlPages(pageRows, drainedPages);
                    pageCursor = nextCrawlPageCursor(pageRows, drainBatch.nextAfter);
                    results = pageRows.filter(isCapturedCrawlPage);
                } else {
                    pageCursor = nextCrawlPageCursor(pageRows, pageBatch.nextAfter);
                }

                const errors = Array.isArray(job.errors) ? job.errors : [];
                crawlPagesCount.textContent = results.length;
                crawlErrorsCount.textContent = errors.length;
                crawledCountHeader.textContent = results.length + errors.length;

                const percent = Math.round(crawlProgress(job) * 100);
                crawlProgressBar.style.width = `${percent}%`;
                crawlProgressPercent.textContent = `${percent}% Complete`;

                updateCrawlLogs(results, errors);
                crawlResultsData = results;

                if (results.length) {
                    crawlAggregate.innerHTML = Signals.renderAggregate(Signals.summarizeCrawl(results));
                    crawlAggregate.classList.remove('hidden');
                }

                if (isTerminalCrawlStatus(job.status)) {
                    continuePolling = false;
                    setLoading(false);
                    renderCrawlExport(job, jobId);

                    if (results.length > 0) {
                        displayScrapedPage(results[0]);
                        const meta = document.getElementById('outputMetaText') || document.createElement('span');
                        meta.id = 'outputMetaText';
                        meta.innerHTML = `<i class="fa-solid fa-list-check text-success"></i> Crawl ${esc(job.status)}. Scraped ${results.length} pages. Click a list item to view another page.`;
                        outputUrl.innerHTML = '';
                        outputUrl.appendChild(meta);
                        showState('output');
                    } else {
                        showError(`Crawl ${job.status || 'failed'}`, 'No pages were successfully crawled.');
                    }
                }
            } catch (error) {
                continuePolling = false;
                showError('Crawl Error', error.message);
                setLoading(false);
            } finally {
                if (continuePolling) {
                    currentCrawlInterval = setTimeout(poll, 1200);
                }
            }
        };

        poll();
    }

    // Export links for a finished crawl. File artifact links render whenever
    // the job carries an artifact_stem; the /api/export.* buttons are strictly
    // best-effort — the DB is optional, so a 503 (or any failure) from
    // /api/runs silently omits them, mirroring the backend invariant.
    async function renderCrawlExport(job, jobId) {
        let html = '';
        if (job.artifact_stem) {
            const stem = encodeURIComponent(job.artifact_stem);
            html +=
                `<span class="export-label"><i class="fa-solid fa-file-export"></i> Export:</span>` +
                `<a class="export-link" href="/data/crawls/${stem}.json" target="_blank" rel="noopener"><i class="fa-solid fa-file-code"></i> Artifact JSON</a>` +
                `<a class="export-link" href="/data/crawls/${stem}.md" target="_blank" rel="noopener"><i class="fa-brands fa-markdown"></i> Artifact MD</a>`;
        }
        try {
            const res = await fetch('/api/runs?limit=50');
            if (res.ok) {
                const runs = (await res.json()).runs || [];
                const run = runs.find(r => r.externalId === jobId);
                if (run) {
                    html +=
                        `<a class="export-link" href="/api/export.csv?runId=${encodeURIComponent(run.id)}" target="_blank" rel="noopener"><i class="fa-solid fa-table"></i> Export CSV</a>` +
                        `<a class="export-link" href="/api/export.json?runId=${encodeURIComponent(run.id)}" target="_blank" rel="noopener"><i class="fa-solid fa-download"></i> Export JSON</a>`;
                }
            }
        } catch (e) {
            // DB-optional: no DB, no export buttons, no error surfaced.
        }
        if (html) {
            crawlExportRow.innerHTML = html;
            crawlExportRow.classList.remove('hidden');
        }
    }

    // Update the visual list log in crawlState
    function updateCrawlLogs(successes, errors) {
        // Keep track of rendered URLs to prevent duplicating DOM elements
        const existingUrls = Array.from(crawledList.querySelectorAll('.crawled-item-url')).map(el => el.textContent);
        
        successes.forEach((item, index) => {
            if (!existingUrls.includes(item.url)) {
                const li = document.createElement('li');
                li.className = 'crawled-item';
                li.style.cursor = 'pointer'; // Make it look clickable
                li.innerHTML = `
                    <div class="crawled-item-info">
                        <span class="crawled-item-title">${esc(item.title || 'No Title')}</span>
                        <span class="crawled-item-url">${esc(item.url)}</span>
                    </div>
                    <div class="crawled-item-meta">
                        <span class="row-chips">${Signals.renderRowChips(Signals.normalizeSignals(item))}</span>
                        <span class="crawled-item-badge badge-scraped">Scraped</span>
                    </div>
                `;
                
                // Allow user to click to view this page's results
                li.addEventListener('click', () => {
                    displayScrapedPage(item);
                    showState('output');
                });
                
                crawledList.appendChild(li);
            }
        });

        errors.forEach(err => {
            if (!existingUrls.includes(err.url)) {
                const li = document.createElement('li');
                li.className = 'crawled-item';
                li.innerHTML = `
                    <div class="crawled-item-info">
                        <span class="crawled-item-title text-error">Scraping Failed</span>
                        <span class="crawled-item-url">${esc(err.url)}</span>
                    </div>
                    <span class="crawled-item-badge badge-error">Failed</span>
                `;
                crawledList.appendChild(li);
            }
        });
    }

    // Display Scraped Outcome
    function displayScrapedPage(data) {
        currentScrapedData = data;

        extractTabBtn.classList.add('hidden');   // extract-only tab
        extractOutput.textContent = '';

        outputTitle.textContent = data.title || 'Page Scraped successfully';
        outputUrl.innerHTML = `<i class="fa-solid fa-link"></i> ${esc(data.url)}`;

        // Populate tabs
        markdownOutput.textContent = data.markdown || '*Empty Content*';
        htmlOutput.textContent = data.html || '<!-- Empty Content -->';
        
        // Corpus signals: always-on bar + full breakdown in the Signals tab
        const wordCount = data.markdown ? data.markdown.split(/\s+/).filter(Boolean).length : 0;
        const charCount = data.markdown ? data.markdown.length : 0;
        const sig = Signals.normalizeSignals(data);
        signalBar.innerHTML = Signals.renderSignalBar(sig);
        signalsTab.innerHTML = Signals.renderSignalsTab(sig, {
            title: data.title || (data.metadata && data.metadata.title) || '',
            description: data.description || (data.metadata && data.metadata.description) || '',
            url: data.url,
            wordCount: wordCount,
            charCount: charCount
        });

        // Switch to markdown tab by default
        tabBtns[0].click();
    }

    // HTML-escape dynamic text before innerHTML interpolation (same as jobs.js)
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
            ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    }

    // Error display
    function showError(heading, description) {
        document.getElementById('errorHeading').textContent = heading;
        document.getElementById('errorDesc').textContent = description;
        showState('error');
    }

    // Dismiss Error
    backBtn.addEventListener('click', () => {
        showState('idle');
    });

    // Copy Content to clipboard
    copyBtn.addEventListener('click', () => {
        if (!currentScrapedData || !currentScrapedData.markdown) return;
        
        navigator.clipboard.writeText(currentScrapedData.markdown)
            .then(() => {
                const origText = copyBtn.innerHTML;
                copyBtn.innerHTML = '<i class="fa-solid fa-check"></i> Copied!';
                copyBtn.style.color = 'var(--success)';
                setTimeout(() => {
                    copyBtn.innerHTML = origText;
                    copyBtn.style.color = '';
                }, 2000);
            })
            .catch(err => {
                alert('Could not copy text: ', err);
            });
    });

    // Save/Download output file
    downloadBtn.addEventListener('click', () => {
        if (!currentScrapedData || !currentScrapedData.markdown) return;

        const filename = (currentScrapedData.title || 'scraped-page')
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, '-')
            .replace(/(^-|-$)/g, '') + '.md';

        const blob = new Blob([currentScrapedData.markdown], { type: 'text/markdown;charset=utf-8;' });
        const link = document.createElement('a');
        
        if (navigator.msSaveBlob) { // IE 10+
            navigator.msSaveBlob(blob, filename);
        } else {
            link.href = URL.createObjectURL(blob);
            link.setAttribute('download', filename);
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        }
    });
});
