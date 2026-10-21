/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "export",
  trailingSlash: true,
  basePath: "/dashboard",
  images: { unoptimized: true },
  reactStrictMode: true,
};

export default nextConfig;
