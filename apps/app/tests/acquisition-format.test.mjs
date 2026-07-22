import assert from "node:assert/strict";
import test from "node:test";

import {
  formatAttempt,
  formatNativeUsage,
  formatRoute,
} from "../src/lib/acquisition-format.ts";

test("native usage keeps provider units", () => {
  assert.equal(formatNativeUsage("firecrawl", "credits", 4), "4 credits");
  assert.equal(formatNativeUsage("brightdata", "requests", 2), "2 requests");
  assert.equal(formatNativeUsage("browserbase", "browserMinutes", 1.5), "1.5 browser minutes");
  assert.equal(formatNativeUsage("browserbase", "proxyBytes", 0), "0 proxy bytes");
});

test("attempt summary includes classified block reason", () => {
  assert.equal(formatAttempt({
    route: "brightdata_unlocker", provider: "brightdata",
    outcome: "retryable_failure", blockReason: "challenge", durationMs: 1200,
  }), "Bright Data · challenge · retryable · 1.2s");
  assert.equal(formatRoute("firecrawl_interact"), "Firecrawl Interact");
});

test("formatters do not expose unknown provider or block text", () => {
  assert.equal(formatNativeUsage("unknown", "dollars", 1), "—");
  assert.equal(formatAttempt({
    route: "local_http", provider: "unknown", outcome: "mystery",
    blockReason: "token=secret", durationMs: null,
  }), "Unknown provider · blocked · unknown outcome");
});
