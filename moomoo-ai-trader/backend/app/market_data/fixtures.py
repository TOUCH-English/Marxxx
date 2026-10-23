"""演示数据（DEMO）：全部为虚构证券、确定性随机生成，不代表任何真实证券价格，
不构成投资推荐。由 scripts/generate_demo_data.py 生成并提交到 data/fixtures/。
"""
from __future__ import annotations

import csv
import json
import math
import random
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..core.calendar import TradingCalendar
from ..core.money import round_to_tick

FIXTURE_START = date(2025, 1, 2)
FIXTURE_END = date(2026, 10, 22)
DEMO_REPLAY_START = "2026-06-01T00:30:00+00:00"  # 回放默认起点（之前的数据作预热历史）

# 虚构证券定义：regimes = [(起始日期, 日漂移, 日波动)]
DEMO_SYMBOLS = [
    {
        "symbol": "MY.D001", "market": "MY", "name": "演示种植（虚构）", "sec_type": "STOCK",
        "currency": "MYR", "lot_size": 100, "start": 1.20, "spread_bps": 80, "vol_base": 6_000_000,
        "regimes": [("2025-01-02", 0.0004, 0.012), ("2026-04-01", -0.0015, 0.013), ("2026-07-01", 0.0035, 0.014),
                    ("2026-08-20", -0.003, 0.016), ("2026-09-20", 0.002, 0.012)],
    },
    {
        "symbol": "MY.D002", "market": "MY", "name": "演示银行（虚构）", "sec_type": "STOCK",
        "currency": "MYR", "lot_size": 100, "start": 4.50, "spread_bps": 40, "vol_base": 9_000_000,
        "regimes": [("2025-01-02", 0.0002, 0.008), ("2026-05-15", 0.0018, 0.009), ("2026-08-01", -0.002, 0.011),
                    ("2026-09-15", 0.001, 0.008)],
        "halt_days": ["2026-07-15"],
        "dividends": [("2026-08-12", "0.12")],
    },
    {
        "symbol": "MY.D003", "market": "MY", "name": "演示科技（虚构）", "sec_type": "STOCK",
        "currency": "MYR", "lot_size": 100, "start": 0.85, "spread_bps": 120, "vol_base": 15_000_000,
        "regimes": [("2025-01-02", 0.0, 0.02), ("2026-06-10", 0.004, 0.022), ("2026-07-20", -0.004, 0.025),
                    ("2026-09-01", 0.0015, 0.02)],
    },
    {
        "symbol": "MY.D004", "market": "MY", "name": "演示红利ETF（虚构）", "sec_type": "ETF",
        "currency": "MYR", "lot_size": 100, "start": 1.05, "spread_bps": 60, "vol_base": 800_000,
        "regimes": [("2025-01-02", 0.0002, 0.005)],
    },
    {
        "symbol": "MY.D005", "market": "MY", "name": "演示低流动（虚构）", "sec_type": "STOCK",
        "currency": "MYR", "lot_size": 100, "start": 0.30, "spread_bps": 400, "vol_base": 20_000,
        "regimes": [("2025-01-02", 0.0, 0.03)],
    },
    {
        "symbol": "US.DMOA", "market": "US", "name": "Demo Alpha Corp（虚构）", "sec_type": "STOCK",
        "currency": "USD", "lot_size": 1, "start": 30.0, "spread_bps": 5, "vol_base": 3_000_000,
        # 持续平稳上升：回放期内 MA20 始终高于 MA60，不出现新的均线交叉
        "regimes": [("2025-01-02", 0.0002, 0.011), ("2026-02-01", 0.0028, 0.010)],
    },
    {
        "symbol": "US.DMOB", "market": "US", "name": "Demo Beta Inc（虚构）", "sec_type": "STOCK",
        "currency": "USD", "lot_size": 1, "start": 60.0, "spread_bps": 4, "vol_base": 2_500_000,
        "regimes": [("2025-01-02", 0.0, 0.015), ("2026-05-20", -0.0025, 0.017), ("2026-06-25", 0.004, 0.018),
                    ("2026-08-15", -0.0035, 0.02), ("2026-09-25", 0.0015, 0.015)],
        "splits": [("2026-08-03", "2")],
    },
    {
        "symbol": "US.DMOC", "market": "US", "name": "Demo Gamma ETF（虚构）", "sec_type": "ETF",
        "currency": "USD", "lot_size": 1, "start": 80.0, "spread_bps": 3, "vol_base": 4_000_000,
        "regimes": [("2025-01-02", 0.0003, 0.008), ("2026-06-15", -0.001, 0.009), ("2026-07-25", 0.0015, 0.008)],
    },
    {
        "symbol": "US.DMOL", "market": "US", "name": "Demo 3x Leveraged ETF（虚构）", "sec_type": "ETF",
        "currency": "USD", "lot_size": 1, "start": 25.0, "spread_bps": 10, "vol_base": 5_000_000,
        "is_leveraged": True,
        "regimes": [("2025-01-02", 0.0006, 0.035)],
    },
]


