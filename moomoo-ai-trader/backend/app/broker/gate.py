"""实盘闸门：LIVE_REVIEW / LIVE_AUTO 只有在以下全部满足时才能创建实盘执行通道。

1. 部署配置 LIVE_TRADING_ENABLED=true（只能在本机 .env 中设置，网页无法修改）
2. 已绑定并由用户确认的真实账户（显式账户 ID，不使用默认第一项），且 24 小时内只读验证通过
3. 该账户对授权市场的交易权限经只读诊断确认
4. 券商能可靠区分现金与融资额度
5. live 命名空间中有用户确认的风控参数（非模拟测试种子）
6. 用户已核对的真实费用表（非测试费用）
7. 有效、签名正确的运行授权
8. 保护方案已验证：券商原生止损单只读探测通过，或用户在部署配置中明确接受客户端监控止损
9. OpenD 可连接

通过后签发一次性内存许可令牌（进程内有效），执行适配器每次下单前再校验。
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core import authz
from ..db.models import AccountBinding, Authorization, FeeSchedule, MarketCapability, Portfolio, RiskPolicyVersion
from ..db.types import utcnow

_tokens: dict[str, str] = {}
_lock = threading.Lock()


@dataclass
class GateResult:
    passed: bool
    checks: list[dict] = field(default_factory=list)
    token: str | None = None

    @property
    def failures(self) -> list[str]:
        return [f"{c['name']}：{c['detail']}" for c in self.checks if not c["ok"]]


def verify_token(token, acc_id: str) -> bool:
    if not token:
        return False
    with _lock:
        return _tokens.get(token) == str(acc_id)


def revoke_all_tokens() -> None:
    with _lock:
        _tokens.clear()


def gate_checks(s: Session, portfolio_id: int | None) -> list[dict]:
    st = get_settings()
    now = utcnow()
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("部署开关", st.live_trading_enabled,
        "LIVE_TRADING_ENABLED=true" if st.live_trading_enabled else "部署配置未开启实盘（默认关闭，需在本机 .env 中设置）")
    b = s.execute(select(AccountBinding).where(AccountBinding.trd_env == "REAL", AccountBinding.active.is_(True))
                  .order_by(AccountBinding.id.desc()).limit(1)).scalar_one_or_none()
    add("账户绑定", b is not None and b.confirmed_by_user,
        f"已绑定 {b.acc_id_masked}" if b and b.confirmed_by_user else "尚未选择并确认真实账户")
    fresh = b is not None and b.verified_at is not None and now - b.verified_at < timedelta(hours=24)
    add("只读验证", fresh, "24 小时内只读验证通过" if fresh else "需要在「连接与权限诊断」重新运行只读验证")
    markets = (b.markets if b else []) or []
    for m in markets or ["MY", "US"]:
        cap = s.execute(select(MarketCapability).where(MarketCapability.market == m,
                                                       MarketCapability.item == "trade_permission")).scalar_one_or_none()
        add(f"{m} 交易权限", cap is not None and cap.status == "LOCAL_PASSED",
            cap.detail if cap else "尚未验证")
    for m in markets or ["MY", "US"]:
        # 按市场检查（该市场结算币种的现金与可提现现金都必须有值）；旧版不分市场的记录不再被采信
        cash = s.execute(select(MarketCapability).where(MarketCapability.market == m,
                                                        MarketCapability.item == "cash_fields")).scalar_one_or_none()
        add(f"{m} 现金字段", cash is not None and cash.status == "LOCAL_PASSED",
            cash.detail if cash else "尚未验证该市场能否区分现金与融资额度（请重新运行只读诊断）")
    rp = s.execute(select(RiskPolicyVersion).where(RiskPolicyVersion.confirmed_by_user.is_(True),
                                                   RiskPolicyVersion.is_test_seed.is_(False))
                   .order_by(RiskPolicyVersion.version.desc()).limit(1)).scalar_one_or_none()
    add("实盘风控参数", rp is not None, f"已确认 v{rp.version}" if rp else "尚未在实盘设置中确认风控参数（不会继承模拟参数）")
    fees = list(s.execute(select(FeeSchedule).where(FeeSchedule.active.is_(True))).scalars())
    real_fees = {f.market for f in fees if not f.is_test}
    need = set(markets or ["MY", "US"])
    add("费用表", need <= real_fees, "已核对" if need <= real_fees else f"以下市场仍使用测试费用：{sorted(need - real_fees)}")
    for m in markets or ["MY", "US"]:
        prot = s.execute(select(MarketCapability).where(MarketCapability.market == m,
                                                        MarketCapability.item == "native_stop_order")).scalar_one_or_none()
        native_ok = prot is not None and prot.status == "LOCAL_PASSED"
        client_ok = st.allow_client_side_stops_live
        add(f"{m} 保护方案", native_ok or client_ok,
            "券商原生止损单只读探测通过" if native_ok else
            ("用户已在部署配置接受客户端监控止损（断网或关机时不会触发）" if client_ok else
             "未验证券商原生止损，且未接受客户端监控止损：实盘自动交易保持禁用"))
    if portfolio_id is not None:
        a = s.execute(select(Authorization).where(Authorization.portfolio_id == portfolio_id)
                      .order_by(Authorization.id.desc()).limit(1)).scalar_one_or_none()
        p = s.get(Portfolio, portfolio_id)
        ok, why = authz.check(a, now, p.mode if p else None)
        add("运行授权", ok, why)
    from .moomoo_adapter import tcp_probe

    up = tcp_probe(st.moomoo_opend_host, st.moomoo_opend_port, timeout=0.8)
    add("OpenD 连接", up, "可连接" if up else f"无法连接 {st.moomoo_opend_host}:{st.moomoo_opend_port}")
    return checks


def live_gate(s: Session, portfolio_id: int | None) -> GateResult:
    checks = gate_checks(s, portfolio_id)
    passed = all(c["ok"] for c in checks)
    token = None
    if passed:
        b = s.execute(select(AccountBinding).where(AccountBinding.trd_env == "REAL", AccountBinding.active.is_(True))
                      .order_by(AccountBinding.id.desc()).limit(1)).scalar_one()
        token = secrets.token_urlsafe(24)
        with _lock:
            _tokens[token] = b.acc_id
    return GateResult(passed, checks, token)
