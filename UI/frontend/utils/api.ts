function normalizeBase(url: string | undefined | null): string | null {
  if (!url) return null;
  const trimmed = String(url).trim();
  if (!trimmed) return null;
  return trimmed.endsWith('/') ? trimmed.slice(0, -1) : trimmed;
}

function fallbackBases(): string[] {
  const bases: string[] = [];

  const envBase = normalizeBase(process.env.NEXT_PUBLIC_BACKEND_URL);
  if (envBase) bases.push(envBase);

  if (typeof window !== 'undefined') {
    // Keep same-origin API as a fallback candidate for environments where
    // Next.js rewrites/proxying is configured and functional.
    bases.push(window.location.origin);
    bases.push(`${window.location.protocol}//${window.location.hostname}:8001`);
  }

  bases.push('http://127.0.0.1:8001', 'http://localhost:8001');
  return Array.from(new Set(bases));
}

export async function apiFetch(path: string, options?: RequestInit): Promise<Response> {
  const isRelativeApi = path.startsWith('/');

  // If explicit public backend URL exists, try it first for relative paths.
  const envBase = normalizeBase(process.env.NEXT_PUBLIC_BACKEND_URL);
  if (isRelativeApi && envBase) {
    try {
      return await fetch(`${envBase}${path}`, options);
    } catch {
      // Continue to fallback chain below.
    }
  }

  try {
    return await fetch(path, options);
  } catch (err) {
    if (!isRelativeApi) throw err;
    const bases = fallbackBases();
    let lastErr: unknown = err;
    for (const base of bases) {
      try {
        return await fetch(`${base}${path}`, options);
      } catch (retryErr) {
        lastErr = retryErr;
      }
    }
    throw lastErr;
  }
}
