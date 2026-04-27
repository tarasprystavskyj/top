import React, { useEffect, useMemo, useState } from 'react';
import Head from 'next/head';

type DeployResponse = {
  success: boolean;
  message: string;
  output?: string;
  error?: string;
  timestamp?: string;
};

type StatusResponse = {
  success: boolean;
  repo_path?: string;
  current_commit?: string;
  has_local_changes?: boolean;
  timestamp?: string;
  message?: string;
  error?: string;
};

function normalizeBase(value: string | undefined | null): string {
  const raw = String(value || '').trim();
  if (!raw) return '';
  return raw.endsWith('/') ? raw.slice(0, -1) : raw;
}

function apiUrl(path: string): string {
  // Default: same-origin /api/... .
  // Browser calls http://vps2.happyuser.info:3001/api/...
  // Next.js rewrites it server-side to BACKEND_URL=http://127.0.0.1:8001 .
  const base = normalizeBase(process.env.NEXT_PUBLIC_DEPLOY_API_BASE);
  return `${base}${path}`;
}

async function readJsonResponse<T>(response: Response): Promise<T> {
  const text = await response.text();
  let data: any = null;

  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = {
        success: false,
        message: `Backend returned non-JSON response: HTTP ${response.status}`,
        error: text.slice(0, 4000),
        timestamp: new Date().toISOString(),
      };
    }
  } else {
    data = {
      success: response.ok,
      message: response.ok ? 'OK' : `HTTP ${response.status}`,
      timestamp: new Date().toISOString(),
    };
  }

  if (!response.ok && data && typeof data === 'object') {
    data.success = false;
    data.message = data.message || `HTTP ${response.status}`;
  }

  return data as T;
}

function formatTime(value?: string): string {
  if (!value) return '-';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString('uk-UA');
}

