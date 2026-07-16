// Hybrid search box on the Library view. Fetches query-scoped facets and
// renders parent-diverse ranked hits linking to their artifacts. Same conventions as
// library.js: vanilla JS, esc() on every interpolated value. Self-contained —
// no cross-file globals. A 501 (no EMBEDDINGS_BASE_URL) shows a friendly hint.
document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('semanticForm');
    if (!form) return;

    const queryInput = document.getElementById('semanticQuery');
    const kindSelect = document.getElementById('semanticKind');
    const modeSelect = document.getElementById('semanticMode');
    const facetSelects = {
        namespace: document.getElementById('semanticNamespace'),
        bucket: document.getElementById('semanticBucket'),
        tier: document.getElementById('semanticTier'),
        framework: document.getElementById('semanticFramework'),
    };
    const btn = document.getElementById('semanticBtn');
    const msg = document.getElementById('semanticMsg');
    const list = document.getElementById('semanticList');

    form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const q = (queryInput.value || '').trim();
        if (!q) return;
        const kind = kindSelect.value;
        const mode = modeSelect.value;
        const params = new URLSearchParams({ q, k: '10', mode });
        if (kind) params.set('kind', kind);
        for (const [name, select] of Object.entries(facetSelects)) {
            if (select.value) params.set(name, select.value);
        }

        btn.disabled = true;
        setMsg('Searching…', false);
        list.innerHTML = '';
        try {
            const facetParams = new URLSearchParams({ q, mode });
            if (kind) facetParams.set('kind', kind);
            const [res, facetRes] = await Promise.all([
                fetch('/api/search/hybrid?' + params.toString()),
                fetch('/api/search/facets?' + facetParams.toString()),
            ]);
            if (res.status === 501) {
                setMsg('The selected search mode is not configured or available.', true);
                return;
            }
            if (!res.ok) throw new Error(await detailOf(res, 'Search failed'));
            const body = await res.json();
            if (facetRes.ok) {
                const facetBody = await facetRes.json();
                renderFacets(facetBody.facets || {});
            }
            render(body.results || []);
            setMsg(`${(body.results || []).length} result(s) for “${q}”`, false, true);
        } catch (e) {
            setMsg(esc(e.message), true);
        } finally {
            btn.disabled = false;
        }
    });

    kindSelect.addEventListener('change', () => {
        if (kindSelect.value && kindSelect.value !== 'corpus') {
            for (const select of Object.values(facetSelects)) select.value = '';
        }
        if ((queryInput.value || '').trim()) form.requestSubmit();
    });
    for (const select of [modeSelect, ...Object.values(facetSelects)]) {
        select.addEventListener('change', () => {
            if ((queryInput.value || '').trim()) form.requestSubmit();
        });
    }

    function renderFacets(facets) {
        const labels = {
            namespace: 'All namespaces', bucket: 'All license buckets',
            tier: 'All quality tiers', framework: 'All frameworks',
        };
        for (const [name, select] of Object.entries(facetSelects)) {
            const current = select.value;
            const values = facets[name] || {};
            select.innerHTML = `<option value="">${labels[name]}</option>`
                + Object.entries(values)
                    .sort((a, b) => a[0].localeCompare(b[0]))
                    .map(([value, count]) => `<option value="${esc(value)}">${esc(value)} (${esc(count)})</option>`)
                    .join('');
            if (Object.prototype.hasOwnProperty.call(values, current)) select.value = current;
        }
    }

    function render(hits) {
        if (!hits.length) {
            list.innerHTML = '<li class="empty-row">No matches — try a broader query, '
                + 'or run scripts/build_embeddings.py to backfill the index.</li>';
            return;
        }
        list.innerHTML = hits.map(h => {
            const links = [];
            if (h.md) links.push(`<a class="export-link" href="${esc(h.md)}" target="_blank" rel="noopener">md</a>`);
            if (h.json) links.push(`<a class="export-link" href="${esc(h.json)}" target="_blank" rel="noopener">json</a>`);
            const title = (h.meta && (h.meta.title || h.meta.query)) || h.url || h.ref;
            const score = (typeof h.score === 'number') ? h.score.toFixed(3) : '';
            const chunks = h.matchedChunkCount > 1
                ? `<span class="artifact-meta">${esc(h.matchedChunkCount)} matching chunks</span>` : '';
            return `
                <li class="crawled-item semantic-item">
                    <div class="crawled-item-info">
                        <span class="crawled-item-title">${esc(title)}</span>
                        <span class="semantic-snippet">${esc(h.snippet || '')}</span>
                        <span class="crawled-item-url">${esc(h.url || '')}</span>
                    </div>
                    <div class="crawled-item-meta">
                        <span class="artifact-meta">score ${esc(score)}</span>
                        ${chunks}
                        ${links.join('')}
                        <span class="crawled-item-badge kind-badge kind-${esc(h.kind)}">${esc(h.kind)}</span>
                    </div>
                </li>`;
        }).join('');
    }

    function setMsg(text, isError, autohide) {
        msg.textContent = text;
        msg.className = 'form-msg' + (isError ? ' text-error' : ' text-success');
        msg.classList.remove('hidden');
        if (autohide) setTimeout(() => msg.classList.add('hidden'), 4000);
    }

    async function detailOf(res, fallback) {
        try {
            const d = (await res.json()).detail;
            if (typeof d === 'string' && d) return d;
            if (d) return JSON.stringify(d);
        } catch (e) { /* non-JSON error body */ }
        return `${fallback} (HTTP ${res.status})`;
    }

    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
            ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    }
});
