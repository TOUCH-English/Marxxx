"""测试工具：可控的时钟、行情、伪券商与运行设置。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.core.calendar import get_calendar
from app.core.enums import Mode
from app.db.models import AccountBinding, Portfolio, RiskPolicyVersion, StrategyVersion, UniverseRule
from app.db.session import write_session
from app.market_data.base import Bar, InstrumentInfo, ProviderStatus, Quote
from app.risk.policy import SEED_RISK_PARAMS
from app.services.runs import create_authorization, ensure_seed
from app.simulation.fake_broker import FakeBroker

# 2026-10-21（周三）美股常规时段内：UTC 15:00 = 纽约 11:00（夏令时）
US_OPEN = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
# 2026-10-21 马股上午时段：UTC 02:00 = 吉隆坡 10:00
MY_OPEN = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)


class ManualClock:
    is_replay = False

    def __init__(self, t: datetime):
        self.t = t

    def now(self) -> datetime:
        return self.t

    def advance(self, **kw) -> datetime:
        self.t = self.t + timedelta(**kw)
        return self.t


@dataclass
class _Q:
    last: Decimal
    bid: Decimal | None
    ask: Decimal | None
    suspended: bool = False
    age_s: int = 0
    realtime: bool = True


class StaticProvider:
    """可控行情：每个证券给定一串已收盘日收盘价 + 当前报价。"""

    name = "TEST_FEED"
    is_realtime = True

    def __init__(self):
        self.insts: dict[str, InstrumentInfo] = {}
        self.closes: dict[str, list[Decimal]] = {}
        self.q: dict[str, _Q] = {}
        self.connected = True
        self.meta = {}

    def add(self, symbol: str, closes: list, last, *, market: str = "US", lot: int = 1, spread: str = "0.01",
            name: str = "测试证券", sec_type: str = "STOCK", currency: str | None = None, leveraged: bool = False):
        ccy = currency or ("USD" if market == "US" else "MYR")
        self.insts[symbol] = InstrumentInfo(symbol, market, name, sec_type, ccy, lot, is_leveraged=leveraged)
        self.closes[symbol] = [Decimal(str(c)) for c in closes]
        self.set_quote(symbol, last, spread=spread)

    def set_quote(self, symbol, last, *, spread="0.01", suspended=False, age_s=0, realtime=True, bid=None, ask=None):
        last = Decimal(str(last))
        sp = Decimal(spread)
        self.q[symbol] = _Q(last, Decimal(str(bid)) if bid else last - sp, Decimal(str(ask)) if ask else last + sp,
                            suspended, age_s, realtime)

    def status(self):
        return ProviderStatus(self.name, self.connected, True, "测试行情", "" if self.connected else "断开")

    def instruments(self):
        return list(self.insts.values())

    def instrument(self, symbol):
        return self.insts.get(symbol)

    def quote(self, symbol, now):
        q = self.q.get(symbol)
        if q is None:
            return None
        return Quote(symbol=symbol, last=q.last, bid=None if q.suspended else q.bid, ask=None if q.suspended else q.ask,
                     volume=1_000_000, prev_close=self.closes[symbol][-1] if self.closes[symbol] else None,
                     source=self.name, is_realtime=q.realtime, source_ts=now - timedelta(seconds=q.age_s), recv_ts=now,
                     suspended=q.suspended)

    def daily_bars(self, symbol, now, n):
        inst = self.insts.get(symbol)
        if inst is None:
            return []
        cal = get_calendar()
        last = cal.last_closed_session(inst.market, now)
        days = []
        d = last
        while len(days) < len(self.closes[symbol]):
            if cal.is_session_day(inst.market, d):
                days.append(d)
            d -= timedelta(days=1)
        days.reverse()
        bars = [Bar(symbol, dd, c, c * Decimal("1.01"), c * Decimal("0.99"), c, 1_000_000)
                for dd, c in zip(days, self.closes[symbol])]
        return bars[-n:]

    def corporate_actions(self, symbol, start, end):
        return []


def trend(n: int, start: float, step: float) -> list[float]:
    return [round(start + i * step, 2) for i in range(n)]


def setup_run(ns: str, mode: str, *, symbols: list[str], capital: dict | None = None, max_loss: dict | None = None,
              strategy_key: str = "AI_AUTONOMOUS", risk: dict | None = None, as_of: datetime = US_OPEN,
              overnight: bool = True, valid_hours: float = 24) -> tuple[int, int]:
    ensure_seed(ns)
    capital = capital or {"USD": "10000"}
    max_loss = max_loss or {k: str(Decimal(v) / 10) for k, v in capital.items()}
    with write_session(ns) as s:
        if ns == "live":
            b = AccountBinding(broker="MOOMOO", trd_env="REAL", acc_id="12345678", acc_id_masked="****5678",
                               markets=["US", "MY"], confirmed_by_user=True)
        else:
            b = s.execute(select(AccountBinding).where(AccountBinding.broker == "LOCAL_SIM")).scalar_one()
        s.add(b)
        s.flush()
        params = dict(SEED_RISK_PARAMS) | (risk or {})
        rp = RiskPolicyVersion(version=100 + (s.execute(select(RiskPolicyVersion)).scalars().all().__len__()),
                               params=params, confirmed_by_user=True, confirmed_at=as_of, is_test_seed=False)
        s.add(rp)
        p = Portfolio(mode=mode, account_binding_id=b.id, name="test", data_source="MOOMOO_REALTIME", running=True)
        s.add(p)
        s.flush()
        sv = s.execute(select(StrategyVersion).where(StrategyVersion.key == strategy_key,
                                                     StrategyVersion.status == "ACTIVE")).scalar_one()
        a = create_authorization(s, portfolio=p, capital=capital, max_planned_loss=max_loss,
                                 scope={"type": "WHITELIST", "symbols": symbols}, strategy_version_id=sv.id,
                                 risk_policy_version_id=rp.id, overnight=overnight, valid_hours=valid_hours,
                                 exit_rules=None, actor="test", as_of=as_of)
        return p.id, a.id


def make_engine(ns: str, provider, clock, adapter=None, ai=None):
    from app.ai.providers import FixtureProvider
    from app.workers.engine import TradingEngine

    adapter = adapter or FakeBroker(trd_env="REAL" if ns == "live" else "LOCAL")
    ov = {"provider": lambda pid, p: provider, "clock": lambda pid, p: clock, "adapter": lambda pid, p: adapter,
          "ai_provider": (lambda: ai) if ai is not None else (lambda: FixtureProvider())}
    return TradingEngine(ns, overrides=ov), adapter
