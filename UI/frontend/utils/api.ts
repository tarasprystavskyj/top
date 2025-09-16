const API_BASE =
  (typeof process !== 'undefined' &&
    (process.env.NEXT_PUBLIC_API_BASE_URL || process.env.NEXT_PUBLIC_BACKEND_URL)) ||
  '';

const ABSOLUTE_URL_RE = /^[a-zA-Z][a-zA-Z\d+\-.]*:/;

export function resolveApiUrl(path: string | null | undefined): string | undefined {
  if (!path) return undefined;
  if (ABSOLUTE_URL_RE.test(path)) return path;
  if (API_BASE) {
    try {
      return new URL(path, API_BASE).toString();
    } catch {
      return path;
    }
  }
  if (typeof window !== 'undefined') {
    try {
      return new URL(path, window.location.origin).toString();
    } catch {
      return path;
    }
  }
  return path;
}

export function resolveArtifactMap(
  artifacts: Record<string, any> | null | undefined,
): Record<string, any> | undefined {
  if (!artifacts || typeof artifacts !== 'object') return undefined;
  const resolved: Record<string, any> = {};
  for (const [key, value] of Object.entries(artifacts)) {
    if (typeof value === 'string') {
      resolved[key] = resolveApiUrl(value) ?? value;
    } else {
      resolved[key] = value;
    }
  }
  return resolved;
}

export function apiFetch(path: string, options?: RequestInit) {
  const url = resolveApiUrl(path) ?? path;
  return fetch(url, options);
}
