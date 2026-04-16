import { useEffect, useMemo, useState } from 'react';
import { apiFetch } from '../utils/api';
import { avgPriceDiffColorRule, computePollingIntervalMs, seriesToSvgPath } from '../utils/backtestValidation';

type FileEntry = { name: string; path: string; size: number; modified_at: string; symbol_guess?: string | null };

type Point = { ts: string; value: number };

function LineChart({ title, series }: { title: string; series: { name: string; color: string; data: Point[] }[] }) {
  const width = 920;
  const height = 320;
  const padding = 46;
  const merged = series.flatMap((s, sIdx) =>
    s.data.map((d, i) => {
      const tsMs = Date.parse(String(d.ts || ''));
      return { x: Number.isFinite(tsMs) ? tsMs : i + sIdx * 0.0001, y: Number(d.value) || 0 };
    }),
  );
  if (!merged.length) return <div style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 12 }}>{title}: No data</div>;
  const xMin = Math.min(...merged.map(p => p.x));
  const xMax = Math.max(...merged.map(p => p.x));
  const yMin = Math.min(...merged.map(p => p.y));
  const yMax = Math.max(...merged.map(p => p.y));
  const hasTimeX = merged.some(p => p.x > 10_000_000_000);

  const fmtTime = (ms: number) => {
    if (!Number.isFinite(ms)) return '';
    return new Date(ms).toISOString().slice(5, 16).replace('T', ' ');
  };
  const yTickVals = [yMin, (yMin + yMax) / 2, yMax];
  const xTickVals = [xMin, (xMin + xMax) / 2, xMax];

  return (
    <div style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 12, background: '#fff' }}>
      <h4 style={{ marginTop: 0 }}>{title}</h4>
      <svg viewBox={`0 0 ${width} ${height}`} style={{ width: '100%', height: 320 }}>
        <rect x={0} y={0} width={width} height={height} fill="#fff" />
        <line x1={padding} y1={height - padding} x2={width - padding} y2={height - padding} stroke="#cbd5e1" />
        <line x1={padding} y1={padding} x2={padding} y2={height - padding} stroke="#cbd5e1" />
        {yTickVals.map((v, i) => {
          const y = height - padding - ((v - yMin) / (Math.max(1e-9, yMax - yMin))) * (height - padding * 2);
          return (
            <g key={`y-${i}`}>
              <line x1={padding} y1={y} x2={width - padding} y2={y} stroke="#e2e8f0" strokeDasharray="4 4" />
              <text x={padding - 8} y={y + 4} textAnchor="end" fontSize="11" fill="#475569">
                {v.toFixed(2)}
              </text>
            </g>
          );
        })}
        {xTickVals.map((v, i) => {
          const x = padding + ((v - xMin) / (Math.max(1e-9, xMax - xMin))) * (width - padding * 2);
          return (
            <g key={`x-${i}`}>
              <line x1={x} y1={padding} x2={x} y2={height - padding} stroke="#e2e8f0" strokeDasharray="4 4" />
              <text x={x} y={height - padding + 16} textAnchor="middle" fontSize="11" fill="#475569">
                {hasTimeX ? fmtTime(v) : v.toFixed(0)}
              </text>
            </g>
          );
        })}
        {series.map(s => {
          const pts = s.data.map((d, i) => {
            const tsMs = Date.parse(String(d.ts || ''));
            return { x: Number.isFinite(tsMs) ? tsMs : i, y: Number(d.value) || 0 };
          });
          const path = seriesToSvgPath(pts, width, height, padding);
          return <path key={s.name} d={path} stroke={s.color} strokeWidth={2} fill="none" />;
        })}
        <text x={padding} y={padding - 10} fontSize="11" fill="#334155">
          Y: value
        </text>
        <text x={width - padding} y={height - 8} textAnchor="end" fontSize="11" fill="#334155">
          X: {hasTimeX ? 'timestamp' : 'index'}
        </text>
      </svg>
      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        {series.map(s => (
          <span key={s.name} style={{ color: s.color, fontWeight: 600 }}>{s.name}</span>
        ))}
      </div>
    </div>
  );
}

