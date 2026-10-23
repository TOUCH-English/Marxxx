"""历史规则回测（第 3 批 A）：在 Mac 上用 moomoo 的真实历史日 K，测试「不用 AI 的固定规则」。

只读：不下单、不改设置、不调用付费 AI。历史 K 线会占用 moomoo 的「30 天内不同证券数」额度（300 只），
脚本默认至少保留 150 只给自动交易使用。

  python scripts/history_backtest.py check            # 只读检查：OpenD、K 线额度、财报日历/行业权限、SPY 日 K
  python scripts/history_backtest.py fetch            # 下载约 3 年日 K 到 data/history/（额度不够时自动少取）
  python scripts/history_backtest.py run              # 用本机的官方费用表与已确认的风控版本回测，输出中文报告

报告在 data/history/reports/ 下（.md 给人看，.json 留档）。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

OUT = ROOT / "data" / "history"
CSV_PATH = OUT / "us_daily.csv"
IND_PATH = OUT / "us_industries.json"
META_PATH = OUT / "us_meta.json"

# 当前的大市值、高成交额美股（跨行业）。这是「现在」的名单，回测有幸存者偏差，报告会写明。
RESERVE_DEFAULT = 150

DEFAULT_SYMBOLS = [
    "US.SPY",
    "US.AAPL", "US.MSFT", "US.NVDA", "US.GOOGL", "US.AMZN", "US.META", "US.AVGO", "US.AMD", "US.ORCL", "US.CRM",
    "US.ADBE", "US.CSCO", "US.QCOM", "US.TXN", "US.INTC", "US.MU", "US.AMAT", "US.IBM", "US.PLTR", "US.UBER",
    "US.JPM", "US.BAC", "US.WFC", "US.MS", "US.C", "US.V", "US.MA", "US.AXP", "US.SCHW",
    "US.UNH", "US.JNJ", "US.PFE", "US.MRK", "US.ABBV", "US.ABT", "US.BMY", "US.AMGN", "US.GILD", "US.TMO",
    "US.WMT", "US.KO", "US.PEP", "US.PG", "US.MCD", "US.NKE", "US.SBUX", "US.HD", "US.LOW", "US.TGT", "US.DIS",
    "US.TSLA", "US.XOM", "US.CVX", "US.COP", "US.SLB", "US.OXY",
    "US.CAT", "US.BA", "US.GE", "US.HON", "US.UPS", "US.LMT", "US.RTX", "US.UNP", "US.T", "US.VZ", "US.CMCSA",
]


def _ctx():
    import moomoo as m

    from app.config import get_settings

    st = get_settings()
    return m, m.OpenQuoteContext(host=st.moomoo_opend_host, port=st.moomoo_opend_port)


def _quota(ctx, m):
    ret, data = ctx.get_history_kl_quota(get_detail=True)
    if ret != m.RET_OK:
        raise RuntimeError(f"历史K线额度查询失败：{data}")
    used, remain, detail = data
    return int(used), int(remain), {str(d.get("code")) for d in (detail or [])}


def _kline(ctx, m, sym: str, start: str, end: str, autype) -> list[dict]:
    rows, key = [], None
    while True:
        ret, df, key = ctx.request_history_kline(sym, start=start, end=end, ktype=m.KLType.K_DAY, autype=autype,
                                                 max_count=1000, page_req_key=key)
        if ret != m.RET_OK:
            raise RuntimeError(str(df))
        rows += df.to_dict("records")
        time.sleep(1.0)  # 历史 K 线接口限频每 30 秒 60 次；放慢一半，给自动交易留余量
        if key is None:
            return rows


def cmd_check(_args) -> int:
    m, ctx = _ctx()
    out: dict = {}
    try:
        used, remain, seen = _quota(ctx, m)
        out["kline_quota"] = {"used": used, "remain": remain, "symbols_counted": len(seen)}
        today = date.today()
        try:
            ret, df = ctx.get_earnings_calendar(m.Market.US, begin_date=today.isoformat(),
                                                end_date=(today + timedelta(days=6)).isoformat())
            rows = df.to_dict("records") if ret == m.RET_OK else []
            out["earnings_calendar"] = {
                "ok": ret == m.RET_OK, "rows": len(rows), "distinct_symbols": len({str(r.get("security")) for r in rows}),
                "columns": list(df.columns) if ret == m.RET_OK else [],
                "sample": [{k: str(r.get(k)) for k in ("security", "earnings_date", "pub_type")} for r in rows[:3]],
                "note": "行数如果正好是 100/200/500 之类的整数，可能被截断，请回报",
                "error": None if ret == m.RET_OK else str(df)[:200]}
        except Exception as e:  # 旧版 SDK 可能没有这个接口
            out["earnings_calendar"] = {"ok": False, "error": str(e)[:200]}
        try:
            ret, df = ctx.get_owner_plate(["US.AAPL", "US.XOM", "US.CVX", "US.JPM"])
            inds: dict[str, list[str]] = {}
            if ret == m.RET_OK:  # 列出每只的全部行业板块：检查是否一只股票属于多个行业
                for r in df.to_dict("records"):
                    if str(r.get("plate_type", "")).split(".")[-1].upper() == "INDUSTRY":
                        inds.setdefault(str(r.get("code")), []).append(str(r.get("plate_name")))
            out["owner_plate"] = {"ok": ret == m.RET_OK, "industries": inds,
                                  "error": None if ret == m.RET_OK else str(df)[:200]}
        except Exception as e:
            out["owner_plate"] = {"ok": False, "error": str(e)[:200]}
        if "US.SPY" in seen or remain > RESERVE_DEFAULT:
            rows = _kline(ctx, m, "US.SPY", (today - timedelta(days=420)).isoformat(), today.isoformat(), m.AuType.QFQ)
            out["spy_daily"] = {"bars": len(rows), "enough_for_200d": len(rows) >= 200,
                                "last": str(rows[-1]["time_key"])[:10] if rows else None}
    finally:
        ctx.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _us_market_open() -> bool:
    from app.core.calendar import get_calendar

    cal = get_calendar()
    now = datetime.now(timezone.utc)
    return any(a <= now < b for a, b in cal.windows("US", cal.local_date("US", now)))


def cmd_fetch(args) -> int:
    if _us_market_open() and not args.allow_market_hours:
        print("美股正在交易：为了不和自动交易抢行情接口，请在收盘后再下载（或加 --allow-market-hours）。")
        return 2
    m, ctx = _ctx()
    OUT.mkdir(parents=True, exist_ok=True)
    end = date.today()
    start = end - timedelta(days=int(args.years * 365.25) + 10)
    try:
        used, remain, seen = _quota(ctx, m)
        symbols = list(dict.fromkeys(DEFAULT_SYMBOLS if not args.symbols else ["US.SPY"] + args.symbols.split(",")))
        free = [s for s in symbols if s in seen]
        new = [s for s in symbols if s not in seen]
        allow = max(0, remain - args.reserve)
        take = (["US.SPY"] if "US.SPY" in new else []) + [s for s in new if s != "US.SPY"]
        take = take[:allow]
        chosen = [s for s in symbols if s in free or s in take]
        skipped = [s for s in symbols if s not in chosen]
        print(f"K 线额度：已用 {used}，剩余 {remain}；保留 {args.reserve} 给自动交易。"
              f"本次取 {len(chosen)} 只（其中新占额度 {len(take)} 只），跳过 {len(skipped)} 只。")
        if "US.SPY" not in chosen:
            print("SPY 取不到（额度不足），无法回测。")
            return 2
        rows_out = []
        for i, sym in enumerate(chosen, 1):
            try:
                raw = _kline(ctx, m, sym, start.isoformat(), end.isoformat(), m.AuType.NONE)
                adj = {str(r["time_key"])[:10]: Decimal(str(r["close"]))
                       for r in _kline(ctx, m, sym, start.isoformat(), end.isoformat(), m.AuType.QFQ)}
            except Exception as e:
                print(f"  {sym} 失败：{e}")
                continue
            fac, missing = None, 0
            for r in reversed(raw):  # 从最新往回：缺少复权价的日期沿用后一天的因子，不会混入原始价造成假跳空
                k = str(r["time_key"])[:10]
                close = Decimal(str(r["close"]))
                if k in adj and close > 0:
                    fac = adj[k] / close
                else:
                    missing += 1
                r["_fac"] = fac if fac is not None else Decimal(1)
            if missing:
                print(f"  {sym}：{missing} 天缺少复权价，已沿用相邻日期的因子")
            for r in raw:
                k = str(r["time_key"])[:10]
                fac = r["_fac"]
                rows_out.append([sym, k, r["open"], r["high"], r["low"], r["close"], int(r["volume"]), f"{fac:.8f}"])
            print(f"  [{i}/{len(chosen)}] {sym}：{len(raw)} 根日 K")
        with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "date", "open", "high", "low", "close", "volume", "adj_factor"])
            w.writerows(rows_out)
        inds: dict[str, str] = {}
        stocks = [s for s in chosen if s != "US.SPY"]
        try:
            for i in range(0, len(stocks), 200):
                ret, df = ctx.get_owner_plate(stocks[i:i + 200])
                if ret == m.RET_OK:
                    for r in df.to_dict("records"):
                        if str(r.get("plate_type", "")).split(".")[-1].upper() == "INDUSTRY":
                            inds.setdefault(str(r.get("code")), str(r.get("plate_name")))
        except Exception as e:
            print(f"行业查询失败（回测时不做同行业限制）：{e}")
        IND_PATH.write_text(json.dumps(inds, ensure_ascii=False, indent=1), encoding="utf-8")
        _, remain_after, _ = _quota(ctx, m)
        META_PATH.write_text(json.dumps({
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(), "start": start.isoformat(), "end": end.isoformat(),
            "symbols": chosen, "skipped_for_quota": skipped, "kline_quota_remain_after": remain_after,
            "source": "moomoo request_history_kline（不复权成交价 + 前复权因子）",
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"完成：{CSV_PATH}（额度剩余 {remain_after}）")
    finally:
        ctx.close()
    return 0


def _read_db(notes: list[str]) -> tuple[dict | None, dict | None, Decimal | None, Decimal | None]:
    """读本机当前授权的风控版本、官方费用表、资金与停止线（只读；没有数据库时不创建）。"""
    from sqlalchemy import select

    from app.config import get_settings
    from app.db.models import Authorization, FeeSchedule, RiskPolicyVersion
    from app.db.session import read_session

    if not get_settings().db_path("sim").exists():
        notes.append("本机没有模拟数据库，使用默认值")
        return None, None, None, None
    risk = fee = cap = loss = None
    with read_session("sim") as s:
        a = s.execute(select(Authorization).where(Authorization.status == "ACTIVE")
                      .order_by(Authorization.id.desc()).limit(1)).scalar_one_or_none()
        rp = s.get(RiskPolicyVersion, a.risk_policy_version_id) if a else None
        if rp is None:
            rp = s.execute(select(RiskPolicyVersion).where(RiskPolicyVersion.confirmed_by_user.is_(True))
                           .order_by(RiskPolicyVersion.version.desc()).limit(1)).scalar_one_or_none()
        if rp:
            risk = dict(rp.params)
            notes.append(f"风控：v{rp.version}" + ("（当前授权使用的版本）" if a else "（最新已确认版本）"))
        fs = s.execute(select(FeeSchedule).where(FeeSchedule.market == "US", FeeSchedule.active.is_(True))
                       .order_by(FeeSchedule.id.desc()).limit(1)).scalar_one_or_none()
        if fs:
            fee = fs.params
            notes.append(f"费用：{fs.source}" + ("（测试费率）" if fs.is_test else ""))
        if a and "USD" in (a.capital or {}):
            cap = Decimal(str(a.capital["USD"]))
            loss = Decimal(str(a.max_planned_loss.get("USD", "0"))) or None
            notes.append(f"资金与停止线：当前授权 #{a.id}")
    return risk, fee, cap, loss


def _local_settings(args):
    from app.core.money import TEST_FEE_SCHEDULES
    from app.risk.policy import SEED_RISK_PARAMS

    notes: list[str] = []
    try:
        risk, fee, cap, loss = _read_db(notes)
    except Exception as e:
        risk = fee = cap = loss = None
        notes.append(f"读取本机设置失败，改用默认值：{str(e).splitlines()[0][:120]}")
    risk = risk or dict(SEED_RISK_PARAMS)
    if fee is None:
        fee = TEST_FEE_SCHEDULES["US"]
        notes.append("费用：未找到官方费用表，使用测试费率（结果仅供参考）")
    cap = Decimal(args.capital) if args.capital else (cap or Decimal("2448"))
    loss = Decimal(args.max_loss) if args.max_loss else (loss or Decimal("122"))
    return risk, fee, cap, loss, notes


def cmd_run(args) -> int:
    from app.simulation.rules_backtest import BENCHMARK, RulesPolicy, load_history, report_markdown, run_rules_backtest

    if not CSV_PATH.exists():
        print("还没有历史数据，请先运行：python scripts/history_backtest.py fetch")
        return 2
    factors: dict = {}
    hist = load_history(CSV_PATH, factors)
    inds = json.loads(IND_PATH.read_text(encoding="utf-8")) if IND_PATH.exists() else {}
    spy_days = [b.day for b in hist[BENCHMARK]]
    start = date.fromisoformat(args.start) if args.start else spy_days[min(260, len(spy_days) - 1)]
    end = date.fromisoformat(args.end) if args.end else spy_days[-1]
    risk, fee, cap, loss, notes = _local_settings(args)
    kw = dict(start=start, end=end, capital=cap, max_loss=loss, fee_schedule=fee, risk_params=risk, industries=inds,
              factors=factors)
    main = run_rules_backtest(hist, **kw)
    no_regime = run_rules_backtest(hist, **kw, policy=RulesPolicy(regime_filter=False))
    no_halt = run_rules_backtest(hist, **(kw | {"max_loss": None}))
    m = main.metrics
    md = report_markdown(m)
    md += "\n## 设置来源\n\n" + "".join(f"- {n}\n" for n in notes)
    md += f"- 股票清单：{len(hist) - 1} 只当前大市值美股 + SPY；行业数据 {len(inds)} 只\n"
    md += "\n## 对比（同样数据，只改一项）\n\n| 版本 | 收益 | 最大回撤 | 交易数 | 平均 R |\n|---|---|---|---|---|\n"
    for name, r in (("线上规则（含大盘过滤和停止线）", main), ("去掉大盘过滤", no_regime), ("不设停止线（只看规则本身）", no_halt)):
        x = r.metrics
        md += f"| {name} | {x['return_pct']} | {x['max_drawdown_pct']} | {x['closed_trades']} | {x['avg_r'] or '—'} |\n"
    rep = OUT / "reports"
    rep.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    (rep / f"rules_{stamp}.md").write_text(md, encoding="utf-8")
    (rep / f"rules_{stamp}.json").write_text(json.dumps({
        "metrics": m, "trades": main.trades, "equity_curve": main.equity_curve, "settings_notes": notes,
        "variants": {"no_regime": no_regime.metrics, "no_stop_line": no_halt.metrics},
    }, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(md)
    print(f"报告已保存：{rep / f'rules_{stamp}.md'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="历史规则回测（只读，不下单，不调用付费 AI）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    f = sub.add_parser("fetch")
    f.add_argument("--years", type=float, default=3.0)
    f.add_argument("--reserve", type=int, default=RESERVE_DEFAULT, help="至少保留给自动交易的 K 线额度")
    f.add_argument("--allow-market-hours", action="store_true", help="允许在美股交易时段下载（默认不允许）")
    f.add_argument("--symbols", default="", help="逗号分隔，默认用内置大市值清单")
    r = sub.add_parser("run")
    r.add_argument("--start", default="", help="例如纸上交易开始日，用来和 AI 的实际成绩比同一段时间")
    r.add_argument("--end", default="")
    r.add_argument("--capital", default="")
    r.add_argument("--max-loss", default="")
    args = ap.parse_args()
    return {"check": cmd_check, "fetch": cmd_fetch, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
