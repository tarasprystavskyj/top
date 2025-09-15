import { useEffect, useState } from 'react';
import { useRouter } from 'next/router';
import { apiFetch } from '../utils/api';

type CacheDbOption = {
  name: string;
  path: string;
};

type JobStatus = {
  status?: string;
  message?: string;
  progress?: number;
  expected_duration_seconds?: number;
  eta_seconds?: number;
  elapsed_seconds?: number;
  symbol_count?: number;
  limit_bars?: number;
};

const spinnerFrames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

export default function Run() {
  const router = useRouter();
  const [cfgs, setCfgs] = useState<any[]>([]);
  const [cfg, setCfg] = useState('');
  const [universes, setUniverses] = useState<string[]>([]);
  const [universe, setUniverse] = useState('');
  const [bars, setBars] = useState(5000);
  const [job, setJob] = useState<any>(null);
  const [jobStatus, setJobStatus] = useState<JobStatus | null>(null);
  const [res, setRes] = useState<any>(null);
  const [slide, setSlide] = useState(0);
  const [debug, setDebug] = useState(false);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [showTrades, setShowTrades] = useState(true);
  const [logs, setLogs] = useState('');
  const [backtesters, setBacktesters] = useState<string[]>([]);
  const [backtester, setBacktester] = useState('');
  const [cacheDbs, setCacheDbs] = useState<CacheDbOption[]>([]);
  const [cacheDb, setCacheDb] = useState('');
  const [spinnerIndex, setSpinnerIndex] = useState(0);

  // if ?id=JOB_ID is present load that job's result
  useEffect(() => {
    const qid = router.query.id;
    if (qid && typeof qid === 'string') {
      setJob({ job_id: qid });
    }
    const qcache = router.query.cache_db;
    if (qcache && typeof qcache === 'string') {
      setCacheDb(qcache);
    }
  }, [router.query.id, router.query.cache_db]);

  useEffect(() => {
    apiFetch('/api/configs')
      .then(r => r.json())
      .then(setCfgs);
  }, []);

  useEffect(() => {
    apiFetch('/api/universes')
      .then(r => r.json())
      .then(data => {
        if (Array.isArray(data)) setUniverses(data);
        else setUniverses([]);
      })
      .catch(() => setUniverses([]));
  }, []);

  useEffect(() => {
    apiFetch('/api/backtesters')
      .then(r => r.json())
      .then(data => {
        setBacktesters(data.versions || []);
        if (data.current) setBacktester(data.current);
      });
  }, []);

  useEffect(() => {
    apiFetch('/api/cache_dbs')
      .then(r => r.json())
      .then(data => {
        if (!Array.isArray(data)) {
          setCacheDbs([]);
          return;
        }
        const normalized: CacheDbOption[] = data
          .filter((item: any) => item && typeof item.name === 'string' && typeof item.path === 'string')
          .map((item: any) => ({ name: item.name, path: item.path }));
        setCacheDbs(normalized);
      })
      .catch(() => setCacheDbs([]));
  }, []);

  async function start() {
    const override: Record<string, any> = {};
    if (universe) {
      const universePath = `universe/${universe}`;
      override.symbols_file = universePath;
      override.universe_file = universePath;
      override.universe = { file: universePath };
    }
    const payloadOverride = Object.keys(override).length > 0 ? override : undefined;
    // Trim any stray whitespace. Backend resolves the value relative to the repository.
    const cacheDbPath = cacheDb.trim();
    const j = await apiFetch('/api/backtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        cfg_name: cfg,
        limit_bars: bars,
        debug,
        override: payloadOverride,
        backtester,
        cache_db: cacheDbPath || undefined,
      }),
    }).then(r => r.json());
    setJob(j);
    setJobStatus(null);
    setSpinnerIndex(0);
    setRes(null);
    setErrMsg(null);
    setLogs('');
  }

  useEffect(() => {
    if (!job) return;
    let cancelled = false;
    let interval: ReturnType<typeof setInterval> | undefined;
    const fetchStatus = async () => {
      try {
        const st: JobStatus = await apiFetch('/api/jobs/' + job.job_id + '/status').then(r =>
          r.json()
        );
        if (cancelled) return;
        setJobStatus(st);
        if (st.status === 'done' || st.status === 'error') {
          if (interval) clearInterval(interval);
          const rs = await apiFetch('/api/jobs/' + job.job_id + '/result').then(r =>
            r.json()
          );
          if (cancelled) return;
          setRes(rs);
          if (st.status === 'error') setErrMsg(st.message || 'error');
        }
      } catch (err) {
        if (!cancelled) {
          console.error('Failed to fetch job status', err);
        }
      }
    };
    fetchStatus();
    interval = setInterval(fetchStatus, 1000);
    return () => {
      cancelled = true;
      if (interval) clearInterval(interval);
    };
  }, [job]);

  useEffect(() => {
    if (res && (debug || errMsg) && res.artifacts?.['logs.txt']) {
      fetch(res.artifacts['logs.txt'])
        .then(r => r.text())
        .then(setLogs)
        .catch(() => {});
    }
  }, [res, debug, errMsg]);

  useEffect(() => {
    if (!jobStatus) {
      setSpinnerIndex(0);
      return;
    }
    const totalExpected =
      jobStatus.expected_duration_seconds ??
      (jobStatus.elapsed_seconds != null && jobStatus.eta_seconds != null
        ? jobStatus.elapsed_seconds + jobStatus.eta_seconds
        : undefined);
    const fallbackElapsed = jobStatus.elapsed_seconds ?? 0;
    const shouldAnimate =
      (jobStatus.status === 'running' || jobStatus.status === 'queued') &&
      ((totalExpected ?? fallbackElapsed) > 5);
    if (!shouldAnimate) {
      setSpinnerIndex(0);
      return;
    }
    const id = setInterval(() => {
      setSpinnerIndex(prev => (prev + 1) % spinnerFrames.length);
    }, 120);
    return () => clearInterval(id);
  }, [jobStatus]);

  const plotNames = [
    'equity_by_time.png',
    'returns_hist.png',
    'equity_by_trade.png',
    'drawdown_by_trade.png',
  ];
  const plotUrls = plotNames
    .map(n => res?.artifacts?.[n])
    .filter(Boolean) as string[];

  const vizNames = [
    'viz_equity_vs_trade.png',
    'viz_dd_vs_trade.png',
    'viz_equity_vs_time.png',
  ];
  const vizUrls = vizNames
    .map(n => res?.artifacts?.[n])
    .filter(Boolean) as string[];

  const progressValue =
    typeof jobStatus?.progress === 'number'
      ? Math.min(Math.max(jobStatus.progress, 0), 1)
      : null;
  const progressPercent =
    progressValue !== null ? Math.round(progressValue * 100) : null;
  const totalExpectedSeconds =
    jobStatus?.expected_duration_seconds ??
    (jobStatus?.elapsed_seconds != null && jobStatus?.eta_seconds != null
      ? jobStatus.elapsed_seconds + jobStatus.eta_seconds
      : undefined);
  const shouldShowSpinner =
    !!jobStatus &&
    (jobStatus.status === 'running' || jobStatus.status === 'queued') &&
    ((totalExpectedSeconds ?? jobStatus.elapsed_seconds ?? 0) > 5);

  const isReadOnly = !!router.query.id;
  const hasCustomCacheDb = cacheDb !== '' && !cacheDbs.some(opt => opt.path === cacheDb);
  const displayedCacheDbs: CacheDbOption[] = hasCustomCacheDb
    ? [...cacheDbs, { name: `Custom: ${cacheDb}`, path: cacheDb }]
    : cacheDbs;

  return (
    <div>
      <h3>Run Backtest</h3>
      {!isReadOnly && (
        <div>
          <div>
            <select value={cfg} onChange={e => setCfg(e.target.value)}>
              <option value=''>--pick config--</option>
              {cfgs.map((c: any) => (
                <option key={c.name} value={c.name}>
                  {c.name}
                </option>
              ))}
            </select>
            <select value={universe} onChange={e => setUniverse(e.target.value)}>
              <option value=''>--no universe--</option>
              {universes.map(u => (
                <option key={u} value={u}>
                  {u}
                </option>
              ))}
            </select>
            <input
              type='number'
              value={bars}
              onChange={e => setBars(parseInt(e.target.value || '0'))}
            />
            <select
              value={cacheDb}
              onChange={e => setCacheDb(e.target.value)}
              style={{ width: '300px' }}
            >
              <option value=''>--select cache db--</option>
              {displayedCacheDbs.map(opt => (
                <option key={opt.path} value={opt.path}>
                  {opt.name}
                </option>
              ))}
            </select>
            <label>
              <input
                type='checkbox'
                checked={debug}
                onChange={e => setDebug(e.target.checked)}
              />
              Debug
            </label>
            <label>
              <input
                type='checkbox'
                checked={showTrades}
                onChange={e => setShowTrades(e.target.checked)}
              />
              trades
            </label>
            <button onClick={start}>Start</button>
          </div>
          <div>
            <select value={backtester} onChange={e => setBacktester(e.target.value)}>
              {backtesters.map(b => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            </select>
          </div>
        </div>
      )}
      {job && (
        <div style={{ marginTop: '10px', marginBottom: '10px' }}>
          <p style={{ marginBottom: '6px' }}>
            Job: <code>{job.job_id}</code>
          </p>
          {jobStatus ? (
            <div
              style={{
                border: '1px solid #ddd',
                padding: '10px',
                borderRadius: '6px',
                maxWidth: '440px',
              }}
            >
              <div style={{ fontWeight: 600 }}>
                Status: {jobStatus.status || 'unknown'}
                {jobStatus.status === 'error' && jobStatus.message && (
                  <span style={{ color: 'red' }}> ({jobStatus.message})</span>
                )}
              </div>
              {shouldShowSpinner && (
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                    fontFamily: 'monospace',
                    marginTop: '6px',
                  }}
                >
                  <span>{spinnerFrames[spinnerIndex % spinnerFrames.length]}</span>
                  <span>Backtest running...</span>
                </div>
              )}
              {progressPercent !== null && (
                <div style={{ marginTop: '8px' }}>
                  <div
                    style={{
                      height: '8px',
                      background: '#eee',
                      borderRadius: '4px',
                      overflow: 'hidden',
                    }}
                  >
                    <div
                      style={{
                        width: `${Math.min(100, Math.max(0, progressPercent))}%`,
                        background: '#4a90e2',
                        height: '100%',
                        transition: 'width 0.4s ease',
                      }}
                    />
                  </div>
                  <div style={{ marginTop: '4px', fontSize: '0.9em' }}>
                    Progress: {Math.min(100, Math.max(0, progressPercent))}%
                  </div>
                </div>
              )}
              <div
                style={{
                  display: 'flex',
                  flexWrap: 'wrap',
                  gap: '10px',
                  marginTop: '8px',
                  fontSize: '0.85em',
                }}
              >
                {jobStatus.limit_bars != null && <span>Bars: {jobStatus.limit_bars}</span>}
                {jobStatus.symbol_count != null && <span>Symbols: {jobStatus.symbol_count}</span>}
                {jobStatus.elapsed_seconds != null && (
                  <span>Elapsed: {formatDuration(jobStatus.elapsed_seconds)}</span>
                )}
                {jobStatus.eta_seconds != null &&
                  (jobStatus.status === 'running' || jobStatus.status === 'queued') && (
                    <span>ETA: {formatDuration(jobStatus.eta_seconds)}</span>
                  )}
                {totalExpectedSeconds != null && totalExpectedSeconds > 0 && (
                  <span>Estimated total: {formatDuration(totalExpectedSeconds)}</span>
                )}
              </div>
            </div>
          ) : (
            <div style={{ color: '#666' }}>Loading status...</div>
          )}
        </div>
      )}
      {res && (
        <div>
          {errMsg && <pre style={{ color: 'red' }}>Error: {errMsg}</pre>}
          <pre>{JSON.stringify(res.summary, null, 2)}</pre>
          {plotUrls.length > 0 && (
            <div>
              <img
                src={plotUrls[slide]}
                style={{ maxWidth: '600px', display: 'block' }}
              />
              <div>
                <button
                  onClick={() =>
                    setSlide((slide - 1 + plotUrls.length) % plotUrls.length)
                  }
                >
                  Prev
                </button>
                <button onClick={() => setSlide((slide + 1) % plotUrls.length)}>
                  Next
                </button>
              </div>
            </div>
          )}
          {vizUrls.length > 0 && (
            <div
              style={{
                display: 'flex',
                gap: '10px',
                flexWrap: 'wrap',
                marginTop: '10px',
              }}
            >
              {vizUrls.map((u, i) => (
                <img key={i} src={u} style={{ maxWidth: '400px' }} />
              ))}
            </div>
          )}
          {res.debug && (
            <div style={{ marginTop: '10px' }}>
              <strong>Debug info</strong>
              {res.debug.cmd && (
                <pre
                  style={{
                    whiteSpace: 'pre-wrap',
                    background: '#f5f5f5',
                    padding: '6px',
                    overflowX: 'auto',
                  }}
                >
                  {res.debug.cmd}
                </pre>
              )}
              {res.debug.cache_db && (
                <div>
                  Cache DB: <code>{res.debug.cache_db}</code>
                  {res.debug.cache_db_exists === false && (
                    <span style={{ color: 'red' }}> (missing)</span>
                  )}
                </div>
              )}
              {res.debug.cache_db_label &&
                (!res.debug.cache_db || res.debug.cache_db_label !== res.debug.cache_db) && (
                  <div>
                    Selected value: <code>{res.debug.cache_db_label}</code>
                  </div>
                )}
            </div>
          )}
          {logs && (
            <pre style={{ maxHeight: '200px', overflowY: 'auto' }}>{logs}</pre>
          )}
          {showTrades && res.trades && res.trades.length > 0 && (
            <div style={{ maxHeight: '200px', overflow: 'auto' }}>
              <table border={1}>
                <thead>
                  <tr>
                    {Object.keys(res.trades[0]).map(k => (
                      <th key={k}>{k}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {res.trades.map((t: any, i: number) => (
                    <tr key={i}>
                      {Object.keys(res.trades[0]).map(k => (
                        <td key={k}>{formatVal(t[k])}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function formatDuration(seconds?: number | null) {
  if (seconds == null || !isFinite(seconds)) return '';
  const total = Math.max(0, Math.round(seconds));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  if (mins > 0) {
    return `${mins}m ${secs}s`;
  }
  return `${secs}s`;
}

function formatVal(v: any) {
  const num = Number(v);
  return isNaN(num) ? v : num.toFixed(3);
}

