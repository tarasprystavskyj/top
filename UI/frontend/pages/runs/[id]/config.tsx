import { useRouter } from 'next/router';
import useSWR from 'swr';
import dynamic from 'next/dynamic';
import { apiFetch } from '../../../utils/api';

const Monaco = dynamic(() => import('@monaco-editor/react'), { ssr: false });
const fetcher = (u: string) => apiFetch(u).then(r => r.text());

export default function RunConfig() {
  const router = useRouter();
  const { id } = router.query;
  const { data } = useSWR<string>(
    id ? `/api/jobs/${id}/artifacts/cfg_merged.yaml` : null,
    fetcher
  );
  if (!data) return <>Loading...</>;
  return (
    <div>
      <h3>Config for run {id}</h3>
      <div style={{ height: 500, border: '1px solid #eee' }}>
        <Monaco
          language="yaml"
          value={data}
          options={{ readOnly: true, minimap: { enabled: false } }}
        />
      </div>
    </div>
  );
}
