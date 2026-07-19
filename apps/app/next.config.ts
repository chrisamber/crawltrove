import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  assetPrefix: "/static/dashboard",
  images: { unoptimized: true },
  turbopack: { root: process.cwd() },
};

export default nextConfig;
