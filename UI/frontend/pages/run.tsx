import { useEffect, useState } from 'react';
import { useRouter } from 'next/router';
import { apiFetch } from '../utils/api';

export default function Run() {
  const router = useRouter();
  const [cfgs, setCfgs] = useState<any[]>([]);
  const [cfg, setCfg] = useState('');
  const [universes, setUniverses] = useState<string[]>([]);
  const [universe, setUniverse] = useState('');
  const [bars, setBars] = useState(5000);
  const [job, setJob] = useState<any>(null);
  const [res, setRes] = useState<any>(null);
  const [slide, setSlide] = useState(0);
  const [debug, setDebug] = useState(false);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [showTrades, setShowTrades] = useState(true);
  const [logs, setLogs] = useState('');
  const [backtesters, setBacktesters] = useState<string[]>([]);
  const [backtester, setBacktester] = useState('');
  const [cacheDb, setCacheDb] = useState('');

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

  async function start() {
    const override = {
      symbols_file: universe ? `universe/${universe}` : null,
    };
    // Trim any stray whitespace. Backend will resolve relative paths, so we
    // forward the path as-is without prepending directories that may create
    // incorrect locations.
    const cacheDbPath = cacheDb.trim();
    console.log('cacheDbPath', cacheDbPath);
    const j = await apiFetch('/api/backtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cfg_name: cfg, limit_bars: bars, debug, override, backtester, cache_db: cacheDbPath || undefined }),
    }).then(r => r.json());
    setJob(j);
    setRes(null);
    setErrMsg(null);
    setLogs('');
  }

  useEffect(() => {
    if (!job) return;
    const id = setInterval(async () => {
      const st = await apiFetch('/api/jobs/' + job.job_id + '/status').then(r =>
        r.json()
      );
      if (st.status === 'done' || st.status === 'error') {
        const rs = await apiFetch('/api/jobs/' + job.job_id + '/result').then(r =>
          r.json()
        );
        setRes(rs);
        if (st.status === 'error') setErrMsg(st.message || 'error');
        clearInterval(id);
      }
    }, 1000);
    return () => clearInterval(id);
  }, [job]);

  useEffect(() => {
    if (res && (debug || errMsg) && res.artifacts?.['logs.txt']) {
      fetch(res.artifacts['logs.txt'])
        .then(r => r.text())
        .then(setLogs)
        .catch(() => {});
    }
  }, [res, debug, errMsg]);

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

  const isReadOnly = !!router.query.id;

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
            <input
              type='text'
              placeholder='cache db path'
              value={cacheDb}
              onChange={e => setCacheDb(e.target.value)}
              style={{ width: '300px' }}
            />
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
      {job && <p>Job: {job.job_id}</p>}
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

function formatVal(v: any) {
  const num = Number(v);
  return isNaN(num) ? v : num.toFixed(3);
}

