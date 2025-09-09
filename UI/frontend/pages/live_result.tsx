import { useEffect, useState } from 'react';
import { apiFetch } from '../utils/api';

export default function LiveResult() {
  const [sessions, setSessions] = useState<string[]>([]);
  const [sel, setSel] = useState('');
  const [summary, setSummary] = useState<any>(null);
  const [images, setImages] = useState<string[]>([]);
  const [slide, setSlide] = useState(0);
  const [logs, setLogs] = useState('');
  const [trades, setTrades] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiFetch('/api/live_results')
      .then(r => r.json())
      .then(data => {
        console.debug('Sessions response', data);
        setSessions(Array.isArray(data) ? data : []);
      })
      .catch(err => {
        console.error('Sessions fetch error', err);
        setSessions([]);
      });

  useEffect(() => {
    if (!sel) return;
    console.debug('Loading session', sel);
    apiFetch(`/api/live_results/${sel}`)
      .then(r => {
        console.debug('Session response status', r.status);
        if (!r.ok) throw new Error('failed');
        return r.json();
      })
      .then(data => {
        console.debug('Session data', data);
        const order = ['equity_vs_trade', 'dd_vs_trade', 'equity_vs_time'];
        const backImgs = order
          .map(n => data.backtest?.artifacts?.[`bt_viz_${n}.png`])
          .filter(Boolean) as string[];
        const liveImgs = order
          .map(n => data.artifacts?.[`viz_${n}.png`])
          .filter(Boolean) as string[];
        const imgs = backImgs.length ? backImgs : liveImgs;
        console.debug('Live images', liveImgs);
        console.debug('Backtest images', backImgs);
        console.debug('Backtest summary', data.backtest?.summary || null);
        setImages(imgs);
        setSummary(data.backtest?.summary || null);
        setTrades(data.backtest?.trades || []);
        setSlide(0);
        setError(imgs.length ? null : 'No data available for this session');
        if (data.backtest?.logs) {
          fetch(data.backtest.logs)
            .then(r => r.text())
            .then(setLogs)
            .catch(() => setLogs(''));
        } else {
          setLogs('');
        }
      })
      .catch(err => {
        console.error('Session fetch error', err);
        setSummary(null);
        setImages([]);
        setTrades([]);
        setLogs('');
        setError('No data available for this session');
      });
  }, [sel]);

  return (
    <div>
      <h3>Live Result{sel ? ` – ${sel}` : ''}</h3>
      <select
        value={sel}
        onChange={e => {
          console.log('Selected session', e.target.value);
          setSel(e.target.value);
        }}
      >
        <option value=''>--select--</option>
        {sessions.map(s => (
          <option key={s} value={s}>
            {s}
          </option>
        ))}
      </select>
      {error && <p>{error}</p>}
      {summary && (
        <pre style={{ maxWidth: '800px', overflowX: 'auto' }}>
          {JSON.stringify(summary, null, 2)}
        </pre>
      )}
      {images.length > 0 && (
        <div>
          <img
            src={images[slide]}
            style={{ maxWidth: '400px', display: 'block' }}
          />
          {images.length > 1 && (
            <div>
              <button
                onClick={() => setSlide((slide - 1 + images.length) % images.length)}
              >
                Prev
              </button>
              <button onClick={() => setSlide((slide + 1) % images.length)}>
                Next
              </button>
            </div>
          )}
        </div>
      )}
      {logs && (
        <pre style={{ maxHeight: '200px', overflow: 'auto' }}>{logs}</pre>
      )}
      {trades.length > 0 && (
        <div style={{ maxHeight: '200px', overflow: 'auto' }}>
          <table border={1}>
            <thead>
              <tr>
                {Object.keys(trades[0]).map(k => (
                  <th key={k}>{k}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {trades.map((t, i) => (
                <tr key={i}>
                  {Object.keys(trades[0]).map(k => (
                    <td key={k}>{formatVal(t[k])}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function formatVal(v: any) {
  const num = Number(v);
  return isNaN(num) ? v : num.toFixed(3);
}
