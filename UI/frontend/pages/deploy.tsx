import React, { useState, useEffect } from 'react';
import Head from 'next/head';

interface DeployResponse {
  success: boolean;
  message: string;
  output?: string;
  error?: string;
  timestamp: string;
}

interface StatusResponse {
  success: boolean;
  repo_path?: string;
  current_commit?: string;
  has_local_changes?: boolean;
  timestamp: string;
  message?: string;
}

export default function DeployPage() {
  const [secret, setSecret] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<DeployResponse | null>(null);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(false);

  const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

  useEffect(() => {
    // Спроба завантажити секрет з localStorage
    const savedSecret = localStorage.getItem('deploy_secret');
    if (savedSecret) {
      setSecret(savedSecret);
      // Автоматично завантажити статус при наявності секрету
      fetchStatus(savedSecret);
    }
  }, []);

  useEffect(() => {
    let interval: NodeJS.Timeout;
    if (autoRefresh && secret) {
      interval = setInterval(() => {
        fetchStatus(secret);
      }, 10000); // Оновлення кожні 10 секунд
    }
    return () => {
      if (interval) clearInterval(interval);
    };
  }, [autoRefresh, secret]);

  const fetchStatus = async (secretKey: string) => {
    try {
      const response = await fetch(
        `${API_BASE}/api/deploy/status?secret=${encodeURIComponent(secretKey)}`
      );
      const data = await response.json();
      setStatus(data);
    } catch (error) {
      console.error('Error fetching status:', error);
    }
  };

  const handleDeploy = async (e: React.FormEvent) => {
    e.preventDefault();
    
    if (!secret) {
      alert('Введіть секретний ключ');
      return;
    }

    setLoading(true);
    setResult(null);

    try {
      // Зберігаємо секрет
      localStorage.setItem('deploy_secret', secret);

      // Виконуємо deploy через POST
      const response = await fetch(`${API_BASE}/api/deploy/pull`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ secret }),
      });

      const data = await response.json();
      setResult(data);

      // Оновлюємо статус після деплою
      await fetchStatus(secret);
    } catch (error: any) {
      setResult({
        success: false,
        message: `Error: ${error.message}`,
        timestamp: new Date().toISOString(),
      });
    } finally {
      setLoading(false);
    }
  };

  const handleRefreshStatus = () => {
    if (secret) {
      fetchStatus(secret);
    }
  };

  const handleClearSecret = () => {
    setSecret('');
    localStorage.removeItem('deploy_secret');
    setStatus(null);
    setResult(null);
  };

  return (
    <div style={{ padding: '20px', maxWidth: '800px', margin: '0 auto' }}>
      <Head>
        <title>Git Deploy - Admin</title>
      </Head>

      <h1>🚀 Git Deploy Manager</h1>
      
      <div style={{
        background: '#f5f5f5',
        padding: '20px',
        borderRadius: '8px',
        marginBottom: '20px'
      }}>
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
                borderRadius: '4px'
              }}
              placeholder="Введіть секретний ключ для деплою"
            />
          </div>

          <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
            <button
              type="submit"
              disabled={loading || !secret}
              style={{
                padding: '10px 20px',
                fontSize: '16px',
                background: loading ? '#ccc' : '#4CAF50',
                color: 'white',
                border: 'none',
                borderRadius: '4px',
                cursor: loading ? 'not-allowed' : 'pointer'
              }}
            >
              {loading ? '⏳ Виконується...' : '▶️ Execute git pull'}
            </button>

            <button
              type="button"
              onClick={handleRefreshStatus}
              disabled={!secret}
              style={{
                padding: '10px 20px',
                fontSize: '16px',
                background: !secret ? '#ccc' : '#2196F3',
                color: 'white',
                border: 'none',
                borderRadius: '4px',
                cursor: !secret ? 'not-allowed' : 'pointer'
              }}
            >
              🔄 Refresh Status
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
                cursor: 'pointer'
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
              <span>Auto-refresh status (every 10s)</span>
            </label>
          </div>
        </form>
      </div>

      {/* Repository Status */}
      {status && (
        <div style={{
          background: status.success ? '#e8f5e9' : '#ffebee',
          padding: '20px',
          borderRadius: '8px',
          marginBottom: '20px',
          border: `2px solid ${status.success ? '#4CAF50' : '#f44336'}`
        }}>
          <h2>📊 Repository Status</h2>
          {status.success ? (
            <>
              <p><strong>Path:</strong> {status.repo_path}</p>
              <p><strong>Current Commit:</strong> <code>{status.current_commit}</code></p>
              <p>
                <strong>Local Changes:</strong>{' '}
                {status.has_local_changes ? (
                  <span style={{ color: '#ff9800' }}>⚠️ Yes (uncommitted changes)</span>
                ) : (
                  <span style={{ color: '#4CAF50' }}>✅ No (clean)</span>
                )}
              </p>
              <p style={{ fontSize: '12px', color: '#666' }}>
                Updated: {new Date(status.timestamp).toLocaleString('uk-UA')}
              </p>
            </>
          ) : (
            <p style={{ color: '#f44336' }}>{status.message}</p>
          )}
        </div>
      )}

      {/* Deploy Result */}
      {result && (
        <div style={{
          background: result.success ? '#e8f5e9' : '#ffebee',
          padding: '20px',
          borderRadius: '8px',
          border: `2px solid ${result.success ? '#4CAF50' : '#f44336'}`
        }}>
          <h2>{result.success ? '✅ Deploy Success' : '❌ Deploy Failed'}</h2>
          <p><strong>Message:</strong> {result.message}</p>
          
          {result.output && (
            <div style={{ marginTop: '10px' }}>
              <strong>Output:</strong>
              <pre style={{
                background: '#f5f5f5',
                padding: '10px',
                borderRadius: '4px',
                overflow: 'auto',
                fontSize: '12px'
              }}>
                {result.output}
              </pre>
            </div>
          )}
          
          {result.error && (
            <div style={{ marginTop: '10px' }}>
              <strong>Error:</strong>
              <pre style={{
                background: '#fff3cd',
                padding: '10px',
                borderRadius: '4px',
                overflow: 'auto',
                fontSize: '12px',
                color: '#856404'
              }}>
                {result.error}
              </pre>
            </div>
          )}

          <p style={{ fontSize: '12px', color: '#666', marginTop: '10px' }}>
            Timestamp: {new Date(result.timestamp).toLocaleString('uk-UA')}
          </p>
        </div>
      )}

      {/* Help Section */}
      <div style={{
        background: '#fff3cd',
        padding: '20px',
        borderRadius: '8px',
        marginTop: '20px',
        border: '1px solid #ffc107'
      }}>
        <h3>ℹ️ Інструкція</h3>
        <ol>
          <li>Введіть секретний ключ (налаштований в DEPLOY_SECRET)</li>
          <li>Натисніть "Execute git pull" для оновлення коду</li>
          <li>Перевіряйте статус через "Refresh Status"</li>
          <li>Ключ зберігається в localStorage для зручності</li>
        </ol>
        
        <h4 style={{ marginTop: '15px' }}>API Endpoints:</h4>
        <ul style={{ fontSize: '14px', fontFamily: 'monospace' }}>
          <li>GET: <code>{API_BASE}/api/deploy/pull?secret=YOUR_KEY</code></li>
          <li>POST: <code>{API_BASE}/api/deploy/pull</code> (body: {`{secret: "YOUR_KEY"}`})</li>
          <li>GET: <code>{API_BASE}/api/deploy/status?secret=YOUR_KEY</code></li>
        </ul>
      </div>
    </div>
  );
}
