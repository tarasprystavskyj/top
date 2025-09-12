import { useEffect, useState } from 'react';
import { useRouter } from 'next/router';
import { apiFetch } from '../utils/api';

export default function LiveResult() {
  const router = useRouter();
  const [sessions, setSessions] = useState<string[]>([]);
  const [sel, setSel] = useState('');
  const [summary, setSummary] = useState<any>(null);
  const [pairs, setPairs] = useState<{ name: string; live: string | null; back: string | null }[]>([]);
  const [slide, setSlide] = useState(0);
  const [logs, setLogs] = useState('');
  const [trades, setTrades] = useState<any[]>([]);
  const [liveTrades, setLiveTrades] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [liveRange, setLiveRange] = useState<{ start: string; end: string } | null>(null);
  const [debug, setDebug] = useState(false);
  const [debugData, setDebugData] = useState<any>(null);
  const [btRangeText, setBtRangeText] = useState<string>('');

  useEffect(() => {
    const q = router.query.cfg;
    if (typeof q === 'string') {
      setSel(q);
    }
  }, [router.query.cfg]);

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
    console.debug('Loading session', sel, 'debug', debug);
    apiFetch(`/api/live_results/${sel}?debug=${debug ? 1 : 0}`)
      .then(r => {
        console.debug('Session response status', r.status);
        if (!r.ok) throw new Error('failed');
        return r.json();
      })
      .then(data => {
        console.debug('Session data', data);
        const plotFiles = [
          'equity_by_time.png',
          'equity_by_trade.png',
          'drawdown_by_trade.png',
          'returns_hist.png',
        ];
        const ps = plotFiles
          .map(fn => ({
            name: fn.replace('.png', ''),
            back:
              data.backtest?.artifacts?.[fn] ||
              (fn === 'equity_by_time.png'
                ? data.backtest?.artifacts?.['viz_equity_vs_time.png']
                : null),
            live:
              data.artifacts?.[fn] ||
              (fn === 'equity_by_time.png'
                ? data.artifacts?.['viz_equity_vs_time.png']
                : null),
          }))
          .filter(p => p.back || p.live);
        console.debug('Plot pairs', ps);
        console.debug('Backtest summary', data.backtest?.summary || null);
        setPairs(ps);
        setSummary(data.backtest?.summary || null);
        setTrades(data.backtest?.trades || []);
        setLiveTrades(data.live_trades || []);
        setSlide(0);
        const hasData =
          ps.length > 0 ||
          !!data.backtest?.summary ||
          (data.backtest?.trades?.length ?? 0) > 0 ||
          (data.live_trades?.length ?? 0) > 0;
        setError(hasData ? null : 'No data available for this session');
        setBtRangeText(data.backtest?.time_range_text || '');
        setLiveRange(data.live_range || null);
        if (debug && data?.debug) {
          console.debug('Live debug:', data.debug);
          setDebugData(data.debug);
        } else {
          setDebugData(null);
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
        setLiveTrades([]);
        setLogs('');
        setBtRangeText('');
        setLiveRange(null);
        setError('No data available for this session');
        setDebugData(null);
      });
  }, [sel, debug]);

  return (
    <div>
      <h3>Live Result{sel ? ` – ${sel}` : ''}</h3>
      <select
        value={sel}
        onChange={e => {
          const v = e.target.value;
          console.log('Selected session', v);
          const q = { ...router.query } as any;
          if (v) q.cfg = v; else delete q.cfg;
          router.replace({ pathname: router.pathname, query: q }, undefined, { shallow: true });
          setSel(v);
        }}
      >
        <option value=''>--select--</option>
        {sessions.map(s => (
          <option key={s} value={s}>
            {s}
          </option>
        ))}
      </select>
      <label style={{ marginLeft: 12 }}>
        <input
          type="checkbox"
          checked={debug}
          onChange={e => setDebug(e.target.checked)}
        />
        Debug
      </label>
      {error && <p>{error}</p>}
      {summary && (
        <pre style={{ maxWidth: '800px', overflowX: 'auto' }}>
          {JSON.stringify(formatObj(summary), null, 2)}
        </pre>
      )}
      {pairs.length > 0 && (
        <div>
          <div style={{ display: 'flex', gap: '10px' }}>
            <div style={{ textAlign: 'center' }}>
              <h3>
                Backtest {btRangeText ? `(${btRangeText})` : ''}
              </h3>
              {pairs[slide].back ? (
                <img src={pairs[slide].back!} style={{ maxWidth: '400px' }} />
              ) : (
                <div style={{ maxWidth: '400px', textAlign: 'center' }}>
                  No backtest trade data
                </div>
              )}
            </div>
            <div style={{ textAlign: 'center' }}>
              <h3>
                Live {liveRange ? `(${liveRange.start} — ${liveRange.end})` : ''}
              </h3>
              {pairs[slide].live ? (
                <img src={pairs[slide].live!} style={{ maxWidth: '400px' }} />
              ) : (
                <div style={{ maxWidth: '400px', textAlign: 'center' }}>
                  No live trade data
                </div>
              )}
            </div>
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
      {debug && debugData && (
        <pre style={{ maxHeight: 300, overflow: 'auto' }}>
          {JSON.stringify(debugData, null, 2)}
        </pre>
      )}
      {(trades.length > 0 || liveTrades.length > 0) && (
        <div style={{ display: 'flex', gap: 16 }}>
          <div style={{ flex: 1 }}>
            <h4>Backtest trades ({trades.length})</h4>
            {trades.length > 0 ? (
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
            ) : (
              <div>No backtest trades</div>
            )}
          </div>
          <div style={{ flex: 1 }}>
            <h4>Live trades ({liveTrades.length})</h4>
            {liveTrades.length > 0 ? (
              <div style={{ maxHeight: '200px', overflow: 'auto' }}>
                <table border={1}>
                  <thead>
                    <tr>
                      {Object.keys(liveTrades[0]).map(k => (
                        <th key={k}>{k}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {liveTrades.map((t, i) => (
                      <tr key={i}>
                        {Object.keys(liveTrades[0]).map(k => (
                          <td key={k}>{formatVal(t[k])}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div>No live trades</div>
            )}
          </div>
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
