# c_limit14_robust.py
"""
Python-порт TradingView-стратегії
  "C - limit 14 (robust)"

Логіка 1-в-1 з Pine:
- DCA-усереднення по рівнях (true level fills)
- SUB-sell останніх лотів
- FULL TP + трейлінг
- миттєвий рестарт циклу після повного закриття
- дросель сигналів: не більше max_signals_window ордерів
  за window_bars барів (типовий кейс: 14 ордерів / 6 барів на 30s).

Клас розрахований на 1 інструмент.
Використання:

    from c_limit14_robust import CLimit14Robust

    cfg = {
        "first_buy_usdt": 5.0,
        "tp_percent": 1.1,
        "callback_percent": 0.2,
        "margin_call_limit": 244,
        "linear_drop_percent": 0.5,
        "auto_merge": True,
        "sub_sell_tp_percent": 1.3,
        "drop1": 0.3,
        "drop2": 0.4,
        "drop3": 0.6,
        "drop4": 0.8,
        "drop5": 0.8,
        "mult2": 1.5,
        "mult3": 1.0,
        "mult4": 2.0,
        "mult5": 3.5,
        "max_fills_per_bar": 6,
        "max_sub_sells_per_bar": 10,
        "max_signals_window": 14,
        "window_bars": 6,
    }

    s = CLimit14Robust(cfg)

    actions = s.on_bar(ts, open_, high_, low_, close_)
    for a in actions:
        print(a)

Де action – це словник:
    {"type": "BUY", "qty": q, "price": p, "comment": "..."}
    {"type": "SELL_LOT", "qty": q, "price": p, "comment": "..."}
    {"type": "CLOSE_ALL", "price": p, "comment": "...", "lots_closed": n}

Ти можеш адаптувати mapping дій під свій бек-тестер / live-обгортку.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from collections import deque
import math


@dataclass
class Action:
    """
    Узгоджена структура результату on_bar().
    type:
        - "BUY"       : новий DCA-лот
        - "SELL_LOT"  : продаж останнього (LIFO) лота
        - "CLOSE_ALL" : повне закриття всіх лотів
    """
    type: str
    qty: Optional[float] = None
    price: Optional[float] = None
    comment: str = ""
    lots_closed: int = 0  # тільки для CLOSE_ALL


class CLimit14Robust:
    def __init__(self, cfg: Dict[str, Any]):
        cfg = dict(cfg or {})

        # ---- параметри, 1:1 з Pine ----
        self.first_buy_usdt: float = float(cfg.get("first_buy_usdt", 5.0))
        self.tp_percent: float = float(cfg.get("tp_percent", 1.1))
        self.callback_percent: float = float(cfg.get("callback_percent", 0.2))
        self.margin_call_limit: int = int(cfg.get("margin_call_limit", 244))
        self.linear_drop_percent: float = float(cfg.get("linear_drop_percent", 0.5))

        self.auto_merge: bool = bool(cfg.get("auto_merge", True))
        self.sub_sell_tp_percent: float = float(cfg.get("sub_sell_tp_percent", 1.3))

        # нелінійні step-drops
        self.drop1: float = float(cfg.get("drop1", 0.3))
        self.drop2: float = float(cfg.get("drop2", 0.4))
        self.drop3: float = float(cfg.get("drop3", 0.6))
        self.drop4: float = float(cfg.get("drop4", 0.8))
        self.drop5: float = float(cfg.get("drop5", 0.8))

        # множники обʼємів
        self.mult2: float = float(cfg.get("mult2", 1.5))
        self.mult3: float = float(cfg.get("mult3", 1.0))
        self.mult4: float = float(cfg.get("mult4", 2.0))
        self.mult5: float = float(cfg.get("mult5", 3.5))

        self.max_fills_per_bar: int = int(cfg.get("max_fills_per_bar", 6))
        self.max_sub_sells_per_bar: int = int(cfg.get("max_sub_sells_per_bar", 10))

        # throttling TradingView alerts
        self.max_signals_window: int = int(cfg.get("max_signals_window", 14))
        self.window_bars: int = int(cfg.get("window_bars", 6))

        # ---- стан позиції / складу ----
        self.pos_size: float = 0.0
        self.pos_cost_usdt: float = 0.0
        self.avg_price: Optional[float] = None
        self.num_buys: int = 0
        self.last_fill_price: Optional[float] = None
        self.next_level_price: Optional[float] = None

        # LIFO-масиви лотів
        self.lot_ids: List[int] = []
        self.lot_qty: List[float] = []
        self.lot_price: List[float] = []
        self.lot_counter: int = 0

        # трейлінг-TP
        self.trailing_active: bool = False
        self.trailing_max: Optional[float] = None

        # cycle flags
        self.reset_cycle: bool = False
        self.restarted_this_bar: bool = False

        # throttling window (signals per last window_bars)
        self.sig_window: deque[int] = deque([0] * self.window_bars, maxlen=self.window_bars)
        self.last_bar_ts: Any = None

    # ----------------- утиліти Pine-логіки -----------------

    def _get_drop_for_next_level(self, num_buys: int) -> float:
        nb = num_buys + 1
        if nb == 2:
            return self.drop1
        if nb == 3:
            return self.drop2
        if nb == 4:
            return self.drop3
        if nb == 5:
            return self.drop4
        if nb == 6:
            return self.drop5
        return self.linear_drop_percent

    def _get_mult_for_next_level(self, num_buys: int) -> float:
        nb = num_buys + 1
        if nb == 2:
            return self.mult2
        if nb == 3:
            return self.mult3
        if nb == 4:
            return self.mult4
        if nb == 5:
            return self.mult5
        return 1.0

    def _next_level(self, last_fill_price: float, num_buys: int) -> float:
        d = self._get_drop_for_next_level(num_buys)
        return last_fill_price * (1.0 - d / 100.0)

    @staticmethod
    def _recalc_avg(cost: float, size: float) -> Optional[float]:
        return cost / size if size > 0 else None

    # ----------------- throttling -----------------

    def _on_new_bar(self, ts: Any) -> None:
        """Оновлення вікна сигналів при зміні бара."""
        if ts == self.last_bar_ts:
            return
        self.last_bar_ts = ts
        # зсуваємо: додаємо новий бар із 0 сигналів
        self.sig_window.appendleft(0)
        # deque сам обрізає до window_bars
        self.restarted_this_bar = False

    def _total_signals_in_window(self) -> int:
        return sum(self.sig_window)

    def _can_signal(self) -> bool:
        return self._total_signals_in_window() < self.max_signals_window

    def _register_signal(self, count: int = 1) -> None:
        c = max(1, int(count))
        self.sig_window[0] = self.sig_window[0] + c

    # ----------------- службові дії над складом -----------------

    def _open_first_buy(self, price: float, actions: List[Action], comment: str) -> None:
        """Початок нового циклу: перша покупка."""
        buy_usdt = self.first_buy_usdt
        self.lot_counter += 1
        lot_id = self.lot_counter

        qty = buy_usdt / price if price > 0 else 0.0
        actions.append(Action(type="BUY", qty=qty, price=price, comment=comment))

        self.lot_ids.append(lot_id)
        self.lot_qty.append(qty)
        self.lot_price.append(price)

        self.pos_size += qty
        self.pos_cost_usdt += buy_usdt
        self.avg_price = price
        self.num_buys = 1
        self.last_fill_price = price
        self.next_level_price = self._next_level(price, self.num_buys)

    def _close_all(self, close_price: float, actions: List[Action], comment: str) -> None:
        lots = len(self.lot_ids)
        actions.append(
            Action(
                type="CLOSE_ALL",
                qty=self.pos_size if self.pos_size > 0 else None,
                price=close_price,
                comment=comment,
                lots_closed=lots,
            )
        )
        # реєструємо N сигналів, як у strategy.close_all
        self._register_signal(max(lots, 1))

        # позначаємо, що цикл треба перезапустити на цьому ж барі
        self.reset_cycle = True

    # ----------------- основний step -----------------

    def on_bar(self, ts: Any, o: float, h: float, l: float, c: float) -> List[Action]:
        """
        Основний метод: викликається раз на бар.

        Повертає список Action, які треба виконати на цьому барі
        (в бек-тесті – синхронно, у лайві – як ордери на поточній ціні/лімітах).
        """
        actions: List[Action] = []

        # оновлюємо вікно сигналів при зміні бара
        self._on_new_bar(ts)

        # ---- RESET + миттєвий рестарт (Kostya style) ----
        if self.reset_cycle:
            # очистка складу
            self.pos_size = 0.0
            self.pos_cost_usdt = 0.0
            self.avg_price = None
            self.num_buys = 0
            self.last_fill_price = None
            self.next_level_price = None
            self.trailing_active = False
            self.trailing_max = None
            self.lot_ids.clear()
            self.lot_qty.clear()
            self.lot_price.clear()
            self.reset_cycle = False

            # миттєвий Buy_0 на тому ж барі, якщо ще можна сигнал
            if self._can_signal():
                self._open_first_buy(
                    price=c,
                    actions=actions,
                    comment="Restart Buy_0",
                )
                self._register_signal(1)
                self.restarted_this_bar = True

        # ---- Start new cycle if flat ----
        if (
            self.pos_size == 0.0
            and not self.lot_ids
            and not self.reset_cycle
            and not self.restarted_this_bar
        ):
            if self._can_signal():
                self._open_first_buy(
                    price=c,
                    actions=actions,
                    comment="First Buy_0",
                )
                self._register_signal(1)

        # оновимо локальні змінні після можливого старту
        pos_size = self.pos_size
        avg_price = self.avg_price

        # ---- FULL TP (priority) ----
        tp_price = None
        tp_hit = False
        if avg_price is not None and avg_price > 0:
            tp_price = avg_price * (1.0 + self.tp_percent / 100.0)
            tp_hit = c >= tp_price

        if tp_hit and pos_size > 0:
            if self.callback_percent > 0:
                self.trailing_active = True
                self.trailing_max = c if self.trailing_max is None else max(
                    self.trailing_max, c
                )
                trail_stop = self.trailing_max * (1.0 - self.callback_percent / 100.0)

                if c <= trail_stop and self._can_signal():
                    self._close_all(
                        close_price=c,
                        actions=actions,
                        comment="TP Full (Trailing)",
                    )
            else:
                if self._can_signal():
                    self._close_all(
                        close_price=c,
                        actions=actions,
                        comment="TP Full",
                    )

        # якщо цикл позначений на reset – не робимо більше нічого на цьому барі
        if self.reset_cycle:
            return actions

        # ---- DCA buys (TRUE LEVEL FILLS) ----
        if (not tp_hit) and pos_size > 0 and not self.restarted_this_bar:
            can_buy_more = self.num_buys < self.margin_call_limit
            fills = 0

            while (
                can_buy_more
                and fills < self.max_fills_per_bar
                and self.next_level_price is not None
                and l <= self.next_level_price
                and self._can_signal()
            ):
                mult = self._get_mult_for_next_level(self.num_buys)
                buy_usdt = self.first_buy_usdt * mult

                self.lot_counter += 1
                lot_id = self.lot_counter

                fill_price = self.next_level_price
                qty = buy_usdt / fill_price if fill_price > 0 else 0.0

                actions.append(
                    Action(
                        type="BUY",
                        qty=qty,
                        price=fill_price,
                        comment="DCA Buy",
                    )
                )
                self._register_signal(1)

                # оновлення складу
                self.lot_ids.append(lot_id)
                self.lot_qty.append(qty)
                self.lot_price.append(fill_price)

                self.pos_size += qty
                self.pos_cost_usdt += buy_usdt
                self.avg_price = self._recalc_avg(self.pos_cost_usdt, self.pos_size)

                self.num_buys += 1
                self.last_fill_price = fill_price
                self.next_level_price = self._next_level(self.last_fill_price, self.num_buys)

                self.trailing_active = False
                self.trailing_max = None

                fills += 1
                can_buy_more = self.num_buys < self.margin_call_limit

        # ---- SUB-SELL (LIFO, каскадом) ----
        if (not tp_hit) and self.pos_size > 0 and self.num_buys > 5 and not self.restarted_this_bar:
            sold = 0
            any_sold = False

            while sold < self.max_sub_sells_per_bar and self._can_signal():
                last_idx = len(self.lot_ids) - 1
                if last_idx < 0:
                    break

                entry_price = self.lot_price[last_idx]
                qty_last = self.lot_qty[last_idx]

                last_lot_tp = entry_price * (1.0 + self.sub_sell_tp_percent / 100.0)

                if c >= last_lot_tp:
                    any_sold = True

                    actions.append(
                        Action(
                            type="SELL_LOT",
                            qty=qty_last,
                            price=c,
                            comment="Sub-sell last lot",
                        )
                    )
                    self._register_signal(1)

                    entry_cost_usdt = qty_last * entry_price
                    profit_usdt = qty_last * (c - entry_price)

                    self.pos_size -= qty_last
                    self.pos_cost_usdt -= entry_cost_usdt
                    if self.auto_merge:
                        self.pos_cost_usdt -= profit_usdt

                    self.avg_price = self._recalc_avg(self.pos_cost_usdt, self.pos_size)

                    # видаляємо LIFO-лот
                    self.lot_ids.pop()
                    self.lot_qty.pop()
                    self.lot_price.pop()

                    self.num_buys = max(self.num_buys - 1, 0)

                    sold += 1

                    if not self.lot_ids or self.pos_size <= 0:
                        self.reset_cycle = True
                        break
                else:
                    break

            if any_sold and not self.reset_cycle and self.lot_price:
                self.last_fill_price = self.lot_price[-1]
                self.next_level_price = self._next_level(
                    self.last_fill_price, self.num_buys
                )

        return actions
