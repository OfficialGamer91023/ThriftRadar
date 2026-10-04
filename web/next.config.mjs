// Static export served by FastAPI at "/" (DESIGN §4.9). No server features: no API routes, no SSR.
/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
  poweredByHeader: false,
};
export default nextConfig;
