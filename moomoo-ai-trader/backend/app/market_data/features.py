"""技术特征（只用于分析展示与决策输入，不参与资金累计）。

全部基于已收盘日 K（复权价用于指标）与当前报价计算。
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from .base import Bar, Quote


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def atr(bars: list[Bar], n: int = 14) -> float | None:
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-n - 1:-1], bars[-n:]):
        f = float(cur.adj_factor)
        pf = float(prev.adj_factor)
        h, l, pc = float(cur.high) * f, float(cur.low) * f, float(prev.close) * pf
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n


def compute_features(bars: list[Bar], quote: Quote | None) -> dict[str, Any]:
    closes = [float(b.adj_close) for b in bars if not b.suspended]
    out: dict[str, Any] = {"bars_available": len(bars)}
    if not closes:
        return out
    last_close = closes[-1]

    def ret(n: int):
        return round(closes[-1] / closes[-1 - n] - 1, 4) if len(closes) > n else None

    out["ret_5d"] = ret(5)
    out["ret_20d"] = ret(20)
    out["ret_60d"] = ret(60)
    if len(closes) >= 21:
        lr = [math.log(b / a) for a, b in zip(closes[-21:-1], closes[-20:])]
        mu = sum(lr) / len(lr)
        out["vol_20d"] = round(math.sqrt(sum((x - mu) ** 2 for x in lr) / (len(lr) - 1)), 4)
    for n in (10, 20, 60):
        v = sma(closes, n)
        out[f"sma_{n}"] = round(v, 4) if v else None
    a = atr(bars, 14)
    # ATR 换算回原始价格空间（以最近一根 K 线的复权因子）
    if a is not None and bars:
        out["atr_14"] = round(a / float(bars[-1].adj_factor), 4)
        out["atr_pct"] = round(a / last_close, 4)
    if len(bars) >= 20:
        out["avg_turnover_20d"] = round(sum(float(b.close) * b.volume for b in bars[-20:]) / 20, 0)
    hi = max(closes[-250:])
    out["dist_from_252d_high"] = round(last_close / hi - 1, 4)
    if quote and quote.last is not None:
        out["last"] = str(quote.last)
        if quote.prev_close:
            out["chg_today"] = round(float(quote.last / quote.prev_close) - 1, 4)
        if quote.bid is not None and quote.ask is not None and quote.bid > 0:
            mid = (quote.bid + quote.ask) / 2
            out["spread_bps"] = int(((quote.ask - quote.bid) / mid * 10000).to_integral_value())
    return out


def momentum_score(f: dict[str, Any]) -> float:
    """确定性的候选排序分（不是盈利预测），用于在调用 AI 前缩小候选范围。"""
    r20 = f.get("ret_20d") or 0.0
    r60 = f.get("ret_60d") or 0.0
    vol = f.get("vol_20d")
    if vol is None:
        vol = 0.02
    return (0.6 * r20 + 0.4 * r60) / max(vol, 0.005)


def dec(x: Any) -> Decimal | None:
    if x is None:
        return None
    return Decimal(str(x))
