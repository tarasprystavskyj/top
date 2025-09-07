const API_URL = process.env.BACKEND_URL || 'http://vps2.happyuser.info:8001';

module.exports = {
  reactStrictMode: true,
  output: 'standalone',
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${API_URL}/api/:path*`,
      },
    ];
  },
};
