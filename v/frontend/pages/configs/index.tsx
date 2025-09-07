import useSWR from 'swr';
import Link from 'next/link';

const fetcher = (url: string) => fetch(url).then(res => res.json());

export default function ConfigList() {
  const { data } = useSWR('/api/configs', fetcher);

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
