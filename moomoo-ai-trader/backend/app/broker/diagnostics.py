"""连接与权限只读诊断。绝不调用 place_order / modify_order / unlock_trade。

SDK 在 OpenD 未启动时会在构造函数里无限重试连接，因此诊断在独立子进程中运行并设置总超时；
主进程只读取子进程输出的 JSON。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..config import get_settings

LOGIN_STEPS = [
    "在这台电脑上打开 moomoo OpenD（官网下载：https://www.moomoo.com/download/OpenAPI）。",
    "在 OpenD 窗口里用您的 moomoo 账号登录。密码和验证码只在 OpenD 官方界面输入，不要发给任何人或填进本系统。",
    "确认 OpenD 的监听地址为 127.0.0.1、端口 11111（与本系统 .env 中的 MOOMOO_OPEND_PORT 一致）。",
    "回到本页面点击「重新检测」。",
]


def parse_runner_output(stdout: str | None) -> dict[str, Any]:
    """从诊断子进程的标准输出中取出结果 JSON。

    SDK 会把自己的日志打印到标准输出，而且 close() 的日志可能出现在结果之后，
    因此不能只解析最后一行：从后往前找第一行「是 dict 且 items 为列表」的 JSON，找不到返回 {}。
    """
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("items"), list):
            return obj
    return {}


def run_diagnostics(acc_id: str | None = None, trd_env: str | None = None, symbols: list[str] | None = None,
                    timeout: float = 45) -> dict[str, Any]:
    st = get_settings()
    from .moomoo_adapter import tcp_probe

    base = {"opend": f"{st.moomoo_opend_host}:{st.moomoo_opend_port}", "security_firm": st.moomoo_security_firm}
    if not tcp_probe(st.moomoo_opend_host, st.moomoo_opend_port):
        return base | {"ok": False, "items": [{"key": "opend", "label": "OpenD 连接", "status": "FAIL",
                                               "detail": "无法连接 OpenD（端口未监听）：OpenD 未启动或端口不同",
                                               "hint": LOGIN_STEPS}], "accounts": []}
    args = [sys.executable, "-m", "app.broker.diagnostics", json.dumps({"acc_id": acc_id, "trd_env": trd_env,
                                                                       "symbols": symbols or []})]
    backend = Path(__file__).resolve().parents[2]
    try:
        out = subprocess.run(args, cwd=str(backend), capture_output=True, text=True, timeout=timeout)
        data = parse_runner_output(out.stdout)
        if not data:
            data = {"ok": False, "items": [{"key": "runner", "label": "诊断进程", "status": "FAIL",
                                            "detail": (out.stderr or "无输出")[-500:]}], "accounts": []}
    except subprocess.TimeoutExpired:
        data = {"ok": False, "items": [{"key": "opend", "label": "OpenD 响应", "status": "FAIL",
                                        "detail": f"{timeout:.0f} 秒内没有完成初始化：OpenD 可能未登录或正在升级",
                                        "hint": LOGIN_STEPS}], "accounts": []}
    return base | data


def _main(params: dict) -> dict:
    import moomoo as m

    st = get_settings()
    items: list[dict] = []
    accounts: list[dict] = []

    def add(key, label, status, detail, **kw):
        items.append({"key": key, "label": label, "status": status, "detail": detail, **kw})

    ver = (Path(m.__file__).parent / "VERSION.txt")
    add("sdk", "SDK 版本", "PASS", f"moomoo-api {ver.read_text().strip() if ver.exists() else '?'}")
    q = m.OpenQuoteContext(host=st.moomoo_opend_host, port=st.moomoo_opend_port)
    try:
        ret, gs = q.get_global_state()
        if ret == m.RET_OK:
            add("opend", "OpenD 连接", "PASS", f"OpenD 版本 {gs.get('server_ver')}")
            add("qot_login", "行情登录", "PASS" if str(gs.get("qot_logined")) in ("1", "True") else "FAIL",
                "已登录行情服务器" if str(gs.get("qot_logined")) in ("1", "True") else "行情未登录", hint=LOGIN_STEPS)
            add("trd_login", "交易登录", "PASS" if str(gs.get("trd_logined")) in ("1", "True") else "FAIL",
                "已登录交易服务器" if str(gs.get("trd_logined")) in ("1", "True") else "交易未登录", hint=LOGIN_STEPS)
            add("market_state", "市场状态", "INFO", f"马股 {gs.get('market_my')}，美股 {gs.get('market_us')}")
        else:
            add("opend", "OpenD 连接", "FAIL", str(gs), hint=LOGIN_STEPS)
        syms = params.get("symbols") or []
        for sym in syms[:6]:
            r, snap = q.get_market_snapshot([sym])
            mk = sym.split(".")[0]
            if r == m.RET_OK and len(snap):
                row = snap.to_dict("records")[0]
                add(f"quote_{sym}", f"{mk} 行情权限（{sym}）", "PASS",
                    f"快照可用：最新价 {row.get('last_price')}，更新时间 {row.get('update_time')}；买一 {row.get('bid_price')} 卖一 {row.get('ask_price')}",
                    market=mk, lot_size=row.get("lot_size"), name=row.get("name"))
            else:
                add(f"quote_{sym}", f"{mk} 行情权限（{sym}）", "NEEDS_PERMISSION", f"无法获取快照：{snap}", market=mk)
        r, sub = q.query_subscription()
        add("quota", "行情订阅额度", "INFO" if r == m.RET_OK else "FAIL", str(sub)[:400])
    finally:
        q.close()
    for market in ("MY", "US"):
        try:
            t = m.OpenSecTradeContext(filter_trdmarket=getattr(m.TrdMarket, market), host=st.moomoo_opend_host,
                                      port=st.moomoo_opend_port, security_firm=getattr(m.SecurityFirm, st.moomoo_security_firm))
        except Exception as e:
            add(f"trade_ctx_{market}", f"{market} 交易连接", "FAIL", str(e))
            continue
        try:
            r, acc = t.get_acc_list()
            if r != m.RET_OK:
                add(f"acc_{market}", f"{market} 账户列表", "FAIL", str(acc))
                continue
            for row in acc.to_dict("records"):
                accounts.append({"acc_id": str(row["acc_id"]), "acc_id_masked": "*" * 6 + str(row["acc_id"])[-4:],
                                 "trd_env": str(row["trd_env"]), "acc_type": str(row.get("acc_type")),
                                 "security_firm": str(row.get("security_firm")), "markets": [str(x) for x in (row.get("trdmarket_auth") or [])],
                                 "status": str(row.get("acc_status")), "filter_market": market})
            add(f"acc_{market}", f"{market} 账户列表", "PASS", f"{len(acc)} 个账户（请在页面上明确选择，不会默认使用第一个）")
            acc_id, env = params.get("acc_id"), params.get("trd_env")
            if acc_id and env:
                envv = m.TrdEnv.REAL if env == "REAL" else m.TrdEnv.SIMULATE
                matched = [a for a in accounts if a["acc_id"] == str(acc_id) and a["trd_env"] == env and a["filter_market"] == market]
                if not matched:
                    add(f"bind_{market}", f"{market} 所选账户", "SKIPPED", "所选账户不在此市场的账户列表中")
                    continue
                perm = market in matched[0]["markets"]
                add(f"trade_permission_{market}", f"{market} 交易权限", "PASS" if perm else "NEEDS_PERMISSION",
                    f"账户市场权限：{matched[0]['markets']}", market=market, capability="trade_permission")
                r, info = t.accinfo_query(trd_env=envv, acc_id=int(acc_id), refresh_cache=True, currency=m.Currency.MYR)
                if r == m.RET_OK:
                    from .moomoo_adapter import parse_cash

                    row = info.to_dict("records")[0]
                    cash = parse_cash(row)
                    rel = all(c.reliable for c in cash)
                    add("cash_fields", "现金与融资区分", "PASS" if rel else "FAIL",
                        "；".join(f"{c.currency} 可用可结算现金 {c.cash}（购买力 {c.buying_power} 仅展示）" for c in cash)
                        + f"；总资产 {row.get('total_assets')}；账户类型 {matched[0]['acc_type']}", capability="cash_fields",
                        total_assets=str(row.get("total_assets")))
                else:
                    add("cash_fields", "现金与融资区分", "FAIL", str(info), capability="cash_fields")
                r, pos = t.position_list_query(trd_env=envv, acc_id=int(acc_id))
                add(f"positions_{market}", f"{market} 持仓查询", "PASS" if r == m.RET_OK else "FAIL",
                    f"{len(pos)} 条持仓（只读，不会改动您原有的持仓）" if r == m.RET_OK else str(pos))
                r, od = t.order_list_query(trd_env=envv, acc_id=int(acc_id))
                add(f"orders_{market}", f"{market} 订单查询", "PASS" if r == m.RET_OK else "FAIL",
                    f"{len(od)} 条当日订单" if r == m.RET_OK else str(od))
                test_sym = next((s for s in (params.get("symbols") or []) if s.startswith(market + ".")), None)
                if test_sym:
                    for ot, cap in ((m.OrderType.NORMAL, "limit_order"), (m.OrderType.STOP_LIMIT, "native_stop_order")):
                        r, ti = t.acctradinginfo_query(order_type=ot, code=test_sym, price=1.0, trd_env=envv, acc_id=int(acc_id))
                        add(f"{cap}_{market}", f"{market} {'限价单' if cap == 'limit_order' else '止损限价单'}（只读探测）",
                            "PASS" if r == m.RET_OK else "UNSUPPORTED",
                            "acctradinginfo_query 接受该订单类型（只读探测，未下单）" if r == m.RET_OK else f"不支持或无权限：{ti}",
                            market=market, capability=cap)
                else:
                    add(f"probe_{market}", f"{market} 订单类型探测", "SKIPPED", "未提供该市场的测试证券代码，跳过")
        finally:
            t.close()
    add("rate_limit", "频率限制", "INFO", "以官方「权限及配额」文档为准；本系统下单频率远低于每分钟一次，且不会自动重发。")
    return {"ok": not any(i["status"] == "FAIL" for i in items), "items": items, "accounts": accounts}


if __name__ == "__main__":
    p = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    try:
        res = _main(p)
    except Exception as e:  # pragma: no cover - 外部环境
        res = {"ok": False, "items": [{"key": "error", "label": "诊断异常", "status": "FAIL", "detail": f"{type(e).__name__}: {e}"}],
               "accounts": []}
    print(json.dumps(res, ensure_ascii=False, default=str))
