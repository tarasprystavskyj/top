const API_URL = process.env.NEXT_PUBLIC_BACKEND_URL || 'http://vps2.happyuser.info:8001';
export function apiFetch(path: string, options?: RequestInit) {
  return fetch(`${API_URL}${path}`, options);
}
