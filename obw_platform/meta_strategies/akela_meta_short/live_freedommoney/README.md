# FREEDOMMONEY Paper/Live Candidate

Status: prepared, not started.

Primary config:

```text
obw_platform/meta_strategies/akela_meta_short/live_freedommoney/V21_freedommoney_bingx_live_min2p2.yaml
```

Universe:

```text
obw_platform/universe/universe_freedommoney_live.txt
```

The config starts from `obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml`
and keeps the ENA live safety style:

- `minOrderUSDT` copied from ENA live baseline, then raised from `2.0` to `2.2`
  after BingX public market metadata showed:
  - `limits.amount.min = 244`
  - `limits.cost.min = 2.0`
  - current notional at min qty was about `2.0018 USDT`
- limit-maker entry settings remain enabled.
- no exchange, fee, slippage, liquidation, margin, or backtest math was changed.

## Start Command

Do not run this without explicit human confirmation:

```bash
cd /var/www/vps2.happyuser.info/top/top_1/obw_platform
python3 bt_live_paper_runner_separated_universe_4.py \
  --mode live \
  --env-file .env \
  --cfg meta_strategies/akela_meta_short/live_freedommoney/V21_freedommoney_bingx_live_min2p2.yaml \
  --exchange bingx \
  --symbol-format usdtm \
  --poll-sec 2 \
  --bar-delay-sec 1 \
  --limit_klines 300 \
  --prewarm-bars 300 \
  --results-dir _reports/_live/bingx_freedommoney_v21_min2p2 \
  --session-db session.sqlite \
  --cache-out combined_cache_session.db \
  --hour-cache save \
  --universe-file universe/universe_freedommoney_live.txt
```

## Monitor Agent

Agent name: `Lyra`.

`Lyra` is a read/report monitor loop. It is prepared but not started.
It checks the FREEDOMMONEY live result directory every 30 minutes and writes
compact status files under this folder.

Start only after the live session exists:

```bash
tmux new-session -d -s lyra_freedommoney_live_monitor \
  -c /var/www/vps2.happyuser.info/top/top_1 \
  './obw_platform/meta_strategies/akela_meta_short/live_freedommoney/run_lyra_monitor_loop.sh'
```
