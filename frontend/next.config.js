/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  async rewrites() {
    const apiUrl = process.env.API_URL || "http://localhost:8000";
    return {
      // afterFiles rewrites run AFTER checking pages/API routes,
      // so /api/cases/[caseId]/notes API route takes priority for uploads
      afterFiles: [
        {
          source: "/api/:path*",
          destination: `${apiUrl}/api/:path*`,
        },
      ],
    };
  },
};

module.exports = nextConfig;
