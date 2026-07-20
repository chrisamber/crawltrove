import assert from "node:assert/strict";
import test from "node:test";

import {
  isAbortError,
  isTerminalCrawlStatus,
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
  assert.equal(isTerminalCrawlStatus("failed"), true);
  assert.equal(isTerminalCrawlStatus("cancelled"), true);
  assert.equal(isTerminalCrawlStatus("interrupted"), true);
  assert.equal(isTerminalCrawlStatus("running"), false);
});
