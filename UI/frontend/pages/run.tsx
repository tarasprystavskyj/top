import { useEffect, useState } from 'react';
import { apiFetch } from '../utils/api';

export default function Run() {
  const [cfgs, setCfgs] = useState<any[]>([]);
  const [cfg, setCfg] = useState('');
  const [bars, setBars] = useState(5000);
  const [job, setJob] = useState<any>(null);
  const [res, setRes] = useState<any>(null);
  const [slide, setSlide] = useState(0);

  useEffect(() => {
    apiFetch('/api/configs')
      .then(r => r.json())
      .then(setCfgs);
  }, []);

  async function start() {
    const j = await apiFetch('/api/backtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cfg_name: cfg, limit_bars: bars }),
    }).then(r => r.json());
    setJob(j);
    setRes(null);
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
        clearInterval(id);
      }
    }, 1000);
    return () => clearInterval(id);
  }, [job]);

  const plotNames = [
    'equity_by_time.png',
    'returns_hist.png',
    'equity_by_trade.png',
    'drawdown_by_trade.png',
  ];
  const plotUrls = plotNames
    .map(n => res?.artifacts?.[n])
    .filter(Boolean) as string[];

  return (
    <div>
      <h3>Run Backtest</h3>
      <div>
        <select value={cfg} onChange={e => setCfg(e.target.value)}>
          <option value=''>--pick config--</option>
          {cfgs.map((c: any) => (
            <option key={c.name} value={c.name}>
              {c.name}
            </option>
          ))}
        </select>
        <input
          type='number'
          value={bars}
          onChange={e => setBars(parseInt(e.target.value || '0'))}
        />
        <button onClick={start}>Start</button>
      </div>
      {job && <p>Job: {job.job_id}</p>}
      {res && (
        <div>
          <pre>{JSON.stringify(res.summary, null, 2)}</pre>
          {res.trades && res.trades.length > 0 && (
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
                      <td key={k}>{t[k]}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
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
        </div>
      )}
    </div>
  );
}

