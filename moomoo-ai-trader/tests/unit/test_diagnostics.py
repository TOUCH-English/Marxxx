"""只读诊断：子进程输出解析（Mac 实机发现 SDK 关闭日志出现在结果 JSON 之后，导致 JSONDecodeError）。"""
import json
import subprocess

from app.broker import diagnostics, moomoo_adapter

RESULT = {"ok": True, "items": [{"key": "opend", "label": "OpenD 连接", "status": "PASS", "detail": "OpenD 版本 x"}],
          "accounts": []}
# 仿照 SDK 的日志格式；其中一行日志本身就是 JSON，但不是诊断结果
STARTUP = "2026-10-23 10:00:00,001 | 1234 | [open_context_base.py] InitConnect ok: conn_id=1"
CLOSE = "2026-10-23 10:00:01,002 | 1234 | [network_manager.py] Close: conn_id=1"
LOG_JSON = json.dumps({"event": "keepalive", "conn_id": 1})


def _run(monkeypatch, stdout, stderr=""):
    monkeypatch.setattr(moomoo_adapter, "tcp_probe", lambda *a, **k: True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout, stderr))
    return diagnostics.run_diagnostics(symbols=["US.AAPL"])


def test_result_followed_by_sdk_close_log_is_parsed(env, monkeypatch):
    out = "\n".join([STARTUP, json.dumps(RESULT, ensure_ascii=False), CLOSE, LOG_JSON, ""])
    res = _run(monkeypatch, out)
    assert res["ok"] is True
    assert res["items"] == RESULT["items"]
    assert "opend" in res and "security_firm" in res


def test_last_result_wins_and_non_result_json_is_ignored(env, monkeypatch):
    older = {"ok": False, "items": [], "accounts": []}
    out = "\n".join([json.dumps(older), STARTUP, json.dumps(RESULT), "{not json", LOG_JSON])
    assert diagnostics.parse_runner_output(out) == RESULT


def test_no_result_reports_runner_failure_instead_of_success(env, monkeypatch):
    res = _run(monkeypatch, "\n".join([STARTUP, LOG_JSON, CLOSE]), stderr="Traceback: boom")
    assert res["ok"] is False
    assert res["items"][0]["key"] == "runner" and res["items"][0]["status"] == "FAIL"
    assert "boom" in res["items"][0]["detail"]
    assert diagnostics.parse_runner_output("") == {} and diagnostics.parse_runner_output(None) == {}


# ---------------- 按市场/币种检查现金字段（Mac 实测：美股模拟账户 my_* 缺失、us_* 有值却被判 FAIL）
import sys
import types

US_ACC = 87654321


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def to_dict(self, orient):
        assert orient == "records"
        return self.rows


def _fake_sdk(accinfo_row):
    """最小的假 moomoo SDK：只实现诊断用到的只读接口；任何下单/解锁接口都不存在，调用即报错。"""
    m = types.ModuleType("moomoo")
    m.__file__ = __file__
    m.RET_OK, m.RET_ERROR = 0, -1
    m.TrdMarket = types.SimpleNamespace(MY="MY", US="US")
    m.SecurityFirm = types.SimpleNamespace(FUTUMY="FUTUMY")
    m.TrdEnv = types.SimpleNamespace(REAL="REAL", SIMULATE="SIMULATE")
    m.Currency = types.SimpleNamespace(MYR="MYR")
    m.OrderType = types.SimpleNamespace(NORMAL="NORMAL", STOP_LIMIT="STOP_LIMIT")
    calls = []

    class Quote:
        def __init__(self, **kw):
            pass

        def get_global_state(self):
            return 0, {"server_ver": "x", "qot_logined": True, "trd_logined": True, "market_my": "-", "market_us": "-"}

        def get_market_snapshot(self, codes):
            return 0, _Rows([{"last_price": 1, "update_time": "t", "bid_price": 1, "ask_price": 1, "lot_size": 1, "name": "n"}])

        def query_subscription(self):
            return 0, {}

        def close(self):
            pass

    class Trade:
        def __init__(self, filter_trdmarket, **kw):
            self.market = filter_trdmarket

        def get_acc_list(self):
            if self.market != "US":
                return 0, _Rows([])
            return 0, _Rows([{"acc_id": US_ACC, "trd_env": "SIMULATE", "acc_type": "CASH", "security_firm": "FUTUMY",
                              "trdmarket_auth": ["US"], "acc_status": "ACTIVE"}])

        def accinfo_query(self, **kw):
            calls.append(("accinfo_query", kw))
            return 0, _Rows([dict(accinfo_row)])

        def position_list_query(self, **kw):
            return 0, _Rows([])

        def order_list_query(self, **kw):
            return 0, _Rows([])

        def acctradinginfo_query(self, order_type, **kw):
            if order_type == "STOP_LIMIT":
                return -1, "Paper trading does not support Stop Limit order"
            return 0, _Rows([{}])

        def close(self):
            pass

    m.OpenQuoteContext, m.OpenSecTradeContext = Quote, Trade
    return m, calls


