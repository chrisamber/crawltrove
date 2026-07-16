/*
 * signals.js — corpus-signal rendering for the CrawlTrove dashboard.
 *
 * One focused module of PURE functions: signal-in → markup-out. No DOM queries,
 * no global state (mirrors the backend's one-module-per-signal convention). The
 * only export is the global `Signals` object, used by app.js.
 *
 * Handles both API response shapes:
 *   - single scrape  → signals nested under data.metadata.*
 *   - crawl result   → signals flattened onto the item (data.*), no html
 */
(function () {
  "use strict";

  // --- small helpers -------------------------------------------------------

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function safeExternalLink(value) {
    var label = (value === null || value === undefined) ? "" : String(value);
    try {
      var parsed = new URL(label, window.location.href);
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return esc(label);
      return '<a class="sig-link" href="' + esc(parsed.href) +
        '" target="_blank" rel="noopener noreferrer">' + esc(label) + "</a>";
    } catch (_error) {
      return esc(label);
    }
  }

  // Read a field preferring metadata.X, falling back to top-level X (?? semantics)
  function field(data, key) {
    var md = (data && data.metadata) ? data.metadata : {};
    var v = md[key];
    if (v === undefined || v === null) v = data ? data[key] : null;
    return (v === undefined) ? null : v;
  }

  // Map a quality failure category → the signals{} key it corresponds to,
  // so we can highlight the offending row in the breakdown table.
  var FAILURE_SIGNAL = {
    word_count: "n_words",
    avg_word_length: "avg_word_length",
    symbol_ratio: "symbol_word_ratio",
    bullet_lines: "bullet_line_fraction",
    ellipsis_lines: "ellipsis_line_fraction",
    alpha_words: "alpha_word_fraction",
    stop_words: "stop_word_hits",
    unpunctuated_lines: "punct_line_fraction",
    duplicate_lines: "dup_line_char_fraction",
    short_lines: "short_line_fraction"
  };

  var SIGNAL_ORDER = [
    "n_words", "n_lines", "avg_word_length", "symbol_word_ratio",
    "bullet_line_fraction", "ellipsis_line_fraction", "alpha_word_fraction",
    "stop_word_hits", "punct_line_fraction", "dup_line_char_fraction",
    "short_line_fraction"
  ];

  // --- normalization -------------------------------------------------------

  function normalizeSignals(data) {
    data = data || {};
    var q = field(data, "quality");
    var lg = field(data, "language");
    var lc = field(data, "license");
    var dd = field(data, "dedup");

    var failures = (q && Array.isArray(q.failures)) ? q.failures : [];

    // dedup state: needs a real content_hash to mean anything
    var dstate = null;
    if (dd && dd.content_hash) {
      if (dd.exact_duplicate_of) dstate = "exact";
      else if (dd.near_duplicate_of) dstate = "near";
      else dstate = "unique";
    }

    return {
      quality: {
        passed: (q && typeof q.passed === "boolean") ? q.passed : null,
        failures: failures,
        failureCount: failures.length,
        signals: (q && q.signals) ? q.signals : null
      },
      language: {
        lang: (lg && lg.lang) ? lg.lang : null,
        prob: (lg && typeof lg.prob === "number") ? lg.prob : null
      },
      license: {
        id: (lc && lc.id) ? lc.id : null,
        url: (lc && lc.url) ? lc.url : "",
        source: (lc && lc.source) ? lc.source : "",
        evidence: (lc && lc.evidence) ? lc.evidence : ""
      },
      dedup: {
        state: dstate,
        contentHash: dd && dd.content_hash ? dd.content_hash : "",
        exactOf: dd && dd.exact_duplicate_of ? dd.exact_duplicate_of : null,
        nearOf: dd && dd.near_duplicate_of ? dd.near_duplicate_of : null
      },
      provenance: {
        engine: field(data, "engine") || null,
        extractor: field(data, "extractor") || null
      }
    };
  }

  // --- chip helpers --------------------------------------------------------

  // state ∈ {ok, warn, err, muted}; target = id of card to scroll to on click
  function chip(state, target, inner, opts) {
    opts = opts || {};
    var cls = "signal-chip" + (state ? " " + state : "");
    var dot = opts.dot ? '<span class="dot ' + esc(state) + '"></span>' : "";
    return '<span class="' + cls + '" data-target="' + esc(target) + '" tabindex="0" role="button">' +
      dot + '<span class="chip-label">' + inner + "</span></span>";
  }

  function langLabel(language) {
    if (!language.lang) return null;
    var pct = (language.prob !== null) ? " · " + Math.round(language.prob * 100) + "%" : "";
    return esc(language.lang.toUpperCase()) + pct;
  }

  // --- signal bar (always-on) ---------------------------------------------

  function renderSignalBar(sig) {
    var chips = [];

    // Quality
    if (sig.quality.passed === null) {
      chips.push(chip("muted", "card-quality", "Quality —"));
    } else if (sig.quality.passed) {
      chips.push(chip("ok", "card-quality", "Quality", { dot: true }));
    } else {
      chips.push(chip("warn", "card-quality", "Quality · " + sig.quality.failureCount, { dot: true }));
    }

    // Language
    var ll = langLabel(sig.language);
    chips.push(ll ? chip("", "card-langprov", ll) : chip("muted", "card-langprov", "Lang —"));

    // License
    chips.push(sig.license.id
      ? chip("", "card-license", esc(sig.license.id))
      : chip("muted dashed", "card-license", "License —"));

    // Dedup
    var d = sig.dedup.state;
    if (d === "unique") chips.push(chip("ok", "card-dedup", "Unique", { dot: true }));
    else if (d === "near") chips.push(chip("warn", "card-dedup", "Near-dup", { dot: true }));
    else if (d === "exact") chips.push(chip("err", "card-dedup", "Exact dup", { dot: true }));
    else chips.push(chip("muted", "card-dedup", "Dedup —"));

    // Provenance (always informational/muted)
    var p = sig.provenance;
    var pLabel = (p.engine || p.extractor)
      ? esc((p.engine || "?") + " · " + (p.extractor || "?"))
      : "—";
    chips.push(chip("muted", "card-langprov", pLabel));

    return chips.join("");
  }

  // --- signals tab (full breakdown) ---------------------------------------

  function kvRow(k, v, flagged) {
    return '<span class="k' + (flagged ? " flag" : "") + '">' + esc(k) + "</span>" +
      '<span class="v' + (flagged ? " flag" : "") + '">' + v + "</span>";
  }

  function pill(state, text) {
    return '<span class="sig-pill ' + esc(state) + '">' + esc(text) + "</span>";
  }

  function qualityCard(sig) {
    var q = sig.quality;
    var head;
    if (q.passed === null) head = pill("muted", "n/a");
    else if (q.passed) head = pill("ok", "Passed");
    else head = pill("warn", "Flagged · " + q.failureCount + " rule" + (q.failureCount === 1 ? "" : "s"));

    var body = "";
    if (q.signals) {
      var flaggedKeys = {};
      q.failures.forEach(function (f) { if (FAILURE_SIGNAL[f]) flaggedKeys[FAILURE_SIGNAL[f]] = true; });
      var rows = SIGNAL_ORDER
        .filter(function (key) { return q.signals[key] !== undefined; })
        .map(function (key) { return kvRow(key, esc(q.signals[key]), !!flaggedKeys[key]); })
        .join("");
      body = '<div class="kv">' + rows + "</div>";
      if (q.failures.length) {
        body += '<div class="sig-note">Failed rules: ' + esc(q.failures.join(", ")) + "</div>";
      }
    } else {
      body = '<div class="sig-note">No quality signals available.</div>';
    }
    return card("card-quality", "Quality", head, body);
  }

  function dedupCard(sig) {
    var d = sig.dedup;
    var head, body;
    if (!d.state) {
      head = pill("muted", "n/a");
      body = '<div class="sig-note">No dedup signal.</div>';
    } else {
      var pillMap = { unique: ["ok", "Unique"], near: ["warn", "Near-duplicate"], exact: ["err", "Exact duplicate"] };
      head = pill(pillMap[d.state][0], pillMap[d.state][1]);
      var rows = kvRow("content_hash", esc((d.contentHash || "").slice(0, 12)) + (d.contentHash.length > 12 ? "…" : ""), false);
      var match = d.exactOf || d.nearOf;
      if (match) {
        rows += kvRow("duplicate of", safeExternalLink(match), false);
      }
      body = '<div class="kv">' + rows + "</div>";
    }
    return card("card-dedup", "Dedup", head, body);
  }

  function licenseCard(sig) {
    var l = sig.license;
    var body;
    if (l.id) {
      var rows = kvRow("license", esc(l.id), false);
      if (l.source) rows += kvRow("source", esc(l.source), false);
      if (l.url) rows += kvRow("url", safeExternalLink(l.url), false);
      body = '<div class="kv">' + rows + "</div>";
      if (l.evidence) body += '<div class="sig-evidence">' + esc(l.evidence) + "</div>";
    } else {
      body = '<div class="sig-note">No license markers found in footer or &lt;meta&gt;.</div>';
    }
    return card("card-license", "License", "", body);
  }

  function langProvCard(sig) {
    var ll = langLabel(sig.language) || "—";
    var p = sig.provenance;
    var rows = kvRow("language", ll, false) +
      kvRow("engine", esc(p.engine || "—"), false) +
      kvRow("extractor", esc(p.extractor || "—"), false);
    return card("card-langprov", "Language · Provenance", "", '<div class="kv">' + rows + "</div>");
  }

  function documentCard(extras) {
    extras = extras || {};
    var rows = "";
    if (extras.title) rows += kvRow("title", esc(extras.title), false);
    if (extras.description) rows += kvRow("description", esc(extras.description), false);
    if (extras.url) rows += kvRow("source url", safeExternalLink(extras.url), false);
    if (typeof extras.wordCount === "number") rows += kvRow("words", esc(extras.wordCount), false);
    if (typeof extras.charCount === "number")
      rows += kvRow("characters", esc(extras.charCount) + " (" + (extras.charCount / 1024).toFixed(2) + " KB)", false);
    if (!rows) return "";
    return card("card-document", "Document", "", '<div class="kv">' + rows + "</div>");
  }

  function card(id, title, headRight, bodyHtml) {
    return '<div class="signals-card" id="' + esc(id) + '">' +
      '<div class="signals-card-head"><span class="signals-card-title">' + esc(title) + "</span>" +
      (headRight || "") + "</div>" + bodyHtml + "</div>";
  }

  function renderSignalsTab(sig, extras) {
    return '<div class="signals-cards">' +
      qualityCard(sig) + dedupCard(sig) + licenseCard(sig) + langProvCard(sig) + documentCard(extras) +
      "</div>";
  }

  // --- crawl mode ----------------------------------------------------------

  function summarizeCrawl(results) {
    results = results || [];
    var out = { pages: results.length, qPassed: 0, qFlagged: 0, near: 0, exact: 0, langs: [] };
    var langSet = {};
    results.forEach(function (r) {
      var s = normalizeSignals(r);
      if (s.quality.passed === true) out.qPassed++;
      else if (s.quality.passed === false) out.qFlagged++;
      if (s.dedup.state === "near") out.near++;
      else if (s.dedup.state === "exact") out.exact++;
      if (s.language.lang) langSet[s.language.lang.toUpperCase()] = true;
    });
    out.langs = Object.keys(langSet).sort();
    return out;
  }

  function renderAggregate(sum) {
    var parts = [];
    parts.push("<b>" + sum.pages + " page" + (sum.pages === 1 ? "" : "s") + "</b>");
    if (sum.qPassed) parts.push('<span class="agg-item"><span class="dot ok"></span>' + sum.qPassed + " passed</span>");
    if (sum.qFlagged) parts.push('<span class="agg-item"><span class="dot warn"></span>' + sum.qFlagged + " flagged</span>");
    if (sum.near) parts.push('<span class="agg-item"><span class="dot warn"></span>' + sum.near + " near-dup</span>");
    if (sum.exact) parts.push('<span class="agg-item"><span class="dot err"></span>' + sum.exact + " exact-dup</span>");
    if (sum.langs.length) parts.push('<span class="agg-item">langs: ' + esc(sum.langs.join(", ")) + "</span>");
    return parts.join('<span class="agg-sep">·</span>');
  }

  function renderRowChips(sig) {
    var out = [];
    // quality dot (+ count if flagged)
    if (sig.quality.passed === true) out.push('<span class="row-chip ok"><span class="dot ok"></span></span>');
    else if (sig.quality.passed === false) out.push('<span class="row-chip warn"><span class="dot warn"></span>' + sig.quality.failureCount + "</span>");
    // dedup flag only when duplicated
    if (sig.dedup.state === "near") out.push('<span class="row-chip warn">near-dup</span>');
    else if (sig.dedup.state === "exact") out.push('<span class="row-chip err">exact-dup</span>');
    // language code
    if (sig.language.lang) out.push('<span class="row-chip">' + esc(sig.language.lang.toUpperCase()) + "</span>");
    return out.join("");
  }

  window.Signals = {
    normalizeSignals: normalizeSignals,
    renderSignalBar: renderSignalBar,
    renderSignalsTab: renderSignalsTab,
    summarizeCrawl: summarizeCrawl,
    renderAggregate: renderAggregate,
    renderRowChips: renderRowChips
  };
})();
