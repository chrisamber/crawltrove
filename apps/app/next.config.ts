import type { NextConfig } from "next";

export function fastApiRewrites(baseUrl = "http://127.0.0.1:8000") {
  const backend = baseUrl.replace(/\/+$/, "");

  return [
    { source: "/api/:path*", destination: `${backend}/api/:path*` },
    { source: "/data/:path*", destination: `${backend}/data/:path*` },
    { source: "/artifacts", destination: `${backend}/artifacts` },
    { source: "/docs", destination: `${backend}/docs` },
    { source: "/openapi.json", destination: `${backend}/openapi.json` },
  ];
}

const sharedConfig: NextConfig = {
  assetPrefix: "/static/dashboard",
  images: { unoptimized: true },
  turbopack: { root: process.cwd() },
};

export function createNextConfig(
  environment = process.env.NODE_ENV,
  backendUrl = process.env.FASTAPI_URL ?? "http://127.0.0.1:8000",
): NextConfig {
  if (environment === "development") {
    return {
      ...sharedConfig,
      rewrites: async () => fastApiRewrites(backendUrl),
    };
  }

  return {
    ...sharedConfig,
    output: "export",
  };
}

export default createNextConfig();
