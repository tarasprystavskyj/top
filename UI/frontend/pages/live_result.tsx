import { useEffect, useState } from 'react';
import { apiFetch } from '../utils/api';

export default function LiveResult() {
  const [sessions, setSessions] = useState<string[]>([]);
  const [sel, setSel] = useState('');
  const [summary, setSummary] = useState<any>(null);
  const [pairs, setPairs] = useState<{ name: string; live: string | null; back: string | null }[]>([]);
  const [slide, setSlide] = useState(0);
  const [logs, setLogs] = useState('');
  const [trades, setTrades] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [range, setRange] = useState<{ start: string; end: string } | null>(null);

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
  }, []);

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
        const plotNames = [
          'equity_by_time.png',
          'returns_hist.png',
          'equity_by_trade.png',
          'drawdown_by_trade.png',
          'viz_equity_vs_trade.png',
          'viz_dd_vs_trade.png',
          'viz_equity_vs_time.png',
        ];
        const ps = plotNames
          .map(name => ({
            name,
            back: data.backtest?.artifacts?.[name] || null,
            live: data.artifacts?.[name] || null,
          }))
          .filter(p => p.back || p.live);
        console.debug('Plot pairs', ps);
        console.debug('Backtest summary', data.backtest?.summary || null);
        setPairs(ps);
        setSummary(data.backtest?.summary || null);
        setTrades(data.backtest?.trades || []);
        setSlide(0);
        setError(ps.length ? null : 'No data available for this session');
        if (data.backtest?.trades?.length) {
          const t0 = data.backtest.trades[0];
          const t1 = data.backtest.trades[data.backtest.trades.length - 1];
          const k = t0.ts_utc ? 'ts_utc' : t0.ts ? 'ts' : null;
          if (k) setRange({ start: t0[k], end: t1[k] });
          else setRange(null);
        } else {
          setRange(null);
        }
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
        setPairs([]);
        setTrades([]);
        setLogs('');
        setRange(null);
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
          {JSON.stringify(formatObj(summary), null, 2)}
        </pre>
      )}
      {pairs.length > 0 && (
        <div>
          <div style={{ display: 'flex', gap: '10px' }}>
            {pairs[slide].back && (
              <img src={pairs[slide].back!} style={{ maxWidth: '400px' }} />
            )}
            {pairs[slide].live && (
              <img src={pairs[slide].live!} style={{ maxWidth: '400px' }} />
            )}
          </div>
          {pairs.length > 1 && (
            <div>
              <button onClick={() => setSlide((slide - 1 + pairs.length) % pairs.length)}>
                Prev
              </button>
              <span style={{ margin: '0 8px' }}>{pairs[slide].name}</span>
              <button onClick={() => setSlide((slide + 1) % pairs.length)}>Next</button>
            </div>
          )}
        </div>
      )}
      {logs && (
        <pre style={{ maxHeight: '200px', overflow: 'auto' }}>{logs}</pre>
      )}
      {range && (
        <p>
          Window: {range.start} – {range.end}
        </p>
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

function formatObj(obj: any) {
  if (!obj) return obj;
  const out: any = {};
  for (const [k, v] of Object.entries(obj)) {
    const num = Number(v as any);
    out[k] = isNaN(num) ? v : num.toFixed(3);
  }
  return out;
}
