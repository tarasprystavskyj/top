import { useEffect, useMemo, useState } from 'react';
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
  const liveEquitySeries = useMemo(() => buildLiveEquitySeries(liveTrades), [liveTrades]);
  const liveHeaders = useMemo(() => {
    if (!Array.isArray(liveTrades) || liveTrades.length === 0) return [] as string[];
    const keys = new Set<string>();
    for (const trade of liveTrades) {
      if (trade && typeof trade === 'object') {
        for (const key of Object.keys(trade)) keys.add(key);
      }
    }
    return Array.from(keys);
  }, [liveTrades]);

  const slideIndex = pairs.length > 0 ? Math.min(slide, pairs.length - 1) : 0;
  const currentPair = pairs.length > 0 ? pairs[slideIndex] : null;
  const currentPairName = currentPair?.name.toLowerCase() || '';
  const liveChartMode: 'trade' | 'time' = currentPairName.includes('time') ? 'time' : 'trade';
  const showLiveEquityChart = Boolean(currentPair && liveEquitySeries && currentPairName.includes('equity'));

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
        // Кожному «базовому» файлу даємо список альтернатив viz_*,
        // бо бекенд може породити саме їх.
        const plotDefs = [
          { key: 'equity_by_time.png',   alts: ['viz_equity_vs_time.png'] },
          { key: 'equity_by_trade.png',  alts: ['viz_equity_vs_trade.png'] },
          { key: 'drawdown_by_trade.png',alts: ['viz_dd_vs_trade.png'] },
          { key: 'returns_hist.png',     alts: [] },
        ];

        const pick = (arts: any, key: string, alts: string[]) => {
          if (!arts) return null;
          if (arts[key]) return arts[key];
          for (const a of alts) if (arts[a]) return arts[a];
          return null;
        };

        const label = (fn: string) =>
          fn
            .replace('equity_by_', 'equity: ')
            .replace('drawdown_by_', 'drawdown: ')
            .replace('.png', '')
            .replace(/_/g, ' ');

        const ps = plotDefs
          .map(({ key, alts }) => {
            const back = pick(data.backtest?.artifacts, key, alts);
            const live = pick(data.artifacts, key, alts);
            return back || live ? { name: label(key), back, live } : null;
          })
          .filter(Boolean) as { name: string; back: string | null; live: string | null }[];
        console.debug('Plot pairs', ps);
        console.debug('Backtest summary', data.backtest?.summary || null);
        setPairs(ps);
        setSummary(data.backtest?.summary || null);
        setTrades(data.backtest?.trades || []);
        setLiveTrades(data.live_trades || []);
        setSlide(0);
        setError(ps.length ? null : 'No data available for this session');
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

  const tableColumnWidth = 'calc((100% - 20px) / 2)';
  const fallbackTableWidth = 'calc((100% - 16px) / 2)';

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
          <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
            <div style={{ display: 'flex', gap: 20, flexWrap: 'wrap', alignItems: 'stretch' }}>
              <div style={{ flex: '1 1 320px', textAlign: 'center' }}>
                <h3>
                  Backtest {btRangeText ? `(${btRangeText})` : ''}
                </h3>
                {currentPair?.back && (
                  <img src={currentPair.back!} style={{ width: '100%', maxWidth: '420px' }} />
                )}
              </div>
              <div style={{ flex: '1 1 320px', textAlign: 'center' }}>
                <h3>
                  Live {liveRange ? `(${liveRange.start} — ${liveRange.end})` : ''}
                </h3>
                {showLiveEquityChart ? (
                  <LiveEquityChart series={liveEquitySeries!} mode={liveChartMode} />
                ) : currentPair?.live ? (
                  <img src={currentPair.live!} style={{ width: '100%', maxWidth: '420px' }} />
                ) : (
                  <div style={{ width: '100%', maxWidth: '420px', margin: '0 auto' }}>
                    No live trade data
                  </div>
                )}
              </div>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8, alignItems: 'center' }}>
              <strong>{currentPair?.name}</strong>
              {pairs.length > 1 && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 12, width: '100%', maxWidth: 520 }}>
                  <button onClick={() => setSlide((slide - 1 + pairs.length) % pairs.length)}>Prev</button>
                  <input
                    type="range"
                    min={0}
                    max={pairs.length - 1}
                    value={slideIndex}
                    onChange={e => setSlide(Number(e.target.value))}
                    style={{ flex: 1 }}
                  />
                  <button onClick={() => setSlide((slide + 1) % pairs.length)}>Next</button>
                </div>
              )}
            </div>
            {(trades.length > 0 || liveTrades.length > 0) && (
              <div style={{ display: 'flex', gap: 20, flexWrap: 'nowrap', alignItems: 'flex-start', overflowX: 'auto' }}>
                {trades.length > 0 && (
                  <div style={{ flex: `0 0 ${tableColumnWidth}`, width: tableColumnWidth }}>
                    <h4>Backtest trades ({trades.length})</h4>
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
                  </div>
                )}
                {liveTrades.length > 0 && (
                  <div style={{ flex: `0 0 ${tableColumnWidth}`, width: tableColumnWidth }}>
                    <h4>Live trades ({liveTrades.length})</h4>
                    <div style={{ maxHeight: '200px', overflow: 'auto' }}>
                      <table border={1}>
                        <thead>
                          <tr>
                            {liveHeaders.map(k => (
                              <th key={k}>{k}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {liveTrades.map((t, i) => {
                            const pnl = Number((t as any).realised_pnl);
                            return (
                              <tr key={i} style={pnl > 0 ? { backgroundColor: '#d4edda' } : undefined}>
                                {liveHeaders.map(k => (
                                  <td key={k}>{formatVal(t[k])}</td>
                                ))}
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
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
      {pairs.length === 0 && (trades.length > 0 || liveTrades.length > 0) && (
        <div style={{ display: 'flex', gap: 16, flexWrap: 'nowrap', overflowX: 'auto' }}>
          {trades.length > 0 && (
            <div style={{ flex: `0 0 ${fallbackTableWidth}`, width: fallbackTableWidth }}>
              <h4>Backtest trades ({trades.length})</h4>
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
            </div>
          )}
          {liveTrades.length > 0 && (
            <div style={{ flex: `0 0 ${fallbackTableWidth}`, width: fallbackTableWidth }}>
              <h4>Live trades ({liveTrades.length})</h4>
              <div style={{ maxHeight: '200px', overflow: 'auto' }}>
                <table border={1}>
                  <thead>
                    <tr>
                      {liveHeaders.map(k => (
                        <th key={k}>{k}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {liveTrades.map((t, i) => {
                      const pnl = Number((t as any).realised_pnl);
                      return (
                        <tr key={i} style={pnl > 0 ? { backgroundColor: '#d4edda' } : undefined}>
                          {liveHeaders.map(k => (
                            <td key={k}>{formatVal(t[k])}</td>
                          ))}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

type EquityPoint = {
  xValue: number;
  y: number;
  ts: number | null;
};

type LiveEquitySeries = {
  trade: EquityPoint[];
  time: EquityPoint[];
};

function LiveEquityChart({ series, mode }: { series: LiveEquitySeries; mode: 'trade' | 'time' }) {
  const hasTimePoints = series.time.length > 0;
  const effectiveMode: 'trade' | 'time' = mode === 'time' && hasTimePoints ? 'time' : 'trade';
  const points = effectiveMode === 'time' ? series.time : series.trade;

  if (!points.length) {
    return (
      <div style={{ width: '100%', maxWidth: '420px', margin: '0 auto' }}>
        Not enough live trades to plot equity
      </div>
    );
  }

  const width = 420;
  const height = 260;
  const padding = 36;
  const usableWidth = width - padding * 2;
  const usableHeight = height - padding * 2;
  const xMin = Math.min(...points.map(p => p.xValue));
  const xMax = Math.max(...points.map(p => p.xValue));
  const yMin = Math.min(...points.map(p => p.y));
  const yMax = Math.max(...points.map(p => p.y));
  const xRange = xMax - xMin;
  const yRange = yMax - yMin;

  const coords = points.map((point, idx) => {
    const x =
      xRange === 0
        ? padding + (points.length === 1 ? usableWidth / 2 : (idx / Math.max(points.length - 1, 1)) * usableWidth)
        : padding + ((point.xValue - xMin) / xRange) * usableWidth;
    const y =
      yRange === 0
        ? padding + usableHeight / 2
        : padding + ((yMax - point.y) / yRange) * usableHeight;
    return { x, y };
  });

  const pathData = coords
    .map((c, idx) => `${idx === 0 ? 'M' : 'L'}${c.x.toFixed(2)} ${c.y.toFixed(2)}`)
    .join(' ');

  const zeroWithinRange = yMin < 0 && yMax > 0;
  const zeroY = zeroWithinRange
    ? padding + ((yMax - 0) / (yRange || 1)) * usableHeight
    : null;

  const formatX = effectiveMode === 'time' ? formatTimeLabel : (value: number) => `#${Math.round(value)}`;
  const xStartLabel = formatX(points.length === 1 ? points[0].xValue : xMin);
  const xEndLabel = formatX(xMax);
  const xAxisTitle = effectiveMode === 'time' ? 'Time' : 'Trade #';

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      style={{ width: '100%', maxWidth: '420px', height: '260px', background: '#ffffff', border: '1px solid #d1d5db', borderRadius: 8 }}
    >
      <rect x={0} y={0} width={width} height={height} fill="#ffffff" rx={8} ry={8} />
      <g>
        <line x1={padding} y1={height - padding} x2={width - padding} y2={height - padding} stroke="#cbd5f5" strokeWidth={1} />
        <line x1={padding} y1={padding} x2={padding} y2={height - padding} stroke="#cbd5f5" strokeWidth={1} />
        {zeroY !== null && (
          <line x1={padding} y1={zeroY} x2={width - padding} y2={zeroY} stroke="#94a3b8" strokeDasharray="4 4" strokeWidth={1} />
        )}
        <path d={pathData} stroke="#2563eb" strokeWidth={2} fill="none" />
        {coords.map((c, idx) => (
          <circle key={idx} cx={c.x} cy={c.y} r={3} fill="#2563eb" />
        ))}
        <text x={padding - 8} y={padding + 12} textAnchor="end" fontSize={12} fill="#475569">
          {formatAxisNumber(yMax)}
        </text>
        <text x={padding - 8} y={height - padding} textAnchor="end" fontSize={12} fill="#475569">
          {formatAxisNumber(yMin)}
        </text>
        <text x={padding} y={height - padding + 20} textAnchor="start" fontSize={12} fill="#475569">
          {xStartLabel}
        </text>
        <text x={width - padding} y={height - padding + 20} textAnchor="end" fontSize={12} fill="#475569">
          {xEndLabel}
        </text>
        <text x={width / 2} y={height - 8} textAnchor="middle" fontSize={12} fill="#475569">
          {xAxisTitle}
        </text>
        <text
          x={-height / 2}
          y={padding - 20}
          transform="rotate(-90)"
          textAnchor="middle"
          fontSize={12}
          fill="#475569"
        >
          Equity (cum. PnL)
        </text>
      </g>
    </svg>
  );
}

function buildLiveEquitySeries(trades: any[]): LiveEquitySeries | null {
  if (!Array.isArray(trades) || trades.length === 0) {
    return null;
  }
  const candidates = ['realised_pnl', 'realized_pnl', 'realizedPnl', 'pnl', 'profit'];
  const pnlKey = candidates.find(key =>
    trades.some(t => t && t[key] !== undefined && t[key] !== null && String(t[key]).trim() !== '')
  );
  if (!pnlKey) return null;

  const tradePoints: EquityPoint[] = [];
  const timePoints: EquityPoint[] = [];
  let cumulative = 0;

  trades.forEach((trade, idx) => {
    const raw = trade?.[pnlKey];
    const pnl = Number(raw);
    if (!Number.isFinite(pnl)) {
      return;
    }
    cumulative += pnl;
    const tsCandidate = trade?.exit_fill_ts ?? trade?.exit_ts ?? trade?.close_time ?? trade?.ts ?? trade?.timestamp;
    const tsDate = tsCandidate ? new Date(tsCandidate) : null;
    const ts = tsDate && !Number.isNaN(tsDate.getTime()) ? tsDate.getTime() : null;
    tradePoints.push({ xValue: idx + 1, y: cumulative, ts });
    if (ts !== null) {
      timePoints.push({ xValue: ts, y: cumulative, ts });
    }
  });

  if (!tradePoints.length) {
    return null;
  }

  timePoints.sort((a, b) => a.xValue - b.xValue);

  return { trade: tradePoints, time: timePoints };
}

function formatAxisNumber(value: number) {
  if (!Number.isFinite(value)) return '';
  const abs = Math.abs(value);
  if (abs >= 1000) return value.toFixed(0);
  if (abs >= 100) return value.toFixed(1);
  if (abs >= 1) return value.toFixed(2);
  return value.toFixed(4);
}

function formatTimeLabel(value: number) {
  if (!Number.isFinite(value)) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
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
