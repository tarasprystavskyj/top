import { useEffect, useState } from 'react';
import { apiFetch } from '../utils/api';

export default function LiveResult() {
  const [sessions, setSessions] = useState<string[]>([]);
  const [sel, setSel] = useState('');
  const [images, setImages] = useState<string[]>([]);

  useEffect(() => {
    apiFetch('/api/live_results')
      .then(r => r.json())
      .then(data => Array.isArray(data) ? setSessions(data) : setSessions([]))
      .catch(() => setSessions([]));
  }, []);

  useEffect(() => {
    if (!sel) return;
    apiFetch(`/api/live_results/${sel}`)
      .then(r => r.json())
      .then(data => {
        const imgs = Object.values(data.artifacts || {});
        setImages(imgs as string[]);
      });
  }, [sel]);

  return (
    <div>
      <h3>Live Result</h3>
      <select value={sel} onChange={e => setSel(e.target.value)}>
        <option value=''>--select--</option>
        {sessions.map(s => (
          <option key={s} value={s}>
            {s}
          </option>
        ))}
      </select>
      <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', marginTop: '10px' }}>
        {images.map((u, i) => (
          <img key={i} src={u} style={{ maxWidth: '400px' }} />
        ))}
      </div>
    </div>
  );
}
