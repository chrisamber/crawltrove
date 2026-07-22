import assert from "node:assert/strict";
import test from "node:test";

import {
  crawlMarkdown,
  crawlPageToResult,
  formatDate,
  mergeCrawlPages,
} from "../src/lib/api.ts";

test("date formatting preserves the Unix epoch while treating missing values as empty", () => {
  assert.equal(formatDate(null), "—");
  assert.equal(formatDate(undefined), "—");
  assert.notEqual(formatDate(0), "—");
});

test("durable crawl pages merge by discovery sequence without dropping loaded Markdown", () => {
  const first = mergeCrawlPages([], [{
    discovery_seq: 0,
    state: "succeeded",
    original_url: "https://example.com",
    final_url: "https://example.com/final",
    title: "Seed",
    markdown: "seed body",
    metadata: { engine: "http" },
  }]);
  const merged = mergeCrawlPages(first, [
    { discovery_seq: 0, state: "succeeded", title: "Seed refreshed" },
    {
      discovery_seq: 1,
      state: "succeeded",
      normalized_url: "https://example.com/next",
      markdown: "next body",
    },
  ]);

  assert.equal(merged.length, 2);
  assert.equal(merged[0].url, "https://example.com/final");
  assert.equal(merged[0].title, "Seed refreshed");
  assert.equal(merged[0].markdown, "seed body");
  assert.equal(merged[1].url, "https://example.com/next");
});

test("inline crawl Markdown is rendered in discovery order", () => {
  const page = crawlPageToResult({
    discovery_seq: 3,
    state: "succeeded",
    original_url: "https://example.com/docs",
    markdown: "Documentation",
  });
  assert.equal(
    crawlMarkdown([page]),
    "# https://example.com/docs\n\nDocumentation",
  );
});
