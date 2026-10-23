"""moomoo 行情（只读）。

- 只有 API 实际返回的报价才显示为实时；没有买卖价时不伪造盘口。
- 报价带源时间戳（券商 update_time，按交易所时区解析）与接收时间；风控据此判断是否过期。
- 未连接或无权限时 connected=False，所有开仓被健康检查阻止。
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from ..config import get_settings
from ..core.calendar import MARKET_TZ
from ..db.types import D
from .base import Bar, CorporateAction, InstrumentInfo, ProviderStatus, Quote


class MoomooQuoteProvider:
    name = "MOOMOO_REALTIME"
    is_realtime = True
    _shared = None

    def __init__(self, ctx_factory=None):
        self._ctx = None
        self._factory = ctx_factory
        self._lock = threading.Lock()
        self._instruments: dict[str, InstrumentInfo] = {}
        self._cache: dict[str, tuple[float, Quote]] = {}
        self._bars: dict[str, tuple[float, list[Bar]]] = {}
        self._err = ""

    @classmethod
    def shared(cls) -> "MoomooQuoteProvider":
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    def _context(self):
        with self._lock:
            if self._ctx is None:
                if self._factory:
                    self._ctx = self._factory()
                else:
                    from ..broker.moomoo_adapter import tcp_probe
                    import moomoo as m

                    st = get_settings()
                    if not tcp_probe(st.moomoo_opend_host, st.moomoo_opend_port):
                        raise ConnectionError("OpenD 未运行")
                    self._ctx = m.OpenQuoteContext(host=st.moomoo_opend_host, port=st.moomoo_opend_port)
            return self._ctx

    def status(self) -> ProviderStatus:
        try:
            import moomoo as m

            ret, gs = self._context().get_global_state()
            ok = ret == m.RET_OK and str(gs.get("qot_logined")) in ("1", "True")
            return ProviderStatus(self.name, ok, True, "moomoo OpenD 行情" if ok else "moomoo 行情未登录",
                                  detail="" if ok else str(gs))
        except Exception as e:
            return ProviderStatus(self.name, False, True, "尚未连接", detail=str(e))

    def register(self, inst: InstrumentInfo) -> None:
        self._instruments[inst.symbol] = inst

    def instruments(self) -> list[InstrumentInfo]:
        return list(self._instruments.values())

    def instrument(self, symbol: str) -> InstrumentInfo | None:
        return self._instruments.get(symbol)

    def quote(self, symbol: str, now: datetime) -> Quote | None:
        hit = self._cache.get(symbol)
        if hit and time.monotonic() - hit[0] < 2:
            return hit[1]
        import moomoo as m

        ret, df = self._context().get_market_snapshot([symbol])
        recv = datetime.now(timezone.utc)
        if ret != m.RET_OK or not len(df):
            self._err = str(df)
            return None
        r = df.to_dict("records")[0]
        mk = symbol.split(".")[0]
        try:
            src = datetime.fromisoformat(str(r["update_time"])).replace(tzinfo=MARKET_TZ[mk]).astimezone(timezone.utc)
        except Exception:
            src = None

        def px(k):
            v = r.get(k)
            try:
                d = D(v)
                return d if d.is_finite() and d > 0 else None
            except Exception:
                return None

        q = Quote(symbol=symbol, last=px("last_price"), bid=px("bid_price"), ask=px("ask_price"),
                  volume=int(float(r.get("volume") or 0)), prev_close=px("prev_close_price"), source=self.name,
                  is_realtime=True, source_ts=src, recv_ts=recv, suspended=bool(r.get("suspension")),
                  day_high=px("high_price"), day_low=px("low_price"), day_open=px("open_price"),
                  note="moomoo 快照（API 权限可能与 App 不同）")
        if symbol not in self._instruments and r.get("lot_size"):
            self._instruments[symbol] = InstrumentInfo(symbol=symbol, market=mk, name=str(r.get("name", symbol)),
                                                       sec_type="STOCK", currency={"MY": "MYR", "US": "USD"}.get(mk, ""),
                                                       lot_size=int(r["lot_size"]))
        self._cache[symbol] = (time.monotonic(), q)
        return q

    def daily_bars(self, symbol: str, now: datetime, n: int) -> list[Bar]:
        hit = self._bars.get(symbol)
        if not hit or time.monotonic() - hit[0] > 600:
            import moomoo as m

            start = (now - timedelta(days=int(n * 1.6) + 10)).date().isoformat()
            ctx = self._context()
            ret, df, _ = ctx.request_history_kline(symbol, start=start, end=now.date().isoformat(),
                                                   ktype=m.KLType.K_DAY, autype=m.AuType.NONE, max_count=1000)
            if ret != m.RET_OK:
                return []
            # 前复权收盘价用于计算复权因子（指标用复权价，成交与持仓用原始价）
            ret2, adf, _ = ctx.request_history_kline(symbol, start=start, end=now.date().isoformat(),
                                                     ktype=m.KLType.K_DAY, autype=m.AuType.QFQ, max_count=1000)
            adj = {str(r["time_key"])[:10]: D(r["close"]) for r in adf.to_dict("records")} if ret2 == m.RET_OK else {}
            bars = []
            for r in df.to_dict("records"):
                k = str(r["time_key"])[:10]
                close = D(r["close"])
                fac = (adj[k] / close) if k in adj and close > 0 else Decimal(1)
                bars.append(Bar(symbol=symbol, day=date.fromisoformat(k), open=D(r["open"]), high=D(r["high"]),
                                low=D(r["low"]), close=close, volume=int(r["volume"]), adj_factor=fac))
            self._bars[symbol] = (time.monotonic(), bars)
        from ..core.calendar import get_calendar

        mk = symbol.split(".")[0]
        last_closed = get_calendar().last_closed_session(mk, now)
        bars = [b for b in self._bars[symbol][1] if last_closed is not None and b.day <= last_closed]
        return bars[-n:]

    def corporate_actions(self, symbol: str, start: date, end: date) -> list[CorporateAction]:
        return []
