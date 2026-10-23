"""初始化种子数据、创建/撤销运行授权。

- sim 命名空间：种子风控参数（测试用）、测试费用表、默认策略版本、默认股票池规则（需在向导中确认）。
- live 命名空间：只有策略版本与「待核对」测试费用表；没有已批准的风控参数，默认实盘范围为空。
  实盘参数不会从模拟参数自动继承为已批准。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import authz
from ..core.audit import audit
from ..core.enums import Mode, Namespace
from ..core.money import TEST_FEE_SCHEDULES
from ..db.models import (
    AccountBinding,
    Authorization,
    FeeSchedule,
    Portfolio,
    RiskPolicyVersion,
    StrategyVersion,
    UniverseRule,
)
from ..db.session import migrate, write_session
from ..db.types import D, utcnow
from ..market_data.universe import DEFAULT_POOL_RULES
from ..portfolio import ledger
from ..risk.policy import SEED_RISK_PARAMS, validate_params
from ..strategies.ai_autonomous import DEFAULT_AI_PARAMS
from ..strategies.baseline_ma import DEFAULT_BASELINE_PARAMS


def ensure_seed(ns: str) -> None:
    migrate(ns)
    now = utcnow()
    with write_session(ns) as s:
        if not s.execute(select(StrategyVersion.id)).first():
            from ..ai.prompt import PROMPT_VERSION

            s.add(StrategyVersion(key="AI_AUTONOMOUS", version="v1", params=DEFAULT_AI_PARAMS, prompt_version=PROMPT_VERSION,
                                  status="ACTIVE", approved_by_user=True, approved_at=now,
                                  note="AI 自主决策政策 v1（随软件交付的初始版本）"))
            s.add(StrategyVersion(key="BASELINE_MA", version="v1", params=DEFAULT_BASELINE_PARAMS, status="ACTIVE",
                                  approved_by_user=True, approved_at=now, note="固定规则基准：MA20/MA60 交叉"))
        if not s.execute(select(FeeSchedule.id)).first():
            for m, sched in TEST_FEE_SCHEDULES.items():
                s.add(FeeSchedule(market=m, params=sched, source=sched["label"], as_of=now, is_test=True))
        if ns == Namespace.SIM:
            if not s.execute(select(RiskPolicyVersion.id)).first():
                s.add(RiskPolicyVersion(version=1, params=SEED_RISK_PARAMS, confirmed_by_user=True, confirmed_at=now,
                                        is_test_seed=True, note="LOCAL_PAPER/DEMO 测试种子参数（非实盘授权）"))
            if not s.execute(select(UniverseRule.id)).first():
                s.add(UniverseRule(version=1, rules=DEFAULT_POOL_RULES, confirmed_by_user=False,
                                   note="默认股票池规则（需在启动向导中确认后生效）"))
            if not s.execute(select(AccountBinding.id).where(AccountBinding.broker == "LOCAL_SIM")).first():
                s.add(AccountBinding(broker="LOCAL_SIM", trd_env="LOCAL", acc_id="LOCAL-SIM-1",
                                     acc_id_masked="LOCAL-SIM-1", markets=["MY", "US"], confirmed_by_user=True,
                                     confirmed_at=now, verified_at=now, verification={"note": "本地模拟账户"}))


def create_authorization(s: Session, *, portfolio: Portfolio, capital: dict[str, str], max_planned_loss: dict[str, str],
                         scope: dict, strategy_version_id: int, risk_policy_version_id: int, overnight: bool,
                         valid_hours: float, exit_rules: dict | None, actor: str, as_of: datetime) -> Authorization:
    mode = Mode(portfolio.mode)
    rp = s.get(RiskPolicyVersion, risk_policy_version_id)
    if rp is None or not rp.confirmed_by_user:
        raise ValueError("风控参数版本未经用户确认，不能授权")
    errs = validate_params(rp.params)
    if errs:
        raise ValueError("风控参数无效：" + "；".join(errs))
    if mode.is_live and rp.is_test_seed:
        raise ValueError("实盘不能使用模拟测试种子参数")
    sv = s.get(StrategyVersion, strategy_version_id)
    if sv is None or sv.status != "ACTIVE" or not sv.approved_by_user:
        raise ValueError("策略版本未批准或未生效")
    if scope.get("type") == "POOL":
        rule = s.get(UniverseRule, scope.get("universe_rule_id"))
        if rule is None or not rule.confirmed_by_user:
            raise ValueError("股票池规则尚未确认")
    elif scope.get("type") == "WHITELIST":
        if not scope.get("symbols"):
            raise ValueError("白名单为空")
    else:
        raise ValueError("必须指定交易范围（白名单或股票池）")
    for ccy, amt in capital.items():
        if ccy not in ("MYR", "USD") or D(amt) <= 0:
            raise ValueError(f"可使用资金无效：{ccy} {amt}")
        mpl = D(max_planned_loss.get(ccy, "0"))
        if mpl <= 0 or mpl > D(amt):
            raise ValueError(f"{ccy} 可承受亏损必须大于 0 且不超过可使用资金")
    if valid_hours <= 0 or valid_hours > 24 * 90:
        raise ValueError("运行期限必须在 0–90 天之间")
    wall = utcnow()
    # 旧授权被新授权取代
    for old in s.execute(select(Authorization).where(Authorization.portfolio_id == portfolio.id,
                                                     Authorization.status == "ACTIVE")).scalars():
        old.status = "SUPERSEDED"
    a = Authorization(
        portfolio_id=portfolio.id, mode=mode.value, account_binding_id=portfolio.account_binding_id,
        capital={k: str(D(v)) for k, v in capital.items()},
        max_planned_loss={k: str(D(v)) for k, v in max_planned_loss.items()}, scope=scope,
        permissions={"buy": True, "sell": True, "cancel": True, "reprice": True},
        strategy_version_ids=[strategy_version_id], risk_policy_version_id=risk_policy_version_id,
        overnight=overnight, exit_rules=exit_rules or {"on_expiry": "KEEP_PROTECTION"},
        valid_from=wall - timedelta(seconds=1), expires_at=wall + timedelta(hours=valid_hours), status="ACTIVE",
        created_by=actor,
    )
    s.add(a)
    s.flush()
    a.summary_text = authorization_summary(a, sv, rp, scope)
    a.signature = authz.sign(a)
    # 资金分配：使分配总额等于本次授权的可使用资金（差额作为现金流记录，不计入盈亏）
    for ccy, amt in a.capital.items():
        cur = ledger.total_flows(s, portfolio.id, ccy)
        delta = D(amt) - cur
        if delta != 0:
            ledger.allocate(s, portfolio.id, mode.value, ccy, delta, as_of,
                            note=f"运行授权 #{a.id} 设定可使用资金 {amt} {ccy}")
    audit(s, "AUTH", "CREATED", f"创建运行授权 #{a.id}：{a.summary_text[:200]}", actor=actor, portfolio_id=portfolio.id,
          as_of=as_of)
    return a


def authorization_summary(a: Authorization, sv: StrategyVersion, rp: RiskPolicyVersion, scope: dict) -> str:
    p = rp.params
    cap = "、".join(f"{v} {k}" for k, v in a.capital.items())
    loss = "、".join(f"{v} {k}" for k, v in a.max_planned_loss.items())
    sc = (f"授权股票池（规则 #{scope.get('universe_rule_id')}，成员由程序按规则每日更新）" if scope.get("type") == "POOL"
          else f"白名单 {', '.join(scope.get('symbols', []))}")
    return (f"模式：{Mode(a.mode).label}。可使用资金：{cap}。可承受计划亏损：{loss}（达到后停止开新仓；"
            f"这是停止线，不保证最大实际亏损，跳空或无法成交时亏损可能更大）。交易范围：{sc}。"
            f"策略：{sv.key} {sv.version}（AI 在范围内自行选择买卖，程序风控计算数量，无需逐笔确认）。"
            f"单标的上限 {D(p['max_position_pct']) * 100:.0f}%、总投入上限 {D(p['max_total_exposure_pct']) * 100:.0f}%、"
            f"单笔计划风险 {D(p['risk_per_trade_pct']) * 100:.2f}%、最多持有 {p['max_positions']} 个、"
            f"每日新开仓最多 {p['max_daily_new_orders']} 笔、当日亏损 {D(p['daily_loss_pct']) * 100:.1f}% 暂停、"
            f"回撤 {D(p['max_drawdown_pct']) * 100:.1f}% 暂停。{'允许' if a.overnight else '不允许'}隔夜持仓。"
            f"有效期至 {a.expires_at.isoformat()}；到期或撤销后停止新开仓，已有持仓按止损/止盈/持有期限规则自动管理。"
            f"禁止：融资、做空、期权、自动换汇、入金出金、管理账户中的其他持仓。")


def start_sim_run(*, mode: str = "DEMO", data_source: str = "DEMO_FIXTURE", capital: dict[str, str] | None = None,
                  max_planned_loss: dict[str, str] | None = None, scope: dict | None = None,
                  strategy_key: str = "AI_AUTONOMOUS", overnight: bool = True, valid_hours: float = 24 * 30,
                  actor: str = "user", confirm_pool_rule: bool = True, sim_start: datetime | None = None) -> tuple[int, int]:
    ns = Namespace.SIM
    ensure_seed(ns)
    capital = capital or {"MYR": "20000", "USD": "5000"}
    max_planned_loss = max_planned_loss or {k: str(D(v) * Decimal("0.05")) for k, v in capital.items()}
    with write_session(ns) as s:
        binding = s.execute(select(AccountBinding).where(AccountBinding.broker == "LOCAL_SIM")).scalar_one()
        for old in s.execute(select(Portfolio).where(Portfolio.running.is_(True), Portfolio.mode == mode)).scalars():
            old.running = False
            for a in s.execute(select(Authorization).where(Authorization.portfolio_id == old.id,
                                                           Authorization.status == "ACTIVE")).scalars():
                a.status = "SUPERSEDED"
        rule = s.execute(select(UniverseRule).order_by(UniverseRule.version.desc()).limit(1)).scalar_one()
        if scope is None:
            if confirm_pool_rule and not rule.confirmed_by_user:
                rule.confirmed_by_user = True
                rule.confirmed_at = utcnow()
                audit(s, "UNIVERSE", "CONFIRM", f"用户确认股票池规则 v{rule.version}", actor=actor)
            scope = {"type": "POOL", "universe_rule_id": rule.id}
        if sim_start is None and data_source == "DEMO_FIXTURE":
            from ..market_data.fixtures import DEMO_REPLAY_START

            sim_start = datetime.fromisoformat(DEMO_REPLAY_START)
        if data_source == "DEMO_FIXTURE":
            from ..db.models import FxRate

            if not s.execute(select(FxRate.id).where(FxRate.source.like("演示%"))).first():
                s.add(FxRate(pair="USDMYR", rate=D("4.20"), source="演示固定汇率（非真实，仅用于折算展示）",
                             as_of=sim_start or utcnow()))
        p = Portfolio(mode=mode, account_binding_id=binding.id, name=f"{mode} 运行", data_source=data_source,
                      sim_clock=sim_start, running=True)
        s.add(p)
        s.flush()
        rp = s.execute(select(RiskPolicyVersion).where(RiskPolicyVersion.confirmed_by_user.is_(True))
                       .order_by(RiskPolicyVersion.version.desc()).limit(1)).scalar_one()
        sv = s.execute(select(StrategyVersion).where(StrategyVersion.key == strategy_key,
                                                     StrategyVersion.status == "ACTIVE")).scalar_one()
        a = create_authorization(s, portfolio=p, capital=capital, max_planned_loss=max_planned_loss, scope=scope,
                                 strategy_version_id=sv.id, risk_policy_version_id=rp.id, overnight=overnight,
                                 valid_hours=valid_hours, exit_rules=None, actor=actor, as_of=sim_start or utcnow())
        return p.id, a.id


def revoke(ns: str, portfolio_id: int, actor: str) -> int:
    with write_session(ns) as s:
        n = 0
        for a in s.execute(select(Authorization).where(Authorization.portfolio_id == portfolio_id,
                                                       Authorization.status == "ACTIVE")).scalars():
            a.status = "REVOKED"
            a.revoked_at = utcnow()
            n += 1
        audit(s, "AUTH", "REVOKED", "用户撤销运行授权：立即停止新开仓；已有持仓按批准的保护规则管理", actor=actor,
              portfolio_id=portfolio_id)
        return n


def current_portfolio(s: Session, ns: str) -> Portfolio | None:
    p = s.execute(select(Portfolio).where(Portfolio.running.is_(True)).order_by(Portfolio.id.desc()).limit(1)).scalar_one_or_none()
    if p is None:
        p = s.execute(select(Portfolio).order_by(Portfolio.id.desc()).limit(1)).scalar_one_or_none()
    return p