export default function DeployPage() {
  const [secret, setSecret] = useState('');
  const [loading, setLoading] = useState(false);
  const [statusLoading, setStatusLoading] = useState(false);
  const [result, setResult] = useState<DeployResponse | null>(null);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(false);

  const apiBaseLabel = useMemo(() => {
    const base = normalizeBase(process.env.NEXT_PUBLIC_DEPLOY_API_BASE);
    return base || 'same-origin /api через Next.js rewrite';
  }, []);

  useEffect(() => {
    const savedSecret = window.localStorage.getItem('deploy_secret') || '';
    if (savedSecret) {
      setSecret(savedSecret);
      void fetchStatus(savedSecret);
    }
  }, []);

  useEffect(() => {
    if (!autoRefresh || !secret) return;

    const interval = window.setInterval(() => {
      void fetchStatus(secret);
    }, 10000);

    return () => window.clearInterval(interval);
  }, [autoRefresh, secret]);

  async function fetchStatus(secretKey: string) {
    if (!secretKey) return;

    setStatusLoading(true);
    try {
      const response = await fetch(
        apiUrl(`/api/deploy/status?secret=${encodeURIComponent(secretKey)}`),
        { method: 'GET' }
      );
      const data = await readJsonResponse<StatusResponse>(response);
      setStatus(data);
    } catch (error: any) {
      setStatus({
        success: false,
        message: `Fetch failed: ${error?.message || String(error)}`,
        timestamp: new Date().toISOString(),
      });
    } finally {
      setStatusLoading(false);
    }
  }

  async function handleDeploy(e: React.FormEvent) {
    e.preventDefault();

    const trimmedSecret = secret.trim();
    if (!trimmedSecret) {
      alert('Введіть секретний ключ');
      return;
    }

    setLoading(true);
    setResult(null);
    window.localStorage.setItem('deploy_secret', trimmedSecret);

    try {
      const response = await fetch(apiUrl('/api/deploy/pull'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ secret: trimmedSecret }),
      });

      const data = await readJsonResponse<DeployResponse>(response);
      setResult(data);
      await fetchStatus(trimmedSecret);
    } catch (error: any) {
      setResult({
        success: false,
        message: `Fetch failed: ${error?.message || String(error)}`,
        timestamp: new Date().toISOString(),
      });
    } finally {
      setLoading(false);
    }
  }

  function handleRefreshStatus() {
    void fetchStatus(secret.trim());
  }

  function handleClearSecret() {
    setSecret('');
    window.localStorage.removeItem('deploy_secret');
    setStatus(null);
    setResult(null);
  }

  return (
    <div style={{ padding: '20px', maxWidth: '860px', margin: '0 auto', fontFamily: 'Arial, sans-serif' }}>
      <Head>
        <title>Git Deploy - Admin</title>
      </Head>

      <h1>🚀 Git Deploy Manager</h1>

      <div style={{ background: '#f5f5f5', padding: '20px', borderRadius: '8px', marginBottom: '20px' }}>
        <form onSubmit={handleDeploy}>
          <div style={{ marginBottom: '15px' }}>
            <label htmlFor="secret" style={{ display: 'block', marginBottom: '5px' }}>
              <strong>Секретний ключ:</strong>
            </label>
            <input
              id="secret"
              type="password"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              style={{
                width: '100%',
                padding: '10px',
                fontSize: '14px',
                border: '1px solid #ccc',
                borderRadius: '4px',
                boxSizing: 'border-box',
              }}
              placeholder="Введіть DEPLOY_SECRET"
              autoComplete="off"
            />
          </div>

          <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
            <button
              type="submit"
              disabled={loading || !secret.trim()}
              style={{
                padding: '10px 20px',
                fontSize: '16px',
                background: loading || !secret.trim() ? '#aaa' : '#4CAF50',
                color: 'white',
                border: 'none',
                borderRadius: '4px',
                cursor: loading || !secret.trim() ? 'not-allowed' : 'pointer',
              }}
            >
              {loading ? '⏳ Виконується...' : '▶️ Execute git pull'}
            </button>

            <button
              type="button"
              onClick={handleRefreshStatus}
              disabled={statusLoading || !secret.trim()}
              style={{
                padding: '10px 20px',
                fontSize: '16px',
                background: statusLoading || !secret.trim() ? '#aaa' : '#2196F3',
                color: 'white',
                border: 'none',
                borderRadius: '4px',
                cursor: statusLoading || !secret.trim() ? 'not-allowed' : 'pointer',
              }}
            >
              {statusLoading ? '⏳ Status...' : '🔄 Refresh Status'}
            </button>

            <button
              type="button"
              onClick={handleClearSecret}
              style={{
                padding: '10px 20px',
                fontSize: '16px',
                background: '#f44336',
                color: 'white',
                border: 'none',
                borderRadius: '4px',
                cursor: 'pointer',
              }}
            >
              🗑️ Clear Secret
            </button>
          </div>

          <div style={{ marginTop: '15px' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(e) => setAutoRefresh(e.target.checked)}
              />
              <span>Auto-refresh status кожні 10 секунд</span>
            </label>
          </div>
        </form>
      </div>

      <div style={{ background: '#eef5ff', padding: '12px 16px', borderRadius: '8px', marginBottom: '20px', border: '1px solid #9cc5ff' }}>
        <strong>API mode:</strong> <code>{apiBaseLabel}</code>
        <div style={{ fontSize: '12px', marginTop: '6px', color: '#555' }}>
          Правильний режим для VPS: браузер викликає <code>/api/...</code>, а Next.js прокидує це на backend <code>127.0.0.1:8001</code> на сервері.
        </div>
      </div>

      {status && (
        <div
          style={{
            background: status.success ? '#e8f5e9' : '#ffebee',
            padding: '20px',
            borderRadius: '8px',
            marginBottom: '20px',
            border: `2px solid ${status.success ? '#4CAF50' : '#f44336'}`,
          }}
        >
          <h2>📊 Repository Status</h2>
          {status.success ? (
            <>
              <p><strong>Path:</strong> {status.repo_path || '-'}</p>
              <p><strong>Current Commit:</strong> <code>{status.current_commit || '-'}</code></p>
              <p>
                <strong>Local Changes:</strong>{' '}
                {status.has_local_changes ? (
                  <span style={{ color: '#ff9800' }}>⚠️ Yes</span>
                ) : (
                  <span style={{ color: '#4CAF50' }}>✅ No</span>
                )}
              </p>
              <p style={{ fontSize: '12px', color: '#666' }}>Updated: {formatTime(status.timestamp)}</p>
            </>
          ) : (
            <>
              <p style={{ color: '#f44336' }}><strong>Message:</strong> {status.message || 'Status failed'}</p>
              {status.error && <pre style={{ whiteSpace: 'pre-wrap', background: '#fff', padding: '10px' }}>{status.error}</pre>}
            </>
          )}
        </div>
      )}

      {result && (
        <div
          style={{
            background: result.success ? '#e8f5e9' : '#ffebee',
            padding: '20px',
            borderRadius: '8px',
            marginBottom: '20px',
            border: `2px solid ${result.success ? '#4CAF50' : '#f44336'}`,
          }}
        >
          <h2>{result.success ? '✅ Deploy Success' : '❌ Deploy Failed'}</h2>
          <p><strong>Message:</strong> {result.message}</p>

          {result.output && (
            <div style={{ marginTop: '10px' }}>
              <strong>Output:</strong>
              <pre style={{ background: '#f5f5f5', padding: '10px', borderRadius: '4px', overflow: 'auto', fontSize: '12px', whiteSpace: 'pre-wrap' }}>
                {result.output}
              </pre>
            </div>
          )}

          {result.error && (
            <div style={{ marginTop: '10px' }}>
              <strong>Error:</strong>
              <pre style={{ background: '#fff3cd', padding: '10px', borderRadius: '4px', overflow: 'auto', fontSize: '12px', color: '#856404', whiteSpace: 'pre-wrap' }}>
                {result.error}
              </pre>
            </div>
          )}

          <p style={{ fontSize: '12px', color: '#666', marginTop: '10px' }}>Timestamp: {formatTime(result.timestamp)}</p>
        </div>
      )}

      <div style={{ background: '#fff3cd', padding: '20px', borderRadius: '8px', marginTop: '20px', border: '1px solid #ffc107' }}>
        <h3>ℹ️ Інструкція</h3>
        <ol>
          <li>Backend має слухати <code>127.0.0.1:8001</code> або <code>0.0.0.0:8001</code>.</li>
          <li>Next.js має мати rewrite <code>/api/:path*</code> на <code>BACKEND_URL</code>.</li>
          <li>Цей frontend не використовує <code>127.0.0.1</code> у браузері.</li>
          <li>Якщо deploy падає на git passphrase — це вже SSH/deploy-key проблема, не frontend.</li>
        </ol>

        <h4 style={{ marginTop: '15px' }}>API Endpoints from browser:</h4>
        <ul style={{ fontSize: '14px', fontFamily: 'monospace' }}>
          <li>GET: <code>/api/deploy/status?secret=YOUR_KEY</code></li>
          <li>POST: <code>/api/deploy/pull</code> body <code>{'{"secret":"YOUR_KEY"}'}</code></li>
        </ul>
      </div>
    </div>
  );
}
