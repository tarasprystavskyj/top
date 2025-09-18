# pip install ccxt==4.* (або актуальну)
import time
import ccxt
from datetime import datetime, timezone

API_KEY = "1zpDoyS5MzGJzcgXeKMx8qp974MKXio4CRXLaYPETssQVah7Zfk9bFLDUtLVhBpFXZkcwbEaFQcBpS9nA"
API_SECRET = "amCFzf7hgaI8WQmPCVcciwB2yhsYKCdUyR7D5GUWoG2hzIFxfauHora1ADtsl6wjfjCtudvnRujeAcsWJw"

# Приклад: HIFI перп на BingX у форматі CCXT:
SYMBOL = "HIFI/USDT:USDT"     # USDT-M perp
SIDE_OPEN = "buy"            # приклад для SHORT; для LONG → "buy"
NOTIONAL_USDT = 4.5            # скільки USDT витратити на вхід (підбери під мін.ноціонал)
LEVERAGE = 1
POSITION_MODE = "oneway"      # BingX USDT-M: "oneway" (BOTH) або "hedge"

# SL як умова: якщо ціна піде проти нас → спрацює stop_market
# Для SHORT: SL вище за entry; для LONG: нижче за entry.
SL_TRIGGER = 0.204           # твій тригер-сл (наприклад, з розрахунку BE або ATR)
USE_MARK_PRICE = True         # BingX дозволяє Last/Mark як базу тригера (Index знімають)

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def main():
    ex = ccxt.bingx({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "options": {
            "defaultType": "swap",   # USDT-M futures у CCXT
        },
        "enableRateLimit": True,
    })

    # 1) Переконайся, що тип позиції та плече встановлені (якщо API дозволяє)
    try:
        # режим позиції
        # bingx: 'oneway' → positionSide='BOTH'; 'hedge' → LONG/SHORT роздільно.
        ex.set_position_mode(POSITION_MODE == "hedge")
    except Exception as e:
        print("[warn] set_position_mode not supported:", e)

    try:
        # встановити плече для SYMBOL
        ex.set_leverage(LEVERAGE, SYMBOL, params={"side": "BOTH"})
    except Exception as e:
        print("[warn] set_leverage not supported:", e)

    # 2) Оцінимо поточну ціну й порахуємо кількість з ноціоналу
    ticker = ex.fetch_ticker(SYMBOL)
    last = float(ticker["last"])
    # BingX вимагає кроки/мін-кількість — беремо з markets
    ex.load_markets()
    mkt = ex.markets[SYMBOL]
    amt_step = mkt.get("precision", {}).get("amount", None)
    min_qty = mkt.get("limits", {}).get("amount", {}).get("min", 0) or 0
    min_notional = mkt.get("limits", {}).get("cost", {}).get("min", 0) or 0

    qty = NOTIONAL_USDT / last
    if amt_step:
        qty = ex.amount_to_precision(SYMBOL, qty)
    if min_qty and float(qty) < float(min_qty):
        raise ValueError(f"qty {qty} < min_qty {min_qty}")
    if min_notional and last * float(qty) < float(min_notional):
        raise ValueError(f"notional {last*float(qty):.6g} < min_notional {min_notional}")

    print(f"[{utcnow()}] Opening {SIDE_OPEN.upper()} {qty} {SYMBOL} @~{last}")

    # 3) Відкриваємо MARKET ордер
    # BingX в one-way використовує positionSide='BOTH'; reduceOnly=False для відкриття
    order = ex.create_order(
        SYMBOL,
        "market",
        SIDE_OPEN,
        qty,
        None,
        {"reduceOnly": False, "positionSide": "BOTH"}
    )
    print("open_order:", order.get("id"), order.get("info"))

    # Дамо біржі завершити філл
    time.sleep(0.7)
    # after you fetched ticker/position and know entry
    pos = ex.fetch_positions([SYMBOL])[0]
    entry = float(pos["entryPrice"]) or last

    side_is_long = (SIDE_OPEN == "buy")
    # example: stop 1.5% away
    sl_trigger = entry * (1 - 0.015) if side_is_long else entry * (1 + 0.015)

    # 1) leverage with side
    try:
        ex.set_leverage(LEVERAGE, SYMBOL, params={"side": "BOTH"})
    except Exception as e:
        print("[warn] set_leverage:", e)

    # 2) conditional stop-market (reduceOnly) on the correct side
    params = {
        "reduceOnly": True,
        "positionSide": "BOTH",
        "triggerPrice": float(sl_trigger),    # <--- for LONG below entry, for SHORT above
        "workingType": "MARK_PRICE",          # optional; default is last price
    }
    side_close = "sell" if side_is_long else "buy"

    try:
        sl_order = ex.create_order(SYMBOL, "stop_market", side_close, qty, None, params)
        print("sl_order:", sl_order.get("id"), sl_order.get("info"))
    except ccxt.ExchangeError as e:
        print("[info] retrying with stopPrice:", e)
        # some BingX routes want 'stopPrice' key
        params_alt = dict(params)
        params_alt.pop("triggerPrice", None)
        params_alt["stopPrice"] = float(sl_trigger)
        sl_order = ex.create_order(SYMBOL, "stop_market", side_close, qty, None, params_alt)
        print("sl_order:", sl_order.get("id"), sl_order.get("info"))
    # 5) Перевіряємо відкриті умовні ордери
    time.sleep(0.5)
    oo = ex.fetch_open_orders(SYMBOL)
    print("open_orders:", oo)

if __name__ == "__main__":
    main()
