const API_URL = process.env.BACKEND_URL || 'http://127.0.0.1:8001';

module.exports = {
  reactStrictMode: true,
  output: 'standalone',
  experimental: { appDir: false },
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${API_URL}/api/:path*`,
      },
    ];
  },
};