def _diag(monkeypatch, row):
    m, calls = _fake_sdk(row)
    monkeypatch.setitem(sys.modules, "moomoo", m)
    res = diagnostics._main({"acc_id": str(US_ACC), "trd_env": "SIMULATE", "symbols": ["US.AAPL"]})
    return res, {i["key"]: i for i in res["items"]}, calls


# Mac 实测的字段存在情况（金额是虚构的测试值）
MAC_ROW = {"my_cash": "N/A", "my_avl_withdrawal_cash": "N/A", "us_cash": "1000", "us_avl_withdrawal_cash": "900",
           "usd_net_cash_power": "5000", "cash": "1000", "avl_withdrawal_cash": "900", "total_assets": "1000"}


def test_us_account_checks_only_usd_and_ignores_unused_myr(env, monkeypatch):
    res, items, calls = _diag(monkeypatch, MAC_ROW)
    cash = items["cash_fields_US"]
    assert cash["status"] == "PASS" and cash["market"] == "US" and cash["capability"] == "cash_fields"
    # 取「现金」与「可提现现金」较小者；购买力 5000 只展示
    assert "USD 可用可结算现金 900" in cash["detail"] and "不用于风控" in cash["detail"]
    assert "cash_fields_MY" not in items and "cash_fields" not in items
    assert items["native_stop_order_US"]["status"] == "UNSUPPORTED"  # 模拟账户不支持止损限价单，如实记录
    assert res["ok"] is True
    # 只读查询显式指定模拟环境与账户
    assert calls and all(kw["trd_env"] == "SIMULATE" and kw["acc_id"] == US_ACC for _, kw in calls)


def test_missing_usd_field_fails_and_names_it_without_using_buying_power(env, monkeypatch):
    row = MAC_ROW | {"us_avl_withdrawal_cash": "N/A"}  # 购买力和不分币种的 cash 都还有值
    res, items, _ = _diag(monkeypatch, row)
    cash = items["cash_fields_US"]
    assert cash["status"] == "FAIL" and res["ok"] is False
    assert "USD 缺少字段：us_avl_withdrawal_cash。" in cash["detail"]
    assert "可用可结算现金" not in cash["detail"]
    row = MAC_ROW | {"us_cash": None, "us_avl_withdrawal_cash": ""}
    _, items, _ = _diag(monkeypatch, row)
    assert "us_cash, us_avl_withdrawal_cash" in items["cash_fields_US"]["detail"]


def test_missing_cash_fields_and_parse_cash_are_unchanged_for_risk(env):
    assert moomoo_adapter.missing_cash_fields(MAC_ROW, "USD") == []
    assert moomoo_adapter.missing_cash_fields(MAC_ROW, "MYR") == ["my_cash", "my_avl_withdrawal_cash"]
    by = {c.currency: c for c in moomoo_adapter.parse_cash(MAC_ROW)}
    assert by["USD"].reliable and str(by["USD"].cash) == "900"
    assert not by["MYR"].reliable and by["MYR"].cash is None  # 下单风控仍按币种拒绝不可靠现金
