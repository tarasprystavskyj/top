# HTX friend 200 USD server handoff

Created: 2026-06-03  
Agent: `l_HuygensHTXTradePrep1`

## Safety

- Do not print `.env` values.
- Expected server `.env` key aliases observed by name only: `HTX_MAKSYM_KEY`, `HTX_MAKSYM_SECRET`.
- HTX exchange and env key names are declared in the config files.
- `obw_platform/runners/common.py` is not changed. The HYPE live runner maps `HTX_MAKSYM_KEY`/`HTX_MAKSYM_SECRET` to process-local `HTX_KEY`/`HTX_SECRET` before ccxt init without printing values.
- `binance_online_copytrading` HTX scripts are paper/shadow only and never call `create_order`.

## Allocation

- Veronika / HYPE follower `4300516091842181632`: max notional `110 USDT` (55%).
- Callme follower `4512404768792222208`: max notional `90 USDT` (45%), multi-symbol. Do not force AMD-only.
- V21 `base_order_pct_eq=5.0`, so initial/base order targets scale to `5.5 USDT` and `4.5 USDT`; DCA sizing scales from delegated capital through the same V21 path.

## Local validation command

```bash
python obw_platform/meta_strategies/binance_online_copytrading/binance_online_copytrading.py \
  --config obw_platform/meta_strategies/binance_online_copytrading/configs/htx_friend_200usd_55_45.json \
  --paper-exchange htx \
  --state-path obw_platform/meta_strategies/binance_online_copytrading/reports/htx_friend_200usd_shadow/state.json \
  --session-db obw_platform/meta_strategies/binance_online_copytrading/reports/htx_friend_200usd_shadow/session.sqlite \
  --shadow-orders-path obw_platform/meta_strategies/binance_online_copytrading/reports/htx_friend_200usd_shadow/shadow_orders.jsonl \
  --run-id FRIEND_200USD_HTX_SHADOW_ONCE \
  --once
```

## Server pull

Git root is `/var/www/vps2.happyuser.info/top/top_1`; the `.env` file is under `obw_platform/.env`.

```bash
cd /var/www/vps2.happyuser.info/top/top_1
git fetch origin
git checkout veronika
git pull --ff-only origin veronika
```

If the main server worktree is dirty, do not reset it. Use a separate worktree:

```bash
cd /var/www/vps2.happyuser.info/top/top_1
git fetch origin
git worktree add /var/www/vps2.happyuser.info/top/top_1_htx_friend_live origin/veronika
cd /var/www/vps2.happyuser.info/top/top_1_htx_friend_live
```

## Server start commands

Veronika/HYPE live canary, only after checking config and accepting live orders:

```bash
cd /var/www/vps2.happyuser.info/top/top_1
export VERONIKA_HYPE_HTX_LIVE_ACK=I_ACCEPT_REAL_HTX_ORDERS
bash obw_platform/meta_strategies/telegram_signal_dca/run_veronika_hype_htx_live.sh
```

Live config:

```bash
obw_platform/meta_strategies/telegram_signal_dca/configs/htx_veronika_hype_live_110.json
```

Callme multi-symbol HTX must run shadow first. The current live canary is per-symbol, so do not start an AMD-only live process after the owner clarified that Callme is multi-symbol.

```bash
cd /var/www/vps2.happyuser.info/top/top_1
bash obw_platform/meta_strategies/binance_online_copytrading/run_callme_multi_htx_shadow.sh
```

If Callme live is required, first implement a multi-symbol live adapter or explicit per-symbol allowlist based on current Callme open positions and HTX symbol availability.
