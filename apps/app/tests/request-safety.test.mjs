import assert from "node:assert/strict";
import test from "node:test";

import {
  isAbortError,
  isTerminalCrawlStatus,
  nextCrawlPageDrainCursor,
  nextCrawlPageCursor,
  normalizePageLimit,
} from "../src/lib/request-safety.ts";

test("page limits stay within the backend contract", () => {
  assert.equal(normalizePageLimit(Number.NaN), 25);
  assert.equal(normalizePageLimit(0), 1);
  assert.equal(normalizePageLimit(12.9), 12);
  assert.equal(normalizePageLimit(900), 500);
});

test("abort errors are identified without hiding ordinary failures", () => {
  assert.equal(isAbortError(new DOMException("cancelled", "AbortError")), true);
  assert.equal(isAbortError(new Error("network down")), false);
  assert.equal(isAbortError(null), false);
});

test("restored interrupted crawls stop client polling", () => {
  assert.equal(isTerminalCrawlStatus("completed"), true);
  assert.equal(isTerminalCrawlStatus("partial"), true);
  assert.equal(isTerminalCrawlStatus("failed"), true);
  assert.equal(isTerminalCrawlStatus("cancelled"), true);
  assert.equal(isTerminalCrawlStatus("timed_out"), true);
  assert.equal(isTerminalCrawlStatus("interrupted"), true);
  assert.equal(isTerminalCrawlStatus("running"), false);
});

test("page cursors revisit the earliest unresolved discovery sequence", () => {
  assert.equal(nextCrawlPageCursor([
    { discovery_seq: 0, state: "succeeded" },
    { discovery_seq: 1, state: "leased" },
    { discovery_seq: 2, state: "succeeded" },
  ], 2), 0);
  assert.equal(nextCrawlPageCursor([
    { discovery_seq: 0, state: "pending" },
  ], 0), null);
  assert.equal(nextCrawlPageCursor([
    { discovery_seq: 0, state: "succeeded" },
    { discovery_seq: 1, state: "permanent_failed" },
  ], 1), 1);
});

test("terminal page drains stop on completion, empty batches, or stalled cursors", () => {
  const base = {
    requestedAfter: 99,
    nextAfter: 199,
    batchSize: 100,
    capturedResults: 100,
    expectedResults: 250,
  };
  assert.equal(nextCrawlPageDrainCursor(base), 199);
  assert.equal(nextCrawlPageDrainCursor({ ...base, batchSize: 0 }), null);
  assert.equal(nextCrawlPageDrainCursor({ ...base, capturedResults: 250 }), null);
  assert.equal(nextCrawlPageDrainCursor({ ...base, nextAfter: 99 }), null);
});
