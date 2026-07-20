import assert from "node:assert/strict";
import test from "node:test";

import { formatDate } from "../src/lib/api.ts";

test("date formatting preserves the Unix epoch while treating missing values as empty", () => {
  assert.equal(formatDate(null), "—");
  assert.equal(formatDate(undefined), "—");
  assert.notEqual(formatDate(0), "—");
});
