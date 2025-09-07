import useSWR from 'swr';
import Link from 'next/link';

// Base URL of the backend API.  The server is typically started on port 8001
// via ``uvicorn api_main:app --port 8001`` so we default to that, but the
// value can be overridden at build/runtime via the ``NEXT_PUBLIC_API_BASE_URL``
// environment variable.
const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ||
  (typeof window === 'undefined'
    ? 'http://localhost:8001'
    : `${window.location.protocol}//${window.location.hostname}:8001`);
const fetcher = (url: string) => fetch(url).then(res => res.json());

export default function ConfigList() {
  const { data } = useSWR(`${API_BASE}/api/configs`, fetcher);

  if (!data) {
    return <div>Loading...</div>;
  }

  return (
    <ul>
      {data.map((c: any) => (
        <li key={c.name}>
          <Link href={`/configs/${c.name}`}>{c.name}</Link>
        </li>
      ))}
    </ul>
  );
}
