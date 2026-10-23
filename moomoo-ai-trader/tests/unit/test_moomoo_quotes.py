"""实时行情：快照合并请求与限速、证券类型核实、演示证券不进入实时模式（LOCAL_PAPER 启动前修复 A1–A3）。"""
import moomoo as m
import pytest

from app.market_data import moomoo_quotes as mq

GOOD = [f"US.S{i}" for i in range(10)]
TYPES = {s: "STOCK" for s in GOOD} | {"US.ETF1": "ETF", "US.WRT": "WARRANT", "US.OLD": "STOCK"}


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def to_dict(self, orient):
        return self.rows


class FakeQuoteCtx:
    """假行情上下文：未知代码会让整次快照请求失败（与 SDK 行为一致的保守假设）。"""

    def __init__(self):
        self.snapshots: list[list[str]] = []
        self.basicinfo: list[list[str]] = []
        self.fail_basicinfo = False

    def get_market_snapshot(self, codes):
        self.snapshots.append(list(codes))
        if any(c not in TYPES for c in codes):
            return m.RET_ERROR, "unknown stock"
        return m.RET_OK, _Rows([{"code": c, "last_price": 10, "bid_price": 9.99, "ask_price": 10.01, "volume": 1,
                                  "update_time": "2026-10-23 10:00:00", "suspension": False, "lot_size": 1}
                                 for c in codes])

    def get_stock_basicinfo(self, market, stock_type, code_list):
        self.basicinfo.append(list(code_list))
        if self.fail_basicinfo:
            return m.RET_ERROR, "busy"
        return m.RET_OK, _Rows([{"code": c, "name": c, "lot_size": 1, "stock_type": TYPES[c],
                                  "delisting": c == "US.OLD"} for c in code_list if c in TYPES])


@pytest.fixture()
def clock(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(mq.time, "monotonic", lambda: t["now"])
    return t


def _provider():
    ctx = FakeQuoteCtx()
    return mq.MoomooQuoteProvider(ctx_factory=lambda: ctx), ctx


def test_quotes_for_many_symbols_are_batched_under_rate_limit(clock):
    prov, ctx = _provider()
    for _ in range(30):  # 60 秒，工作进程每 2 秒一轮，每轮取全部 10 只
        for s in GOOD:
            assert prov.quote(s, None) is not None
        clock["now"] += 2
    # 第一次每只单独核实（10 次），之后每 4 秒一次合并请求；旧实现约 300 次
    assert len(ctx.snapshots) <= 10 + 60 / mq.QUOTE_TTL_S + 1
    assert sorted(ctx.snapshots[-1]) == sorted(GOOD)
    per_30s = mq.SNAPSHOT_RATE[0]
    assert len(ctx.snapshots) - 10 < per_30s


def test_unknown_code_does_not_poison_batch_and_is_retried_slowly(clock):
    prov, ctx = _provider()
    for s in GOOD[:3]:
        prov.quote(s, None)
    for _ in range(20):  # 40 秒
        assert prov.quote("US.NOPE", None) is None
        for s in GOOD[:3]:
            assert prov.quote(s, None) is not None
        clock["now"] += 2
    assert sum(1 for c in ctx.snapshots if c == ["US.NOPE"]) == 1  # 60 秒内只单独试一次
    assert all("US.NOPE" not in c for c in ctx.snapshots if len(c) > 1)


def test_rate_limited_or_failed_refresh_falls_back_only_to_recent_cache(clock, monkeypatch):
    prov, ctx = _provider()
    q = prov.quote(GOOD[0], None)
    monkeypatch.setattr(mq, "SNAPSHOT_RATE", (0, 30.0))  # 之后的请求全部被限速
    clock["now"] += 10
    assert prov.quote(GOOD[0], None) is q  # 10 秒前的报价仍可用于保护（开仓另查源时间戳）
    clock["now"] += 10
    assert prov.quote(GOOD[0], None) is None  # 超过 15 秒不再使用
    assert "限速" in prov._err


def test_instrument_types_come_from_broker_and_unverified_types_are_rejected(clock):
    prov, ctx = _provider()
    for s in ("US.S0", "US.ETF1", "US.WRT", "US.OLD"):
        prov.quote(s, None)
    assert prov.instrument("US.S0").sec_type == "STOCK"
    assert prov.instrument("US.ETF1").sec_type == "ETF_UNVERIFIED"  # 接口无法判断杠杆/反向
    assert prov.instrument("US.WRT").sec_type == "WARRANT"
    assert prov.instrument("US.OLD") is None  # 已退市不登记
    # 风控只允许 STOCK / ETF（见 test_risk.py::test_rejections 中 ETF_UNVERIFIED 被拒绝）


def test_type_lookup_failure_registers_nothing_and_retries_later(clock):
    prov, ctx = _provider()
    ctx.fail_basicinfo = True
    assert prov.quote("US.S1", None) is not None  # 报价照常（保护性退出需要）
    assert prov.instrument("US.S1") is None  # 但不登记，策略不会选它
    clock["now"] += 2
    prov.quote("US.S1", None)
    assert len(ctx.basicinfo) == 1  # 60 秒内不重复查询
    ctx.fail_basicinfo = False
    clock["now"] += 60
    prov.quote("US.S1", None)
    assert prov.instrument("US.S1").sec_type == "STOCK"



def test_realtime_runtime_verifies_only_real_instruments_and_registers_late_ones(env, monkeypatch, clock):
    """实时模式：演示证券不登记；类型向券商核实；运行中才核实的证券也会登记到本地模拟券商。"""
    from app.db.models import Instrument
    from app.db.session import write_session
    from app.workers.engine import TradingEngine
    from helpers import US_OPEN, ManualClock, setup_run

    prov, ctx = _provider()
    mq.MoomooQuoteProvider._shared = prov
    pid, _ = setup_run("sim", "LOCAL_PAPER", symbols=["US.S0", "US.S1"])
    with write_session("sim") as s:
        s.add(Instrument(symbol="US.S0", market="US", name="旧记录", sec_type="STOCK", currency="USD", lot_size=1))
        s.add(Instrument(symbol="US.DMOA", market="US", name="演示", sec_type="STOCK", currency="USD", lot_size=1,
                         is_demo=True))
    eng = TradingEngine("sim", overrides={"clock": lambda pid, p: ManualClock(US_OPEN)})
    rt = eng.runtime(pid)
    assert ctx.basicinfo == [["US.S0"]]  # 只核实真实证券，演示证券不发请求
    assert "US.S0" in rt.adapter_instruments and "US.DMOA" not in rt.adapter_instruments
    prov.quote("US.S1", None)  # 运行中第一次取到报价并核实类型
    eng._register_instruments(rt)
    assert rt.adapter.state["instruments"]["US.S1"]["lot"] == 1
