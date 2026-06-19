function normalizeBase(url: string | undefined | null): string | null {
  if (!url) return null;
  const trimmed = String(url).trim();
  if (!trimmed) return null;
  return trimmed.endsWith('/') ? trimmed.slice(0, -1) : trimmed;
}

function fallbackBases(): string[] {
  const bases: string[] = [];

  const explicitBase =
    normalizeBase(process.env.NEXT_PUBLIC_BACKEND_URL) ||
    normalizeBase(process.env.NEXT_PUBLIC_API_URL) ||
    normalizeBase(process.env.NEXT_PUBLIC_DEPLOY_API_BASE);

  if (explicitBase) bases.push(explicitBase);

  if (typeof window !== 'undefined') {
    // Correct default for VPS/Next rewrites: same origin, e.g.
    // http://vps2.happyuser.info:3001/api/... -> backend through next.config.js rewrite.
    bases.push(window.location.origin);

    // Public backend-port fallbacks. These work only if the backend port is open externally.
    bases.push(`${window.location.protocol}//${window.location.hostname}:8001`);
    bases.push(`${window.location.protocol}//${window.location.hostname}:8002`);
  }

  // Last-resort local fallbacks. They are intentionally last, because in a browser
  // 127.0.0.1 means the user's own machine, not the VPS.
  bases.push('http://127.0.0.1:8002', 'http://localhost:8002', 'http://127.0.0.1:8001', 'http://localhost:8001');

  return Array.from(new Set(bases));
}

export async function apiFetch(path: string, options?: RequestInit): Promise<Response> {
  const isRelativeApi = path.startsWith('/');
  const method = String(options?.method || 'GET').toUpperCase();
  const canRetryResponse = method === 'GET' || method === 'HEAD';

  if (!isRelativeApi) {
    return fetch(path, options);
  }

  const bases = fallbackBases();
  let lastErr: unknown = null;

  for (const base of bases) {
    try {
      const response = await fetch(`${base}${path}`, options);
      if (!canRetryResponse || response.status < 500) return response;
      lastErr = new Error(`${response.status} ${response.statusText}`);
    } catch (err) {
      lastErr = err;
    }
  }

  throw lastErr || new Error(`Failed to fetch ${path}`);
}
