// Corpus browser view. Reads GET /api/corpus + /api/corpus/stats, renders stat
// tiles, filter chips (target / namespace / tier), a substring filter, and a
// paginated record list. Links into the S1 semantic search scoped to
// kind=corpus. Same conventions as library.js/semantic.js: vanilla JS, esc().
document.addEventListener('DOMContentLoaded', () => {
    const corpusView = document.getElementById('corpusView');
    if (!corpusView) return;

    const totalEl = document.getElementById('corpusTotal');
    const tilesEl = document.getElementById('corpusStatTiles');
    const targetChips = document.getElementById('corpusTargetChips');
    const nsChips = document.getElementById('corpusNamespaceChips');
    const tierChips = document.getElementById('corpusTierChips');
    const form = document.getElementById('corpusForm');
    const queryInput = document.getElementById('corpusQuery');
    const semanticLink = document.getElementById('corpusSemanticLink');
    const msg = document.getElementById('corpusMsg');
    const list = document.getElementById('corpusList');
    const prevBtn = document.getElementById('corpusPrevBtn');
    const nextBtn = document.getElementById('corpusNextBtn');
    const pageLabel = document.getElementById('corpusPageLabel');
    const refreshBtn = document.getElementById('refreshCorpusBtn');

    const PAGE = 25;
    const filters = { target: '', namespace: '', tier: '', q: '' };
    let offset = 0;
    let loadedStats = false;

    document.addEventListener('viewchange', (ev) => {
        if (ev.detail && ev.detail.view === 'corpus') {
            if (!loadedStats) { loadStats(); loadedStats = true; }
            loadRecords();
        }
    });
    refreshBtn.addEventListener('click', () => { loadStats(); loadRecords(); });

    form.addEventListener('submit', (ev) => {
        ev.preventDefault();
        filters.q = (queryInput.value || '').trim();
        offset = 0;
        loadRecords();
    });
    prevBtn.addEventListener('click', () => { offset = Math.max(0, offset - PAGE); loadRecords(); });
    nextBtn.addEventListener('click', () => { offset += PAGE; loadRecords(); });
    semanticLink.addEventListener('click', (ev) => {
        // Hand off to the Library semantic search box, scoped to corpus.
        ev.preventDefault();
        const q = (queryInput.value || '').trim();
        document.getElementById('viewLibrary').click();
        const sq = document.getElementById('semanticQuery');
        const sk = document.getElementById('semanticKind');
        if (sq) sq.value = q;
        if (sk) sk.value = 'corpus';
        if (q) document.getElementById('semanticForm')
            .dispatchEvent(new Event('submit', { cancelable: true }));
    });

    async function loadStats() {
        try {
            const res = await fetch('/api/corpus/stats');
            if (!res.ok) throw new Error('stats HTTP ' + res.status);
            renderStats((await res.json()).stats || {});
        } catch (e) {
            tilesEl.innerHTML = `<span class="text-error">${esc(e.message)}</span>`;
        }
    }

    function renderStats(st) {
        totalEl.textContent = num(st.total);
        const tiles = [];
        for (const [k, v] of Object.entries(st.byTarget || {}))
            tiles.push(tile(k.toUpperCase(), v));
        for (const [k, v] of Object.entries(st.byTier || {}))
            tiles.push(tile('tier ' + k, v));
        tilesEl.innerHTML = tiles.join('');
        renderChips(targetChips, 'target', st.targets || []);
        renderChips(nsChips, 'namespace', st.namespaces || []);
        renderChips(tierChips, 'tier', (st.tiers || []).filter(t => t !== 'untiered').concat(
            (st.tiers || []).includes('untiered') ? ['untiered'] : []));
    }

    function tile(label, value) {
        return `<div class="corpus-tile"><span class="corpus-tile-num">${num(value)}</span>`
             + `<span class="corpus-tile-label">${esc(label)}</span></div>`;
    }

    function renderChips(container, key, values) {
        const all = `<span class="preset-tag filter-chip ${filters[key] === '' ? 'active' : ''}" data-val="">All</span>`;
        const chips = values.map(v =>
            `<span class="preset-tag filter-chip ${filters[key] === v ? 'active' : ''}" data-val="${esc(v)}">${esc(v)}</span>`);
        container.innerHTML = all + chips.join('');
        container.onclick = (ev) => {
            const chip = ev.target.closest('.filter-chip');
            if (!chip) return;
            filters[key] = chip.getAttribute('data-val') || '';
            container.querySelectorAll('.filter-chip').forEach(c =>
                c.classList.toggle('active', c === chip));
            offset = 0;
            loadRecords();
        };
    }

    async function loadRecords() {
        const params = new URLSearchParams({ offset: String(offset), limit: String(PAGE) });
        for (const k of ['target', 'namespace', 'tier', 'q'])
            if (filters[k]) params.set(k, filters[k]);
        try {
            const res = await fetch('/api/corpus?' + params.toString());
            if (!res.ok) throw new Error(await detailOf(res, 'Failed to load corpus'));
            const body = await res.json();
            renderRecords(body.items || []);
            prevBtn.disabled = offset === 0;
            nextBtn.disabled = !body.hasMore;
            const start = body.count ? offset + 1 : 0;
            pageLabel.textContent = `${start}–${offset + body.count}`;
            hideMsg();
        } catch (e) {
            setMsg(esc(e.message), true);
        }
    }

    function renderRecords(items) {
        if (!items.length) {
            list.innerHTML = '<li class="empty-row">No records — build the corpus '
                + '(scripts/build_corpus.py) or clear filters.</li>';
            return;
        }
        list.innerHTML = items.map(r => {
            const crumb = (r.headingPath || []).join(' › ');
            const badges = [
                `<span class="crawled-item-badge kind-badge">${esc(r.target)}</span>`,
                `<span class="crawled-item-badge">${esc(r.namespace || '')}</span>`,
                r.qualityTier ? `<span class="crawled-item-badge tier-${esc(r.qualityTier)}">${esc(r.qualityTier)}</span>` : '',
            ].join('');
            const fileLink = r.file ? `<a class="export-link" href="/${esc(r.file)}" target="_blank" rel="noopener">jsonl</a>` : '';
            return `
                <li class="crawled-item corpus-item">
                    <div class="crawled-item-info">
                        <span class="crawled-item-title">${esc(r.title || r.url || r.id)}</span>
                        ${crumb ? `<span class="corpus-crumb">${esc(crumb)}</span>` : ''}
                        <span class="semantic-snippet">${esc(r.snippet || '')}</span>
                        <span class="crawled-item-url">${esc(r.url || '')}</span>
                    </div>
                    <div class="crawled-item-meta">
                        ${badges}
                        ${fileLink}
                    </div>
                </li>`;
        }).join('');
    }

    function setMsg(text, isError) {
        msg.textContent = text;
        msg.className = 'form-msg' + (isError ? ' text-error' : ' text-success');
        msg.classList.remove('hidden');
    }
    function hideMsg() { msg.classList.add('hidden'); }

    async function detailOf(res, fallback) {
        try {
            const d = (await res.json()).detail;
            if (typeof d === 'string' && d) return d;
            if (d) return JSON.stringify(d);
        } catch (e) { /* non-JSON */ }
        return `${fallback} (HTTP ${res.status})`;
    }
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
            ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    }
    function num(v) { return (typeof v === 'number' && isFinite(v)) ? v : 0; }
});