export default function BacktestLiveValidationPage() {
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [selectedPath, setSelectedPath] = useState('');
  const [inspect, setInspect] = useState<any>(null);
  const [runData, setRunData] = useState<any>(null);
  const [runId, setRunId] = useState<string>('');
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [debugMode, setDebugMode] = useState(true);
  const [debugOpen, setDebugOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastPayload, setLastPayload] = useState<any>(null);
  const [lastRefreshTs, setLastRefreshTs] = useState<string>('');

  const pollingIntervalMs = useMemo(() => {
    return computePollingIntervalMs(inspect?.bar_interval_seconds || runData?.inspect?.bar_interval_seconds || 60);
  }, [inspect?.bar_interval_seconds, runData?.inspect?.bar_interval_seconds]);

  useEffect(() => {
    apiFetch('/api/backtest_live_validation/files')
      .then(r => r.json())
      .then(d => setFiles(Array.isArray(d?.files) ? d.files : []))
      .catch(() => setFiles([]));
  }, []);

  useEffect(() => {
    if (!runId || !autoRefresh) return;
    const timer = setInterval(() => {
      apiFetch(`/api/backtest_live_validation/run/${runId}/poll`)
        .then(r => r.json())
        .then(d => {
          setRunData((prev: any) => ({ ...prev, ...d }));
          setLastPayload(d);
          setLastRefreshTs(new Date().toISOString());
        })
        .catch(() => undefined);
    }, pollingIntervalMs);
    return () => clearInterval(timer);
  }, [runId, autoRefresh, pollingIntervalMs]);

  async function inspectPath(path: string) {
    setSelectedPath(path);
    setRunId('');
    setRunData(null);
    if (!path) return;
    setLoading(true);
    setError(null);
    const payload = { path };
    setLastPayload(payload);
    try {
      const r = await apiFetch('/api/backtest_live_validation/inspect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) throw new Error('Inspect failed');
      const data = await r.json();
      setInspect(data);
    } catch (e: any) {
      setError(e?.message || 'inspect failed');
      setInspect(null);
    } finally {
      setLoading(false);
    }
  }

  async function onUpload(file: File) {
    setError(null);
    setLoading(true);
    try {
      const fd = new FormData();
      fd.append('file', file);
      const r = await apiFetch('/api/backtest_live_validation/upload', { method: 'POST', body: fd });
      if (!r.ok) {
        const txt = await r.text().catch(() => '');
        throw new Error(txt || `upload failed (HTTP ${r.status})`);
      }
      const d = await r.json();
      setSelectedPath(d.path);
      setInspect(d.inspect);
      setRunId('');
      setRunData(null);
      setLastPayload({ upload: file.name, upload_response: d });
    } catch (e: any) {
      setError(e?.message || 'upload failed');
    } finally {
      setLoading(false);
    }
  }

  async function runComparison() {
    if (!selectedPath) return;
    setLoading(true);
    setError(null);
    const payload = { path: selectedPath, auto_fetch_live: true, run_match: true, debug: debugMode };
    setLastPayload(payload);
    try {
      const r = await apiFetch('/api/backtest_live_validation/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) throw new Error('run failed');
      const d = await r.json();
      setRunId(d.run_id);
      const details = await apiFetch(`/api/backtest_live_validation/run/${d.run_id}`).then(x => x.json());
      setRunData(details);
      setLastRefreshTs(new Date().toISOString());
    } catch (e: any) {
      setError(e?.message || 'run failed');
    } finally {
      setLoading(false);
    }
  }

  const avgDiffColor = runData?.stats?.comparison?.avg_price_diff_color || avgPriceDiffColorRule(runData?.stats?.live?.current_average_price, runData?.stats?.backtest?.current_average_price);

  return (
    <div style={{ maxWidth: 1100, margin: '0 auto', padding: 16, display: 'flex', flexDirection: 'column', gap: 14 }}>
      <h3>Backtest vs Live Validation</h3>

      <div style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 12 }}>
        <h4>Source selection</h4>
        <select value={selectedPath} onChange={e => inspectPath(e.target.value)} style={{ minWidth: 420 }}>
          <option value="">--select TradingView source--</option>
          {files.map(f => <option key={f.path} value={f.path}>{f.name}</option>)}
        </select>
        <div style={{ marginTop: 10 }}>
          <input type="file" accept=".csv" onChange={e => e.target.files?.[0] && onUpload(e.target.files[0])} />
        </div>
        {inspect && (
          <pre style={{ background: '#f8fafc', padding: 8, borderRadius: 6, overflowX: 'auto' }}>{JSON.stringify(inspect, null, 2)}</pre>
        )}
      </div>

      <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
        <button onClick={runComparison} disabled={!selectedPath || loading}>{loading ? 'Running...' : 'Run / Refresh comparison'}</button>
        <label><input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)} /> Auto refresh</label>
        <label><input type="checkbox" checked={debugMode} onChange={e => setDebugMode(e.target.checked)} /> Debug mode</label>
        <span>Polling interval: {pollingIntervalMs} ms</span>
        {lastRefreshTs && <span>Last refresh: {lastRefreshTs}</span>}
      </div>

      {error && <p style={{ color: '#dc2626' }}>{error}</p>}

      {runData && (
        <>
          {runData?.fallback_canvas_url && (
            <details style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 12, background: '#fff' }} open>
              <summary style={{ cursor: 'pointer', fontWeight: 700 }}>Python matcher canvas (reference)</summary>
              <div style={{ marginTop: 10 }}>
                <img src={runData.fallback_canvas_url} style={{ width: '100%', maxWidth: 1200 }} />
              </div>
            </details>
          )}
          <LineChart
            title="PNL comparison"
            series={[
              { name: 'Backtest cumulative PNL', color: '#2563eb', data: runData?.pnl_chart?.backtest || [] },
              { name: 'Live cumulative PNL', color: '#16a34a', data: runData?.pnl_chart?.live || [] },
            ]}
          />

          <LineChart
            title="Margin usage"
            series={[
              { name: 'Backtest used margin %', color: '#2563eb', data: runData?.margin_chart?.backtest_margin_used_pct || [] },
              { name: 'Live used margin %', color: '#16a34a', data: runData?.margin_chart?.live_margin_used_pct || [] },
              { name: 'Live free margin %', color: '#f59e0b', data: runData?.margin_chart?.live_free_margin_pct || [] },
            ]}
          />

          <LineChart
            title="Slippage"
            series={[
              { name: 'Signed bps', color: '#7c3aed', data: runData?.slippage_chart?.signed_bps || [] },
              { name: 'Abs bps', color: '#dc2626', data: runData?.slippage_chart?.abs_bps || [] },
              { name: 'Rolling signed bps', color: '#0f766e', data: runData?.slippage_chart?.rolling_signed_bps || [] },
            ]}
          />

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <pre style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 10 }}>Backtest\n{JSON.stringify(runData?.stats?.backtest || {}, null, 2)}</pre>
            <pre style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 10 }}>Live\n{JSON.stringify(runData?.stats?.live || {}, null, 2)}</pre>
          </div>
          <div style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 10 }}>
            <strong>Comparison</strong>
            <pre>{JSON.stringify(runData?.stats?.comparison || {}, null, 2)}</pre>
            <div style={{ color: avgDiffColor, fontWeight: 700 }}>
              Avg price diff: {Number(runData?.stats?.comparison?.avg_price_diff || 0).toFixed(6)} ({avgDiffColor})
            </div>
          </div>
        </>
      )}

      <div style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 10 }}>
        <button onClick={() => setDebugOpen(v => !v)}>{debugOpen ? 'Hide debug' : 'Show debug'}</button>
        {debugOpen && (
          <pre style={{ maxHeight: 360, overflow: 'auto' }}>
            {JSON.stringify({
              selected_backtest_file_path: selectedPath,
              inspect,
              api_request_payload: lastPayload,
              last_refresh_time: lastRefreshTs,
              current_polling_interval: pollingIntervalMs,
              backend_debug: runData?.debug,
              loading,
            }, null, 2)}
          </pre>
        )}
      </div>
    </div>
  );
}
