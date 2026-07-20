import assert from "node:assert/strict";
import test from "node:test";

import * as configModule from "../next.config.ts";

test("development rewrites forward dashboard requests to FastAPI", () => {
  assert.equal(typeof configModule.fastApiRewrites, "function");

  assert.deepEqual(configModule.fastApiRewrites("http://127.0.0.1:8011/"), [
    {
      source: "/api/:path*",
      destination: "http://127.0.0.1:8011/api/:path*",
    },
    {
      source: "/data/:path*",
      destination: "http://127.0.0.1:8011/data/:path*",
    },
    {
      source: "/artifacts",
      destination: "http://127.0.0.1:8011/artifacts",
    },
    {
      source: "/docs",
      destination: "http://127.0.0.1:8011/docs",
    },
    {
      source: "/openapi.json",
      destination: "http://127.0.0.1:8011/openapi.json",
    },
  ]);
});

test("development proxying and production static export stay separate", async () => {
  assert.equal(typeof configModule.createNextConfig, "function");

  const development = configModule.createNextConfig(
    "development",
    "http://127.0.0.1:8011",
  );
  assert.equal(development.output, undefined);
  assert.deepEqual(
    await development.rewrites(),
    configModule.fastApiRewrites("http://127.0.0.1:8011"),
  );

  const production = configModule.createNextConfig("production");
  assert.equal(production.output, "export");
  assert.equal(production.rewrites, undefined);
});
