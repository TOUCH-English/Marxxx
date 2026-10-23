"""moomoo 执行适配器（OpenD + 官方 Python SDK `moomoo-api`）。

安全约束：
- 每个调用都显式传入 trd_env（SIMULATE/REAL）和非 0 的 acc_id。SDK 的 place_order 默认 trd_env=REAL，
  acc_id=0 时 SDK 会默认使用账户列表中的某个账户——本适配器两者都不依赖，acc_id 为 0 直接拒绝。
- REAL 环境只能通过实盘闸门签发的许可令牌构造；每次下单前再次校验令牌与部署开关。
- 不调用 unlock_trade、不接触交易密码：交易解锁由用户在 OpenD 界面完成。
- SDK 推送回调在 SDK 线程中触发，只把归一化后的事件放入线程安全队列，由工作进程单线程处理。
- 下单超时/断线 → UNKNOWN，绝不自动重发。
"""
from __future__ import annotations

import logging
import queue
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from ..config import get_settings
from ..db.types import D
from .base import AdapterHealth, BrokerPosition, CancelResult, CashInfo, FillEvent, OrderUpdate, PlaceResult

log = logging.getLogger(__name__)

_STATUS_MAP = {
    "UNSUBMITTED": "SUBMITTED", "WAITING_SUBMIT": "SUBMITTED", "SUBMITTING": "SUBMITTED", "SUBMITTED": "SUBMITTED",
    "FILLED_PART": "PARTIALLY_FILLED", "FILLED_ALL": "FILLED",
    "CANCELLING_PART": "CANCEL_PENDING", "CANCELLING_ALL": "CANCEL_PENDING",
    "CANCELLED_PART": "CANCELLED", "CANCELLED_ALL": "CANCELLED",
    "FAILED": "REJECTED", "SUBMIT_FAILED": "REJECTED", "DISABLED": "REJECTED", "DELETED": "REJECTED",
    "TIMEOUT": "UNKNOWN", "FILL_CANCELLED": "UNKNOWN",
}
_UNKNOWN_HINTS = ("timeout", "超时", "disconnect", "断开", "network", "网络", "连接", "connect")


