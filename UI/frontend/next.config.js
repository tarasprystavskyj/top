const API_URL = process.env.BACKEND_URL || 'http://127.0.0.1:8001';

module.exports = {
  reactStrictMode: true,
  output: 'standalone',
  experimental: { appDir: false },
  env: {
    NEXT_PUBLIC_API_BASE_URL: process.env.NEXT_PUBLIC_API_BASE_URL || API_URL,
    NEXT_PUBLIC_BACKEND_URL: process.env.NEXT_PUBLIC_BACKEND_URL || API_URL,
  },
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${API_URL}/api/:path*`,
      },
    ];
  },
};
