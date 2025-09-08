import { useState } from 'react';
import useSWR from 'swr';
import { apiFetch } from '../utils/api';

const fetcher = (u: string) => apiFetch(u).then(r => r.json());

interface RunItem {
  job_id: string;
  cfg_name?: string;
  started_at?: number;
}

export default function Runs() {
  const { data } = useSWR<RunItem[]>('/api/runs', fetcher);
  const [open, setOpen] = useState<{ [k: string]: boolean }>({});
  if (!data) return <>Loading...</>;
  return (
    <div>
      <h3>Runs</h3>
      {data.map(r => (
        <div key={r.job_id} style={{ marginBottom: '10px', border: '1px solid #eee' }}>
          <div>
            {r.cfg_name || r.job_id} -{' '}
            {r.started_at ? new Date(r.started_at * 1000).toLocaleString() : ''} -{' '}
            <a href={`/runs/${r.job_id}/config`} target="_blank" rel="noreferrer">
              config
            </a>{' '}
            <button onClick={() => setOpen(o => ({ ...o, [r.job_id]: !o[r.job_id] }))}>
              {open[r.job_id] ? 'Hide' : 'Show'}
            </button>
          </div>
          {open[r.job_id] && (
            <div
              style={{
                height: '200px',
                resize: 'vertical',
                overflow: 'auto',
                borderTop: '1px solid #ddd',
              }}
            >
              <iframe
                src={`/run?id=${r.job_id}`}
                style={{ width: '100%', height: '100%', border: 'none' }}
              />
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
