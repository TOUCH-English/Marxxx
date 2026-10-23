"""moomoo 行情（只读）。

- 只有 API 实际返回的报价才显示为实时；没有买卖价时不伪造盘口。
- 报价带源时间戳（券商 update_time，按交易所时区解析）与接收时间；风控据此判断是否过期。
- 未连接或无权限时 connected=False，所有开仓被健康检查阻止。
- 快照请求有频率限额：同一进程把近期用到的证券合并成一次请求，缓存数秒，并自我限速。
- 证券类型以 get_stock_basicinfo 为准；查不到类型的证券不登记，策略不会选它。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from ..config import get_settings
from ..core.calendar import MARKET_TZ
from ..db.types import D
from .base import Bar, CorporateAction, InstrumentInfo, ProviderStatus, Quote


QUOTE_TTL_S = 4.0  # 报价缓存时间
STALE_FALLBACK_S = 15.0  # 请求失败或限速时，可退回使用的缓存最长年龄（开仓另有源时间戳过期检查）
SNAPSHOT_RATE = (45, 30.0)  # 自我限速：每 30 秒最多 45 次快照请求（官方限额需核对，此处留余量）
WANTED_TTL_S = 120.0  # 近期被请求过的证券才参与合并请求
RETRY_S = 60.0  # 从未成功的证券、类型未核实的证券，最多每 60 秒单独重试一次
BATCH_MAX = 200
# get_stock_basicinfo 的 stock_type → 本系统证券类型。ETF 无法从接口判断是否杠杆/反向，
# 在用户确认流程实现前一律记为 ETF_UNVERIFIED（风控与股票池都不允许）。
SEC_TYPES = {"STOCK": "STOCK", "ETF": "ETF_UNVERIFIED"}


def _px(r: dict, k: str) -> Decimal | None:
    try:
        d = D(r.get(k))
        return d if d.is_finite() and d > 0 else None
    except Exception:
        return None


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
        self._wanted: dict[str, float] = {}  # 证券 → 最近一次被请求的时间
        self._good: set[str] = set()  # 至少成功取得过一次报价的证券
        self._last_try: dict[str, float] = {}  # 单独重试的时间
        self._type_try: dict[str, float] = {}
        self._calls: deque[float] = deque()
        self.snapshot_requests = 0

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

    # ---------------------------------------------------------------- 报价
    def _allow(self, t: float) -> bool:
        n, window = SNAPSHOT_RATE
        while self._calls and t - self._calls[0] > window:
            self._calls.popleft()
        if len(self._calls) >= n:
            self._err = "快照请求达到本机限速，暂用缓存"
            return False
        self._calls.append(t)
        return True

    def _fetch(self, symbols: list[str], t: float) -> bool:
        """一次快照请求取多只证券；成功的写入缓存。返回请求本身是否成功。"""
        import moomoo as m

        if not symbols or not self._allow(t):
            return False
        self.snapshot_requests += 1
        ret, df = self._context().get_market_snapshot(symbols)
        recv = datetime.now(timezone.utc)
        if ret != m.RET_OK:
            self._err = str(df)
            return False
        for r in df.to_dict("records"):
            sym = str(r.get("code"))
            if sym in symbols:
                self._cache[sym] = (t, self._to_quote(sym, r, recv))
                self._good.add(sym)
        return True

    def _to_quote(self, symbol: str, r: dict, recv: datetime) -> Quote:
        mk = symbol.split(".")[0]
        try:
            src = datetime.fromisoformat(str(r["update_time"])).replace(tzinfo=MARKET_TZ[mk]).astimezone(timezone.utc)
        except Exception:
            src = None
        return Quote(symbol=symbol, last=_px(r, "last_price"), bid=_px(r, "bid_price"), ask=_px(r, "ask_price"),
                     volume=int(float(r.get("volume") or 0)), prev_close=_px(r, "prev_close_price"), source=self.name,
                     is_realtime=True, source_ts=src, recv_ts=recv, suspended=bool(r.get("suspension")),
                     day_high=_px(r, "high_price"), day_low=_px(r, "low_price"), day_open=_px(r, "open_price"),
                     note="moomoo 快照（API 权限可能与 App 不同）")

    def quote(self, symbol: str, now: datetime) -> Quote | None:
        t = time.monotonic()
        self._wanted[symbol] = t
        hit = self._cache.get(symbol)
        if hit and t - hit[0] < QUOTE_TTL_S:
            if symbol not in self._instruments:
                self.ensure_instruments([symbol])
            return hit[1]
        if symbol in self._good:
            # 合并请求：近期用到且曾经成功的证券一起刷新（一次请求代替每只一次）
            batch = sorted(x for x, w in self._wanted.items() if x in self._good and t - w < WANTED_TTL_S)
            ok = all(self._fetch(batch[i:i + BATCH_MAX], t) for i in range(0, len(batch), BATCH_MAX))
            if not ok and len(batch) > 1:
                self._fetch([symbol], t)  # 合并请求失败（例如某只已退市）：只单独取本只
        elif t - self._last_try.get(symbol, -RETRY_S) >= RETRY_S:
            self._last_try[symbol] = t  # 从未成功的代码单独试，避免拖垮合并请求
            self._fetch([symbol], t)
        hit = self._cache.get(symbol)
        if hit is None or t - hit[0] > STALE_FALLBACK_S:
            return None
        if symbol not in self._instruments:
            self.ensure_instruments([symbol])
        return hit[1]

    # ---------------------------------------------------------------- 证券资料
    def ensure_instruments(self, symbols) -> None:
        """用 get_stock_basicinfo 核实证券类型与每手股数后登记；查询失败的不登记，稍后重试。"""
        import moomoo as m

        t = time.monotonic()
        todo = sorted(x for x in set(symbols) if x not in self._instruments
                      and t - self._type_try.get(x, -RETRY_S) >= RETRY_S)
        if not todo:
            return
        for x in todo:
            self._type_try[x] = t
        try:
            ret, df = self._context().get_stock_basicinfo(m.Market.US, m.SecurityType.STOCK, code_list=todo)
        except Exception as e:
            self._err = f"证券资料查询失败：{e}"
            return
        if ret != m.RET_OK:
            self._err = f"证券资料查询失败：{df}"
            return
        for r in df.to_dict("records"):
            sym = str(r.get("code"))
            if sym not in todo or not r.get("lot_size") or str(r.get("delisting")) == "True":
                continue
            mk = sym.split(".")[0]
            raw = str(r.get("stock_type", "")).split(".")[-1].upper()
            self._instruments[sym] = InstrumentInfo(
                symbol=sym, market=mk, name=str(r.get("name") or sym), sec_type=SEC_TYPES.get(raw, raw or "UNKNOWN"),
                currency={"MY": "MYR", "US": "USD"}.get(mk, ""), lot_size=int(r["lot_size"]))

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