def _regime(sym: dict, d: date):
    cur = sym["regimes"][0]
    for r in sym["regimes"]:
        if date.fromisoformat(r[0]) <= d:
            cur = r
    return cur[1], cur[2]


def _close_before(series, ex_date: date) -> Decimal:
    prev = None
    for row in series:
        if row[0] >= ex_date:
            break
        prev = row[4]
    return Decimal(repr(prev)) if prev else Decimal(1)


def generate(out_dir: Path, seed: int = 20261023) -> None:
    cal = TradingCalendar()
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    actions: list[dict] = []
    for idx, sym in enumerate(DEMO_SYMBOLS):
        rng = random.Random(seed + idx * 7919)
        market = sym["market"]
        days = cal.sessions_between(market, FIXTURE_START, FIXTURE_END)
        price = sym["start"]
        splits = {date.fromisoformat(d): Decimal(r) for d, r in sym.get("splits", [])}
        divs = {date.fromisoformat(d): Decimal(a) for d, a in sym.get("dividends", [])}
        halts = {date.fromisoformat(d) for d in sym.get("halt_days", [])}
        # 先在「复权空间」生成，再把拆股前价格换回原始价格
        series = []
        for d in days:
            drift, vol = _regime(sym, d)
            gap = rng.gauss(0, vol * 0.3)
            o = price * math.exp(gap)
            ret = drift + rng.gauss(0, vol)
            c = o * math.exp(ret - gap * 0.5)
            hi = max(o, c) * math.exp(abs(rng.gauss(0, vol * 0.5)))
            lo = min(o, c) * math.exp(-abs(rng.gauss(0, vol * 0.5)))
            v = int(sym["vol_base"] * math.exp(rng.gauss(0, 0.35)) * (1 + 4 * abs(ret)))
            series.append([d, o, hi, lo, c, v])
            price = c
        # 拆股：拆股日之前的原始价格 = 复权价 × ratio
        for sd, ratio in sorted(splits.items()):
            actions.append({"symbol": sym["symbol"], "ex_date": sd.isoformat(), "kind": "SPLIT", "split_ratio": str(ratio)})
        for d, a in sorted(divs.items()):
            actions.append({"symbol": sym["symbol"], "ex_date": d.isoformat(), "kind": "DIVIDEND", "cash_per_share": str(a)})
        for row in series:
            d = row[0]
            ratio = Decimal(1)
            for sd, r in splits.items():
                if d < sd:
                    ratio *= r
            prices = []
            for p in row[1:5]:
                raw = Decimal(repr(p)) * ratio
                prices.append(raw)
            o, hi, lo, c = prices
            o = round_to_tick(market, o, "down")
            c = round_to_tick(market, c, "down")
            hi = max(round_to_tick(market, hi, "up"), o, c)
            lo = min(round_to_tick(market, lo, "down"), o, c)
            if lo <= 0:
                lo = round_to_tick(market, min(o, c), "down")
            adj = Decimal(1) / ratio
            # 分红前复权：除息日前价格乘以 (1 - 股息/除息前收盘价)
            for dd, amt in divs.items():
                if d < dd:
                    ref = _close_before(series, dd)
                    adj *= Decimal(1) - amt / ref
            rows.append({
                "symbol": sym["symbol"], "date": d.isoformat(), "open": str(o), "high": str(hi), "low": str(lo),
                "close": str(c), "volume": int(row[5] / float(ratio)), "adj_factor": str(adj.normalize()),
                "suspended": 1 if d in halts else 0,
            })
    with (out_dir / "demo_daily.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    instruments = [
        {k: s[k] for k in ("symbol", "market", "name", "sec_type", "currency", "lot_size", "spread_bps")}
        | {"is_demo": True, "is_leveraged": bool(s.get("is_leveraged", False))}
        for s in DEMO_SYMBOLS
    ]
    meta = {
        "disclaimer": "全部为虚构证券的确定性生成数据，不代表任何真实价格，不构成投资推荐。",
        "seed": seed, "start": FIXTURE_START.isoformat(), "end": FIXTURE_END.isoformat(),
        "replay_start": DEMO_REPLAY_START,
        "fx": [{"pair": "USDMYR", "rate": "4.20", "source": "演示固定汇率（非真实）", "as_of": "2026-06-01T00:00:00+00:00"}],
    }
    (out_dir / "demo_instruments.json").write_text(json.dumps(instruments, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "demo_actions.json").write_text(json.dumps(actions, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "demo_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
