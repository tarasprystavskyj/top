export default function App({ Component, pageProps }: any) {
  return (
    <>
      <style jsx global>{`
        html,
        body,
        #root,
        #__next {
          width: 100%;
          min-height: 100%;
          margin: 0;
          background: #070b16;
          overflow-x: hidden;
          box-sizing: border-box;
        }

        *,
        *::before,
        *::after {
          box-sizing: border-box;
        }

        body {
          color: #f8fafc;
        }

        button,
        input,
        select,
        textarea {
          font: inherit;
        }

        img,
        table,
        pre {
          max-width: 100%;
        }

        .run-page-content {
          width: 100%;
          max-width: 100%;
          min-width: 0;
          padding: 32px 32px 52px 96px;
          overflow-x: hidden;
        }

        .run-backtest-grid {
          display: grid;
          grid-template-columns: minmax(280px, 360px) minmax(0, 1fr);
          gap: 24px;
          width: 100%;
          max-width: 100%;
          min-width: 0;
          align-items: start;
        }

        .run-backtest-grid > *,
        .run-right-panel,
        .run-card,
        .run-chart-wrapper,
        .run-table-wrapper,
        .run-logs-wrapper {
          min-width: 0;
          max-width: 100%;
        }

        .run-table-wrapper,
        .run-logs-wrapper {
          width: 100%;
          overflow-x: auto;
        }

        @media (max-width: 1024px) {
          .run-page-content {
            padding: 24px 24px 42px 88px;
          }

          .run-backtest-grid {
            grid-template-columns: 1fr;
          }
        }

        @media (max-width: 720px) {
          .run-page-content {
            padding: 20px 16px 36px 80px;
          }
        }
      `}</style>
      <Component {...pageProps} />
    </>
  );
}
