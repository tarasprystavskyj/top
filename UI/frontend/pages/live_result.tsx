import { useEffect, useState } from 'react';
import { apiFetch } from '../utils/api';

export default function LiveResult() {
  const [sessions, setSessions] = useState<string[]>([]);
  const [sel, setSel] = useState('');
  const [images, setImages] = useState<string[]>([]);
  const [btImages, setBtImages] = useState<string[]>([]);
  const [btSummary, setBtSummary] = useState<any>(null);
  const [slide, setSlide] = useState(0);

  useEffect(() => {
    apiFetch('/api/live_results')
      .then(r => r.json())
      .then(data => (Array.isArray(data) ? setSessions(data) : setSessions([])))
      .catch(() => setSessions([]));
  }, []);

  useEffect(() => {
    if (!sel) return;
    apiFetch(`/api/live_results/${sel}`)
      .then(r => r.json())
      .then(data => {
        const order = ['equity_vs_trade', 'dd_vs_trade', 'equity_vs_time'];
        const liveImgs = order
          .map(n => data.artifacts?.[`viz_${n}.png`])
          .filter(Boolean) as string[];
        const backImgs = order
          .map(n => data.backtest?.artifacts?.[`bt_viz_${n}.png`])
          .filter(Boolean) as string[];
        setImages(liveImgs);
        setBtImages(backImgs);
        setBtSummary(data.backtest?.summary || null);
        setSlide(0);
      });
  }, [sel]);

  const maxLen = Math.max(btImages.length, images.length, 1);

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
      {btSummary && (
        <pre style={{ maxWidth: '800px', overflowX: 'auto' }}>
          {JSON.stringify(btSummary, null, 2)}
        </pre>
      )}
      {btImages.length > 0 && images.length > 0 && (
        <div>
          <div style={{ display: 'flex', gap: '10px', marginTop: '10px' }}>
            <img
              src={btImages[slide % btImages.length]}
              style={{ maxWidth: '400px' }}
            />
            <img
              src={images[slide % images.length]}
              style={{ maxWidth: '400px' }}
            />
          </div>
          <div>
            <button
              onClick={() => setSlide((slide - 1 + maxLen) % maxLen)}
              disabled={maxLen <= 1}
            >
              Prev
            </button>
            <button
              onClick={() => setSlide((slide + 1) % maxLen)}
              disabled={maxLen <= 1}
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
