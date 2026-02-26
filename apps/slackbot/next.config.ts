import type { NextConfig } from "next";
import path from "path";

const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: path.join(import.meta.dirname),
  basePath: "/ui",
  env: {
    NEXT_PUBLIC_BASE_PATH: "/ui",
  },
};

export default nextConfig;