def tcp_probe(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ts(v) -> datetime:
    try:
        return datetime.fromisoformat(str(v)).replace(tzinfo=timezone.utc) if v else datetime.now(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def normalize_order_row(row: dict) -> OrderUpdate:
    raw = str(row.get("order_status", ""))
    return OrderUpdate(
        broker_order_id=str(row.get("order_id", "")), status=_STATUS_MAP.get(raw, "UNKNOWN"),
        dealt_qty=int(float(row.get("dealt_qty") or 0)),
        dealt_avg_price=D(row["dealt_avg_price"]) if row.get("dealt_avg_price") not in (None, "", "N/A") else None,
        remark=str(row.get("remark") or ""), raw_status=raw, message=str(row.get("last_err_msg") or ""),
        ts=_ts(row.get("updated_time")), symbol=str(row.get("code", "")),
        side="BUY" if str(row.get("trd_side")) == "BUY" else "SELL", qty=int(float(row.get("qty") or 0)),
        price=D(row["price"]) if row.get("price") not in (None, "") else None,
    )


def normalize_deal_row(row: dict) -> FillEvent:
    return FillEvent(
        broker_fill_id=str(row.get("deal_id")), broker_order_id=str(row.get("order_id")), symbol=str(row.get("code")),
        side="BUY" if str(row.get("trd_side")) == "BUY" else "SELL", qty=int(float(row.get("qty") or 0)),
        price=D(row.get("price")), ts=_ts(row.get("create_time")), fee=None,
    )


class LiveTradingNotPermitted(RuntimeError):
    pass


class MoomooExecutionAdapter:
    broker = "MOOMOO"

    def __init__(self, *, trd_env: str, acc_id: str, markets: list[str], gate_token: Any = None,
                 ctx_factory: Callable[[str], Any] | None = None):
        if trd_env not in ("SIMULATE", "REAL"):
            raise ValueError("trd_env 必须显式为 SIMULATE 或 REAL")
        if not acc_id or str(acc_id) == "0":
            raise ValueError("必须显式指定账户 ID（不使用 SDK 默认账户）")
        if trd_env == "REAL":
            from .gate import verify_token

            if not verify_token(gate_token, str(acc_id)):
                raise LiveTradingNotPermitted("没有有效的实盘许可令牌，拒绝创建实盘执行通道")
        self.trd_env = trd_env
        self.acc_id = str(acc_id)
        self.markets = markets
        self._token = gate_token
        self.events: queue.Queue = queue.Queue()
        self._ctx: dict[str, Any] = {}
        self._factory = ctx_factory or self._default_factory
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="moomoo")
        self._lock = threading.Lock()
        self._last_error = ""
        self.last_accinfo: dict = {}

    # ------------------------------------------------------------ 构造
    @classmethod
    def for_paper(cls, binding) -> "MoomooExecutionAdapter":
        if binding is None or binding.trd_env != "SIMULATE" or not binding.confirmed_by_user:
            raise RuntimeError("BROKER_PAPER 需要用户确认的券商模拟账户绑定")
        return cls(trd_env="SIMULATE", acc_id=binding.acc_id, markets=binding.markets or [])

    @classmethod
    def for_live(cls, binding, token) -> "MoomooExecutionAdapter":
        if binding is None or binding.trd_env != "REAL" or not binding.confirmed_by_user:
            raise RuntimeError("实盘需要用户确认的真实账户绑定")
        return cls(trd_env="REAL", acc_id=binding.acc_id, markets=binding.markets or [], gate_token=token)

    def _sdk(self):
        import moomoo

        return moomoo

    def _default_factory(self, market: str):
        st = get_settings()
        if not tcp_probe(st.moomoo_opend_host, st.moomoo_opend_port):
            raise ConnectionError(f"无法连接 OpenD（{st.moomoo_opend_host}:{st.moomoo_opend_port}）")
        m = self._sdk()
        ctx = m.OpenSecTradeContext(filter_trdmarket=getattr(m.TrdMarket, market), host=st.moomoo_opend_host,
                                    port=st.moomoo_opend_port, security_firm=getattr(m.SecurityFirm, st.moomoo_security_firm))
        adapter = self

        class _OrderH(m.TradeOrderHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret == m.RET_OK:
                    for row in data.to_dict("records"):
                        if str(row.get("trd_env")) in (adapter.trd_env, ""):
                            adapter.events.put(normalize_order_row(row))
                return ret, data

        class _DealH(m.TradeDealHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret == m.RET_OK:
                    for row in data.to_dict("records"):
                        adapter.events.put(normalize_deal_row(row))
                return ret, data

        ctx.set_handler(_OrderH())
        ctx.set_handler(_DealH())
        return ctx

    def ctx(self, market: str):
        with self._lock:
            if market not in self._ctx:
                self._ctx[market] = self._factory(market)
            return self._ctx[market]

    def _env(self):
        m = self._sdk()
        return m.TrdEnv.REAL if self.trd_env == "REAL" else m.TrdEnv.SIMULATE

    def _acc(self) -> int:
        return int(self.acc_id)

    @staticmethod
    def _market_of(symbol: str) -> str:
        return symbol.split(".", 1)[0]

    def _guard_live(self) -> None:
        if self.trd_env == "REAL":
            from .gate import verify_token

            if not get_settings().live_trading_enabled or not verify_token(self._token, self.acc_id):
                raise LiveTradingNotPermitted("实盘许可已失效（部署开关关闭或令牌无效）")

    # ------------------------------------------------------------ 交易
    def place_order(self, *, symbol: str, side: str, qty: int, limit_price: Decimal, remark: str,
                    now: datetime) -> PlaceResult:
        self._guard_live()
        m = self._sdk()
        if len(remark.encode("utf-8")) > 64:
            return PlaceResult("REJECTED", message="remark 超过 64 字节")
        try:
            ctx = self.ctx(self._market_of(symbol))
        except Exception as e:
            return PlaceResult("REJECTED", message=f"未连接券商，未提交：{e}")
        call = lambda: ctx.place_order(  # noqa: E731
            price=float(limit_price), qty=int(qty), code=symbol,
            trd_side=m.TrdSide.BUY if side == "BUY" else m.TrdSide.SELL,
            order_type=m.OrderType.NORMAL, trd_env=self._env(), acc_id=self._acc(), remark=remark,
            time_in_force=m.TimeInForce.DAY, fill_outside_rth=False,
        )
        fut = self._pool.submit(call)
        try:
            ret, data = fut.result(timeout=get_settings().moomoo_order_timeout_s)
        except FutTimeout:
            return PlaceResult("UNKNOWN", message="下单请求超时：券商可能已接收，进入未知状态等待对账")
        except Exception as e:
            return PlaceResult("UNKNOWN", message=f"下单异常：{e}")
        if ret != m.RET_OK:
            msg = str(data)
            if any(h in msg.lower() for h in _UNKNOWN_HINTS):
                return PlaceResult("UNKNOWN", message=f"下单结果不确定：{msg}")
            return PlaceResult("REJECTED", message=msg)
        try:
            oid = str(data["order_id"].iloc[0])
        except Exception:
            return PlaceResult("UNKNOWN", message="券商返回成功但无法解析订单号")
        return PlaceResult("ACCEPTED", broker_order_id=oid)

    def cancel_order(self, broker_order_id: str, now: datetime) -> CancelResult:
        self._guard_live()
        m = self._sdk()
        last = None
        for market in self.markets or ["US", "MY"]:
            try:
                ctx = self.ctx(market)
                ret, data = self._pool.submit(lambda c=ctx: c.modify_order(
                    m.ModifyOrderOp.CANCEL, broker_order_id, 0, 0, trd_env=self._env(), acc_id=self._acc())
                ).result(timeout=get_settings().moomoo_order_timeout_s)
            except FutTimeout:
                return CancelResult("UNKNOWN", "撤单请求超时")
            except Exception as e:
                last = str(e)
                continue
            if ret == m.RET_OK:
                return CancelResult("REQUESTED")
            last = str(data)
        return CancelResult("REJECTED", last or "撤单失败")

    # ------------------------------------------------------------ 只读
    def _each_market(self, fn) -> list:
        out = []
        for market in self.markets or ["US", "MY"]:
            ctx = self.ctx(market)
            ret, data = self._pool.submit(lambda c=ctx: fn(c)).result(timeout=get_settings().moomoo_order_timeout_s)
            if ret != self._sdk().RET_OK:
                raise RuntimeError(str(data))
            out.extend(data.to_dict("records"))
        return out

    def query_orders(self, now: datetime) -> list[OrderUpdate]:
        rows = self._each_market(lambda c: c.order_list_query(trd_env=self._env(), acc_id=self._acc(), refresh_cache=True))
        return [normalize_order_row(r) for r in rows]

    def query_fills(self, now: datetime) -> list[FillEvent]:
        try:
            rows = self._each_market(lambda c: c.deal_list_query(trd_env=self._env(), acc_id=self._acc(), refresh_cache=True))
        except RuntimeError as e:
            if self.trd_env == "SIMULATE":
                log.info("deal_list_query unavailable in SIMULATE: %s", e)
                return []
            raise
        return [normalize_deal_row(r) for r in rows]

    def positions(self) -> list[BrokerPosition]:
        rows = self._each_market(lambda c: c.position_list_query(trd_env=self._env(), acc_id=self._acc(), refresh_cache=True))
        out = {}
        for r in rows:
            q = int(float(r.get("qty") or 0))
            out[str(r["code"])] = BrokerPosition(symbol=str(r["code"]), qty=q, can_sell_qty=int(float(r.get("can_sell_qty") or 0)),
                                                 cost_price=D(r["cost_price"]) if r.get("cost_price") not in (None, "", "N/A") else None)
        return list(out.values())

    def cash(self) -> list[CashInfo]:
        m = self._sdk()
        market = (self.markets or ["US"])[0]
        ctx = self.ctx(market)
        ret, data = self._pool.submit(lambda: ctx.accinfo_query(trd_env=self._env(), acc_id=self._acc(), refresh_cache=True,
                                                                currency=m.Currency.MYR)).result(timeout=10)
        if ret != m.RET_OK:
            raise RuntimeError(str(data))
        row = data.to_dict("records")[0]
        self.last_accinfo = {"total_assets": str(row.get("total_assets")), "currency": "MYR",
                             "note": "券商账户总资产（只读，含系统未管理的持仓）"}
        return parse_cash(row)

    def health(self) -> AdapterHealth:
        st = get_settings()
        ok = tcp_probe(st.moomoo_opend_host, st.moomoo_opend_port, timeout=0.5)
        return AdapterHealth(connected=ok, detail=f"OpenD {'可连接' if ok else '无法连接'}（{self.trd_env}，账户 {self.acc_id[-4:]}）")

    def on_market_tick(self, now: datetime, provider) -> None:
        return None


def _num(v) -> Decimal | None:
    try:
        if v is None or v == "" or str(v) in ("N/A", "nan"):
            return None
        d = D(v)
        return d if d.is_finite() else None
    except Exception:
        return None


# 各币种的（现金，可提现现金，购买力）字段名。购买力含融资，只展示，从不用于风控。
CASH_FIELDS = {"MYR": ("my_cash", "my_avl_withdrawal_cash", "myr_net_cash_power"),
               "USD": ("us_cash", "us_avl_withdrawal_cash", "usd_net_cash_power")}
MARKET_CURRENCY = {"MY": "MYR", "US": "USD"}


def missing_cash_fields(row: dict, ccy: str) -> list[str]:
    """判断该币种现金是否可靠所需、但券商没有返回有效值的字段名。"""
    cash_k, wd_k, _ = CASH_FIELDS[ccy]
    return [k for k in (cash_k, wd_k) if _num(row.get(k)) is None]


def parse_cash(row: dict) -> list[CashInfo]:
    """只使用分币种「现金」与「可提现现金」，不使用购买力（含融资）字段。"""
    out = []
    for ccy, (cash_k, wd_k, pw_k) in CASH_FIELDS.items():
        cash, wd = _num(row.get(cash_k)), _num(row.get(wd_k))
        reliable = cash is not None and wd is not None
        settled = min(cash, wd) if reliable else None
        out.append(CashInfo(currency=ccy, cash=settled, withdrawable=wd, buying_power=_num(row.get(pw_k)),
                            reliable=reliable,
                            note="取「现金」与「可提现现金」较小者作为可用且可结算现金；不使用购买力字段"
                            if reliable else "券商未返回可区分的现金字段：禁用自动开仓"))
    return out
