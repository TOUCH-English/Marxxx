"""交易引擎：后台工作进程每个循环调用一次 tick()。

一次 tick 对每个运行中的策略账本依次执行：
  1. 模拟撮合（仅本地模拟器）与券商推送处理
  2. 定期对账（启动时必做）
  3. 公司行动入账
  4. 读取运行授权、风控版本、授权范围、健康状态
  5. 净值更新与亏损/回撤锁定
  6. 保护性退出（止损/止盈/持有期限/不隔夜）——不依赖 AI、不依赖授权是否有效
  7. 订单维护（开仓委托到期撤单、退出委托改价）
  8. 策略决策（AI 自主决策或基准策略）
  9. 提交新订单意图
 10. 日报/周度复盘任务
 11. 回放时钟前进（演示/历史回放）
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import func, select

from ..config import get_settings
from ..core import authz
from ..core.audit import audit, notify
from ..core.calendar import get_calendar
from ..core.clock import ReplayClock, WallClock
from ..core.enums import (
    PROTECTIVE_REASONS,
    DecisionAction,
    DecisionOutcome,
    IntentReason,
    Mode,
    OrderStatus,
    Side,
)
from ..core.money import TEST_FEE_SCHEDULES
from ..db.models import (
    AccountBinding,
    Authorization,
    Command,
    FeeSchedule,
    OrderIntent,
    Portfolio,
    QuoteSnapshot,
    RiskPolicyVersion,
    StrategyVersion,
    UniverseRule,
    UniverseSnapshot,
    WorkerHeartbeat,
)
from ..db.session import read_session, write_session
from ..db.types import D, utcnow
from ..market_data.universe import screen
from ..orders import coordinator
from ..orders.manager import OrderManager
from ..portfolio import ledger
from ..risk import locks
from ..strategies import ai_autonomous, baseline_ma
from ..strategies import market_context as mctx
from ..strategies.common import TickState, record_decision
from .runtime import Runtime

log = logging.getLogger(__name__)

OPEN_SUBMITTED = [OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED]


def fixture_dir():
    return get_settings().data_dir / "fixtures"


class TradingEngine:
    def __init__(self, ns: str, *, worker_id: str = "worker", overrides: dict[str, Callable] | None = None):
        self.ns = ns
        self.worker_id = worker_id
        self.calendar = get_calendar()
        self.runtimes: dict[int, Runtime] = {}
        # 测试用注入：adapter(pid, portfolio) / provider(pid, portfolio) / clock / ai_provider
        self.overrides = overrides or {}
        self.started_at = utcnow()
        self.last_status: dict[str, Any] = {}
        self._pending_cancels: list[tuple[str, str]] = []

    # ================================================================ 运行时构建
    def _fee_schedules(self) -> dict[str, dict]:
        with read_session(self.ns) as s:
            rows = list(s.execute(select(FeeSchedule).where(FeeSchedule.active.is_(True))).scalars())
        out = {r.market: (r.params | {"label": r.params.get("label", r.source)}) for r in rows}
        return out

    def build_runtime(self, pid: int) -> Runtime:
        from ..ai.providers import get_provider

        with read_session(self.ns) as s:
            p = s.get(Portfolio, pid)
            binding = s.get(AccountBinding, p.account_binding_id)
        mode = Mode(p.mode)
        if mode.namespace.value != self.ns:
            raise RuntimeError("模式与命名空间不一致，拒绝运行")
        fees = self._fee_schedules()
        st = get_settings()
        provider = self.overrides["provider"](pid, p) if "provider" in self.overrides else None
        clock = self.overrides["clock"](pid, p) if "clock" in self.overrides else None
        adapter = self.overrides["adapter"](pid, p) if "adapter" in self.overrides else None
        data_source = p.data_source
        if provider is None:
            provider = self._make_provider(p)
        if clock is None:
            if not provider.is_realtime:
                start = p.sim_clock or datetime.fromisoformat(provider.meta.get("replay_start", "2026-06-01T00:30:00+00:00"))
                markets = sorted({i.market for i in provider.instruments()})
                clock = ReplayClock(start, timedelta(minutes=st.replay_step_minutes), markets, self.calendar)
            else:
                clock = WallClock()
        if adapter is None:
            adapter = self._make_adapter(p, binding, fees)
        if "ai_provider" in self.overrides:
            ai = self.overrides["ai_provider"]()
        elif not provider.is_realtime and not st.ai_in_replay:
            from ..ai.providers import FixtureProvider

            ai = FixtureProvider()  # 加速回放不调用付费模型
        else:
            ai = get_provider()
        om = OrderManager(self.ns, adapter, fees, clock.now)
        rt = Runtime(ns=self.ns, portfolio_id=pid, mode=mode, provider=provider, clock=clock, adapter=adapter, om=om,
                     calendar=self.calendar, fee_schedules=fees, data_source=data_source,
                     account_id=binding.acc_id if binding else "", ai_provider=ai)
        self._register_instruments(rt)
        self._sync_sim_funding(rt)
        return rt

    def _register_instruments(self, rt: Runtime) -> None:
        """把行情源已核实的证券登记到本地模拟券商。实时行情的证券是运行中陆续核实的，因此每轮都检查新增。"""
        if not hasattr(rt.adapter, "register_instrument"):
            return
        for inst in rt.provider.instruments():
            if inst.symbol not in rt.adapter_instruments:
                rt.adapter.register_instrument(inst.symbol, inst.market, inst.currency, inst.lot_size)
                rt.adapter_instruments.add(inst.symbol)

    def _make_provider(self, p: Portfolio):
        from ..market_data.replay import ReplayProvider

        if p.data_source == "DEMO_FIXTURE":
            return ReplayProvider(fixture_dir(), self.calendar, name="DEMO_FIXTURE", prefix="demo")
        if p.data_source == "HISTORICAL_REPLAY":
            return ReplayProvider(get_settings().data_dir / "imports", self.calendar, name="HISTORICAL_REPLAY",
                                  prefix="import")
        if p.data_source == "MOOMOO_REALTIME":
            from ..db.models import Instrument
            from ..market_data.moomoo_quotes import MoomooQuoteProvider

            prov = MoomooQuoteProvider.shared()
            with read_session(self.ns) as s:
                # 只登记真实证券（演示用的虚构证券不进入实时模式），类型与每手股数一律向券商核实，不沿用本地记录
                syms = [i.symbol for i in s.execute(select(Instrument).where(Instrument.is_demo.is_(False))).scalars()]
            prov.ensure_instruments(syms)
            return prov
        raise RuntimeError(f"未知数据源 {p.data_source}")

    def _make_adapter(self, p: Portfolio, binding: AccountBinding | None, fees: dict):
        mode = Mode(p.mode)
        if mode in (Mode.DEMO, Mode.LOCAL_PAPER):
            from ..simulation.local_sim import LocalSimBroker

            path = get_settings().data_dir / "local_sim" / f"{self.ns}_portfolio_{p.id}.json"
            return LocalSimBroker(path, acc_id=binding.acc_id if binding else f"LOCAL-{p.id}", fee_schedules=fees,
                                  calendar=self.calendar)
        if mode == Mode.BROKER_PAPER:
            from ..broker.moomoo_adapter import MoomooExecutionAdapter

            return MoomooExecutionAdapter.for_paper(binding)
        # 实盘：必须通过实盘闸门，取得一次性的执行许可
        from ..broker.gate import live_gate
        from ..broker.moomoo_adapter import MoomooExecutionAdapter

        with read_session(self.ns) as s:
            gate = live_gate(s, p.id)
        if not gate.passed:
            raise RuntimeError("实盘闸门未通过：" + "；".join(gate.failures))
        return MoomooExecutionAdapter.for_live(binding, gate.token)

    def _sync_sim_funding(self, rt: Runtime) -> None:
        """本地模拟券商的「入金」= 系统账本中的分配资金（仅模拟）。"""
        if not hasattr(rt.adapter, "fund"):
            return
        with read_session(self.ns) as s:
            for ccy in ("MYR", "USD"):
                total = ledger.total_flows(s, rt.portfolio_id, ccy)
                funded = D(rt.adapter.state.setdefault("funded", {}).get(ccy, "0"))
                if total != funded:
                    rt.adapter.fund(ccy, total - funded)
                    rt.adapter.state["funded"][ccy] = str(total)
                    rt.adapter._save()

    def runtime(self, pid: int) -> Runtime:
        if pid not in self.runtimes:
            self.runtimes[pid] = self.build_runtime(pid)
        return self.runtimes[pid]

    # ================================================================ 主循环
    def tick(self) -> dict:
        wall = utcnow()
        self.process_commands(wall)
        with read_session(self.ns) as s:
            pids = list(s.execute(select(Portfolio.id).where(Portfolio.running.is_(True))).scalars())
        status: dict[str, Any] = {"portfolios": {}}
        for pid in pids:
            try:
                status["portfolios"][pid] = self.tick_portfolio(pid, wall)
            except Exception as e:  # 单个账本故障不影响其它账本；记录并告警
                log.exception("tick failed")
                with write_session(self.ns) as s:
                    audit(s, "SYSTEM", "TICK_ERROR", f"处理账本 {pid} 时出错：{type(e).__name__}: {e}", actor="worker",
                          portfolio_id=pid)
                    notify(s, "error", "工作进程处理出错", f"{type(e).__name__}: {e}", dedupe_key=f"tickerr:{pid}:{type(e).__name__}:{wall:%Y%m%d%H}")
                status["portfolios"][pid] = {"error": str(e)}
        for pid in list(self.runtimes):
            if pid not in pids:
                self.runtimes.pop(pid).executor.shutdown(wait=False)
        self.last_status = status
        return status

    def tick_portfolio(self, pid: int, wall: datetime) -> dict:
        rt = self.runtime(pid)
        rt.reset_tick()
        now = rt.now()
        self._register_instruments(rt)
        # 1. 撮合与推送
        rt.adapter.on_market_tick(now, rt.provider)
        rt.om.process_events()
        # 2. 对账
        with read_session(self.ns) as s:
            params = self._risk_params(s, pid)
        interval = timedelta(minutes=int(params.get("reconcile_interval_minutes", 5)))
        recon = None
        if not rt.started or rt.last_reconcile_wall is None or wall - rt.last_reconcile_wall >= interval:
            recon = rt.om.reconcile(pid, lambda s: self._managed_symbols(s, pid), startup=not rt.started)
            rt.last_reconcile_wall = wall
            if not rt.started:
                with write_session(self.ns) as s:
                    audit(s, "SYSTEM", "STARTUP_RECONCILE", f"启动对账完成：{recon}", actor="worker", portfolio_id=pid,
                          as_of=now)
            rt.started = True
        # 3. 公司行动
        self._corporate_actions(rt)
        # 4. 本次状态（先在事务外按需更新股票池）
        self._refresh_pool(rt)
        ts = self._tick_state(rt, wall)
        if recon and recon.get("error"):
            ts.health_blocks.append(recon["error"])
        # 5. 净值与锁定
        self._equity_and_locks(rt, ts)
        # 6. 保护性退出（无论授权、暂停、AI 状态），并立即提交，不受后面步骤出错影响。
        #    财报日历在写事务之外先取好（每个美东日期一次），避免在写锁内访问 OpenD
        mctx.earnings(rt)
        self._protections(rt, ts)
        self._submit_created(rt)
        # 7. 订单维护
        self._maintain_orders(rt, ts)
        # 8. 策略（出错只影响本轮新决策，不影响保护与提交）
        try:
            if ts.auth_ok and ts.strategy is not None:
                if ts.strategy.key == "AI_AUTONOMOUS":
                    ai_autonomous.run(rt, ts, wall)
                elif ts.strategy.key == "BASELINE_MA":
                    baseline_ma.run(rt, ts, wall)
            else:
                ai_autonomous.process_finished(rt, ts, wall)
        except Exception as e:
            log.exception("策略步骤出错")
            with write_session(self.ns) as s:
                audit(s, "STRATEGY", "STEP_ERROR", f"策略步骤出错，本轮不产生新决策：{type(e).__name__}: {e}",
                      actor="worker", portfolio_id=pid)
                notify(s, "error", "策略步骤出错", f"{type(e).__name__}: {e}"[:500],
                       dedupe_key=f"strategyerr:{pid}:{type(e).__name__}:{wall:%Y%m%d%H}")
        # 9. 提交
        self._submit_created(rt)
        rt.om.process_events()
        # 10. 复盘任务
        from ..learning import scheduler as learning_scheduler

        learning_scheduler.run_due(rt, ts)
        # 11. 报价快照 + 时钟
        self._store_quotes(rt, ts)
        out = self._status(rt, ts)
        if rt.is_replay:
            self._advance_replay(rt)
        return out

    # ================================================================ 子步骤
    @staticmethod
    def _risk_params(s, pid: int) -> dict:
        auth = TradingEngine._active_auth(s, pid)
        if auth is not None:
            rp = s.get(RiskPolicyVersion, auth.risk_policy_version_id)
            if rp is not None:
                return rp.params
        rp = s.execute(select(RiskPolicyVersion).where(RiskPolicyVersion.confirmed_by_user.is_(True))
                       .order_by(RiskPolicyVersion.version.desc()).limit(1)).scalar_one_or_none()
        from ..risk.policy import SEED_RISK_PARAMS

        return rp.params if rp else dict(SEED_RISK_PARAMS)

    @staticmethod
    def _active_auth(s, pid: int) -> Authorization | None:
        return s.execute(select(Authorization).where(Authorization.portfolio_id == pid)
                         .order_by(Authorization.id.desc()).limit(1)).scalar_one_or_none()

    def _managed_symbols(self, s, pid: int) -> set[str]:
        syms = {p.symbol for p in ledger.open_positions(s, pid)}
        syms |= set(s.execute(select(OrderIntent.symbol).where(OrderIntent.portfolio_id == pid)).scalars())
        return syms

    def _tick_state(self, rt: Runtime, wall: datetime) -> TickState:
        with write_session(self.ns) as s:
            p = s.get(Portfolio, rt.portfolio_id)
            auth = self._active_auth(s, rt.portfolio_id)
            ok, why = authz.check(auth, wall, p.mode)
            if auth is not None and auth.status == "ACTIVE" and wall >= auth.expires_at:
                auth.status = "EXPIRED"
                audit(s, "AUTH", "EXPIRED", "运行授权已到期：停止新开仓，保护性退出继续运行", actor="worker",
                      portfolio_id=p.id, as_of=rt.now())
                notify(s, "warning", "运行授权已到期", "系统已停止新开仓；已有持仓按批准的保护规则管理。",
                       dedupe_key=f"authexp:{auth.id}")
            params = self._risk_params(s, p.id)
            rpv = s.get(RiskPolicyVersion, auth.risk_policy_version_id).version if auth else 0
            strategy = None
            if auth and auth.strategy_version_ids:
                strategy = s.get(StrategyVersion, auth.strategy_version_ids[0])
                active = s.execute(select(StrategyVersion).where(StrategyVersion.key == strategy.key,
                                                                 StrategyVersion.status == "ACTIVE")).scalar_one_or_none()
                # 用户批准的策略升级只影响新决策：授权里的版本被替换时使用当前 ACTIVE 版本
                if active is not None and active.id != strategy.id and active.approved_by_user:
                    strategy = active
            scope = self._scope(s, rt, auth)
            p_copy = Portfolio(id=p.id, mode=p.mode, account_binding_id=p.account_binding_id, name=p.name,
                               pause_new_entries=p.pause_new_entries, emergency_stop=p.emergency_stop,
                               data_source=p.data_source, running=p.running)
            auth_copy = None
            if auth is not None:
                s.expunge(auth)
                auth_copy = auth
            ts = TickState(portfolio=p_copy, auth=auth_copy, auth_ok=ok, auth_reason=why, params=params,
                           risk_policy_version=rpv, strategy=strategy, scope=scope,
                           risk_policy_version_id=auth.risk_policy_version_id if auth else 0)
            if strategy is not None:
                s.expunge(strategy)
                if auth_copy is not None and strategy.id not in (auth_copy.strategy_version_ids or []):
                    auth_copy.strategy_version_ids = list(auth_copy.strategy_version_ids or []) + [strategy.id]
        # 健康状态
        try:
            pst = rt.provider.status()
            if not pst.connected:
                ts.health_blocks.append(f"行情未连接：{pst.detail}")
        except Exception as e:
            ts.health_blocks.append(f"行情状态异常：{e}")
        h = rt.adapter.health()
        if not h.connected:
            ts.health_blocks.append(f"执行通道未连接：{h.detail}")
        if rt.mode.is_live or rt.mode == Mode.BROKER_PAPER:
            try:
                ts.broker_cash = {c.currency: c for c in rt.adapter.cash()}
            except Exception as e:
                ts.health_blocks.append(f"读取券商现金失败：{e}")
        return ts

    def _scope(self, s, rt: Runtime, auth: Authorization | None) -> set[str]:
        rt.pool_info = {}
        if auth is None:
            return set()
        sc = auth.scope or {}
        if sc.get("type") == "WHITELIST":
            return set(sc.get("symbols", []))
        if sc.get("type") == "POOL":
            # 股票池在 _refresh_pool 中（事务外）更新；这里只读取最新快照
            rule = s.get(UniverseRule, sc.get("universe_rule_id"))
            if rule is None or not rule.confirmed_by_user:
                return set()
            snap = s.execute(select(UniverseSnapshot).where(UniverseSnapshot.rule_id == rule.id)
                             .order_by(UniverseSnapshot.id.desc()).limit(1)).scalar_one_or_none()
            if snap is None:
                return set()
            rt.pool_info = {m["symbol"]: m for m in snap.members}
            return {m["symbol"] for m in snap.members}
        return set()

    def _refresh_pool(self, rt: Runtime) -> None:
        """需要时更新授权股票池。筛选（可能有网络请求）在任何数据库事务之外进行，避免长时间占用写锁。"""
        now = rt.now()
        with read_session(self.ns) as s:
            auth = self._active_auth(s, rt.portfolio_id)
            sc = (auth.scope or {}) if auth is not None and auth.status == "ACTIVE" else {}
            if sc.get("type") != "POOL":
                return
            rule = s.get(UniverseRule, sc.get("universe_rule_id"))
            if rule is None or not rule.confirmed_by_user:
                return
            rule_id, rules, version = rule.id, dict(rule.rules), rule.version
            last = s.execute(select(UniverseSnapshot.as_of).where(UniverseSnapshot.rule_id == rule_id)
                             .order_by(UniverseSnapshot.id.desc()).limit(1)).scalar_one_or_none()
        # 只做美股的规则按美股交易日更新（马来西亚午夜正处于美股交易时段，不能在盘中换池）
        mk = "US" if rules.get("markets") == ["US"] else "MY"
        day = rt.calendar.local_date(mk, now)
        if last is not None and rt.calendar.local_date(mk, last) == day:
            return
        retry = rt.pool_retry.get(rule_id)
        if retry is not None and now < retry:
            return
        try:
            members, excluded, stats = screen(rt.provider, rules, now)
        except Exception as e:
            # 失败时沿用上一份股票池（没有就为空，只是不开新仓；已有持仓照常保护），30 分钟后再试
            rt.pool_retry[rule_id] = now + timedelta(minutes=30)
            with write_session(self.ns) as s:
                audit(s, "UNIVERSE", "SNAPSHOT_FAILED", f"股票池更新失败，沿用上一份：{e}", actor="worker",
                      portfolio_id=rt.portfolio_id, as_of=now)
                notify(s, "warning", "股票池今日更新失败", f"已沿用上一份股票池，30 分钟后重试。原因：{e}"[:500],
                       dedupe_key=f"pool-fail:{rule_id}:{day}")
            return
        rt.pool_retry.pop(rule_id, None)
        with write_session(self.ns) as s:
            cur = s.execute(select(UniverseSnapshot.as_of).where(UniverseSnapshot.rule_id == rule_id)
                            .order_by(UniverseSnapshot.id.desc()).limit(1)).scalar_one_or_none()
            if cur is not None and rt.calendar.local_date(mk, cur) == day:
                return  # 期间已有其他进程写入当天快照
            s.add(UniverseSnapshot(rule_id=rule_id, as_of=now, members=members, excluded=excluded,
                                   data_source=rt.data_source, stats=stats))
            scanned = (f"扫描 {stats['matched_total']} 只符合条件的美股中成交额前 {stats['fetched']} 只，"
                       if "matched_total" in stats else "")
            audit(s, "UNIVERSE", "SNAPSHOT",
                  f"授权股票池例行更新：{scanned}{len(members)} 个成员（规则 v{version}，无需逐只确认）",
                  actor="worker", portfolio_id=rt.portfolio_id, as_of=now)

    def _corporate_actions(self, rt: Runtime) -> None:
        get_all = getattr(rt.provider, "all_corporate_actions", None)
        if get_all is None:
            return
        now = rt.now()
        with write_session(self.ns) as s:
            for a in get_all():
                inst = rt.provider.instrument(a.symbol)
                if inst is None:
                    continue
                open_t = self.calendar.session_open(inst.market, a.ex_date)
                if open_t is None or now < open_t:
                    continue
                key = f"corp:{rt.portfolio_id}:{a.symbol}:{a.ex_date}:{a.kind}"
                from ..db.models import AuditEvent

                if s.execute(select(AuditEvent.id).where(AuditEvent.action == "CORP_APPLIED",
                                                         AuditEvent.message == key)).first():
                    continue
                ledger.apply_corporate_action(s, rt.portfolio_id, rt.mode.value, a, now)
                audit(s, "LEDGER", "CORP_APPLIED", key, actor="worker", portfolio_id=rt.portfolio_id, as_of=now)

    def _equity_and_locks(self, rt: Runtime, ts: TickState) -> None:
        now = rt.now()
        with write_session(self.ns) as s:
            ccys = set((ts.auth.capital or {}).keys()) if ts.auth else set()
            ccys |= {p.currency for p in ledger.open_positions(s, rt.portfolio_id)}
            for ccy in sorted(ccys):
                ev = ledger.equity_view(s, rt.portfolio_id, ccy, rt.price_of)
                alloc = ledger.total_flows(s, rt.portfolio_id, ccy)
                mpl = D(ts.auth.max_planned_loss.get(ccy)) if ts.auth and ts.auth.max_planned_loss.get(ccy) else None
                locks.update_and_check(s, rt.portfolio_id, ccy, ev.equity, ts.params, now, allocation=alloc,
                                       max_planned_loss=mpl)
            health_ok = not ts.health_blocks and not s.execute(select(OrderIntent.id).where(
                OrderIntent.portfolio_id == rt.portfolio_id, OrderIntent.status == OrderStatus.UNKNOWN)).first()
            locks.try_release_daily(s, rt.portfolio_id, now, health_ok)

    @staticmethod
    def _earnings_exit(rt: Runtime, pos, now: datetime) -> str | None:
        """财报前退出属于附加规则：出错只记日志，不能影响止损等其他保护。"""
        try:
            return mctx.exit_before_earnings(rt, pos, now)
        except Exception:
            log.exception("财报前退出检查出错（%s），本轮跳过该规则", pos.symbol)
            return None

    def _protections(self, rt: Runtime, ts: TickState) -> None:
        now = rt.now()
        with write_session(self.ns) as s:
            rec_locked = {lk.scope for lk in locks.active_locks(s, rt.portfolio_id) if lk.kind == "RECONCILE"}
            for pos in ledger.open_positions(s, rt.portfolio_id):
                if pos.qty <= 0 or pos.symbol in rec_locked:
                    continue
                if not self.calendar.is_open(pos.market, now):
                    continue
                ref = rt.last_good.get(pos.symbol)
                q = rt.quote(pos.symbol)
                if q is None or q.suspended or q.bid is None or q.last is None:
                    self._warn_no_quote(s, rt, pos, now, suspended=bool(q is not None and q.suspended))
                    continue
                rt.no_quote_since.pop(pos.symbol, None)
                # 价格断层（拆股或数据错误）：比上一次有效价跌超过 45%，暂停该证券的自动止损，交人工确认
                ref = rt.prev_good.get(pos.symbol) if ref == q.last else ref
                ref = ref or pos.avg_cost
                if ref and q.last <= ref * Decimal("0.55"):
                    audit(s, "PROTECTION", "PRICE_GAP_HOLD",
                          f"{pos.symbol} 价格 {q.last} 比参考价 {ref} 低超过 45%，疑似拆股或数据错误：暂停自动止损，请人工确认",
                          actor="worker", portfolio_id=rt.portfolio_id, symbol=pos.symbol, as_of=now)
                    notify(s, "error", f"{pos.symbol} 价格异常，已暂停其自动止损",
                           f"最新价 {q.last}，参考价 {ref}。可能是拆股或行情错误，请在 moomoo 核对后处理。",
                           dedupe_key=f"pricegap:{rt.portfolio_id}:{pos.symbol}:{now:%Y%m%d}")
                    continue
                reason, why = None, ""
                if pos.stop_price is not None and (q.last <= pos.stop_price or q.bid <= pos.stop_price):
                    reason, why = IntentReason.EXIT_STOP, f"最新价 {q.last} 触及止损 {pos.stop_price}"
                elif pos.take_profit_price is not None and q.last >= pos.take_profit_price:
                    reason, why = IntentReason.EXIT_TAKE_PROFIT, f"最新价 {q.last} 达到止盈 {pos.take_profit_price}"
                elif pos.max_hold_until is not None and now >= pos.max_hold_until:
                    reason, why = IntentReason.EXIT_TIME, f"已到最长持有期限 {pos.max_hold_until.isoformat()}"
                elif ts.auth is not None and not ts.auth.overnight and \
                        (mtc := self.calendar.minutes_to_close(pos.market, now)) is not None and mtc <= 20:
                    reason, why = IntentReason.EXIT_TIME, "授权不隔夜：收市前退出"
                elif pos.market == "US" and (ew := self._earnings_exit(rt, pos, now)):
                    reason, why = IntentReason.EXIT_TIME, ew
                if reason is None:
                    continue
                # 保护优先：撤销同一证券的非保护性卖单
                for it in coordinator.non_protective_sells(s, pos):
                    if it.status in OPEN_SUBMITTED:
                        self._pending_cancels.append((it.id, "保护性退出优先，撤销其他卖单"))
                seq = s.execute(select(func.count()).select_from(OrderIntent).where(
                    OrderIntent.position_id == pos.id, OrderIntent.reason == reason)).scalar_one()
                inst = rt.provider.instrument(pos.symbol)
                it, msg = coordinator.request_exit(
                    s, pos=pos, reason=reason, bid=q.bid, params=ts.params, mode=rt.mode.value,
                    account_id=rt.account_id, auth_id=ts.auth.id if ts.auth else None, now=now,
                    lot=inst.lot_size if inst else 1, key_suffix=str(seq), strategy_version=pos.protection_version,
                )
                if it is not None:
                    d = record_decision(s, rt, ts, source="PROTECTION", symbol=pos.symbol,
                                    action=DecisionAction.SELL_PROPOSAL, outcome=DecisionOutcome.AUTO_EXECUTED,
                                    reason=f"程序保护规则：{why}；卖出 {it.qty} @≥{it.limit_price}", as_of=now,
                                    facts={"last": str(q.last), "bid": str(q.bid)},
                                    rule_calc={"protection": reason, "stop": str(pos.stop_price),
                                               "take_profit": str(pos.take_profit_price)},
                                    market=pos.market, currency=pos.currency)
                    it.decision_id = d.id
        self._flush_cancels(rt)

    def _warn_no_quote(self, s, rt: Runtime, pos, now: datetime, *, suspended: bool) -> None:
        """开盘时持仓拿不到报价：止损无法执行。超过 30 秒发通知（每只每天一次）。"""
        if suspended:
            return
        wall = utcnow()
        since = rt.no_quote_since.setdefault(pos.symbol, wall)
        if (wall - since).total_seconds() < 30 and not rt.is_replay:
            return
        audit(s, "PROTECTION", "NO_QUOTE", f"{pos.symbol} 开盘中没有可用报价，止损暂时无法执行", actor="worker",
              portfolio_id=rt.portfolio_id, symbol=pos.symbol, as_of=now)
        notify(s, "error", f"{pos.symbol} 没有报价，止损暂时无法执行",
               "可能是 OpenD 断线、行情权限或请求限速。请检查 OpenD 是否登录；恢复报价后止损会自动继续。",
               dedupe_key=f"noquote:{rt.portfolio_id}:{pos.symbol}:{now:%Y%m%d}")

    def _flush_cancels(self, rt: Runtime) -> None:
        pend, self._pending_cancels = list(self._pending_cancels), []
        for iid, why in pend:
            rt.om.cancel(iid, why)

    def _maintain_orders(self, rt: Runtime, ts: TickState) -> None:
        now = rt.now()
        cancels = []
        with read_session(self.ns) as s:
            for it in s.execute(select(OrderIntent).where(OrderIntent.portfolio_id == rt.portfolio_id,
                                                          OrderIntent.status.in_(OPEN_SUBMITTED))).scalars():
                if it.expires_at is not None and now >= it.expires_at:
                    if it.side == Side.BUY:
                        cancels.append((it.id, "开仓委托有效期已到，撤销未成交部分"))
                    else:
                        cancels.append((it.id, "退出委托未在时限内成交，撤单后按最新价重新委托"))
                elif ts.portfolio.emergency_stop and it.side == Side.BUY:
                    cancels.append((it.id, "紧急停止：撤销系统挂起的入场单"))
            # 授权失效后，取消尚未成交的入场单
            if not ts.auth_ok:
                for it in s.execute(select(OrderIntent).where(OrderIntent.portfolio_id == rt.portfolio_id,
                                                              OrderIntent.side == Side.BUY,
                                                              OrderIntent.status.in_(OPEN_SUBMITTED))).scalars():
                    cancels.append((it.id, f"运行授权无效（{ts.auth_reason}），撤销入场单"))
        for iid, why in dict(cancels).items():
            rt.om.cancel(iid, why)
        # AWAITING_APPROVAL 过期
        with write_session(self.ns) as s:
            for it in s.execute(select(OrderIntent).where(OrderIntent.portfolio_id == rt.portfolio_id,
                                                          OrderIntent.status == OrderStatus.AWAITING_APPROVAL)).scalars():
                if it.expires_at is not None and now >= it.expires_at:
                    from ..orders.manager import _event
                    from ..risk.engine import release_reservation

                    _event(s, it, "APPROVAL_EXPIRED", OrderStatus.CANCELLED, "等待确认超时，未提交")
                    release_reservation(s, it.id, "approval expired")

    def _submit_created(self, rt: Runtime) -> None:
        with read_session(self.ns) as s:
            ids = list(s.execute(select(OrderIntent.id).where(OrderIntent.portfolio_id == rt.portfolio_id,
                                                              OrderIntent.status == OrderStatus.CREATED)
                                 .order_by(OrderIntent.side.desc(), OrderIntent.created_at_utc)).scalars())
        for iid in ids:  # 先卖后买（SELL > BUY 字母序降序）
            rt.om.submit(iid)

    def _store_quotes(self, rt: Runtime, ts: TickState) -> None:
        wall = utcnow()
        if rt.last_quote_store and (wall - rt.last_quote_store).total_seconds() < 5 and not rt.is_replay:
            return
        rt.last_quote_store = wall
        with write_session(self.ns) as s:
            syms = set(ts.scope) | {p.symbol for p in ledger.open_positions(s, rt.portfolio_id)}
            for sym in syms:
                q = rt.quote(sym)
                if q is None:
                    continue
                row = s.execute(select(QuoteSnapshot).where(QuoteSnapshot.symbol == sym)).scalar_one_or_none()
                if row is None:
                    row = QuoteSnapshot(symbol=sym, source=q.source)
                    s.add(row)
                row.last, row.bid, row.ask, row.volume = q.last, q.bid, q.ask, q.volume
                row.source, row.is_realtime, row.suspended = q.source, q.is_realtime, q.suspended
                row.source_ts, row.recv_ts = q.source_ts, q.recv_ts

    def _status(self, rt: Runtime, ts: TickState) -> dict:
        try:
            pst = rt.provider.status()
            ds = {"name": pst.name, "label": pst.label, "connected": pst.connected, "realtime": pst.is_realtime}
        except Exception as e:
            ds = {"name": "?", "label": f"异常：{e}", "connected": False, "realtime": False}
        return {
            "mode": rt.mode.value, "business_time": rt.now().isoformat(), "is_replay": rt.is_replay,
            "data_source": ds, "adapter": {"broker": rt.adapter.broker, "trd_env": rt.adapter.trd_env,
                                           "health": rt.adapter.health().detail},
            "ai": {"label": rt.ai_provider.label, "is_fixture": rt.ai_provider.is_fixture,
                   "pending_jobs": len(rt.ai_jobs)},
            "auth_ok": ts.auth_ok, "auth_reason": ts.auth_reason, "health_blocks": ts.health_blocks,
            "scope_size": len(ts.scope),
            "broker_total_assets": (getattr(rt.adapter, "last_accinfo", {}) or {}).get("total_assets"),
        }

    def _advance_replay(self, rt: Runtime) -> None:
        end = getattr(rt.provider, "data_end", lambda: None)()
        t = rt.clock.now()
        if end is not None and t.date() > end:
            with write_session(self.ns) as s:
                p = s.get(Portfolio, rt.portfolio_id)
                if p.running:
                    p.running = False
                    audit(s, "SYSTEM", "REPLAY_END", "回放数据已到末尾，模拟运行结束", actor="worker", portfolio_id=p.id,
                          as_of=t)
                    notify(s, "info", "演示/回放已结束", "回放数据已用完，可在「模拟与复盘」查看结果。",
                           dedupe_key=f"replayend:{p.id}")
            return
        rt.clock.advance()
        with write_session(self.ns) as s:
            s.get(Portfolio, rt.portfolio_id).sim_clock = rt.clock.now()

    # ================================================================ 指令
    def process_commands(self, wall: datetime) -> None:
        with read_session(self.ns) as s:
            cmds = list(s.execute(select(Command).where(Command.status == "PENDING").order_by(Command.id)).scalars())
        for c in cmds:
            try:
                result = self._run_command(c)
                status = "DONE"
            except Exception as e:
                result, status = f"{type(e).__name__}: {e}", "FAILED"
            with write_session(self.ns) as s:
                row = s.get(Command, c.id)
                row.status, row.result, row.processed_at = status, result, wall

    def _run_command(self, c: Command) -> str:
        pid = c.payload.get("portfolio_id")
        if c.type == "EMERGENCY_STOP":
            rt = self.runtime(pid)
            n = 0
            with read_session(self.ns) as s:
                ids = list(s.execute(select(OrderIntent.id).where(
                    OrderIntent.portfolio_id == pid, OrderIntent.side == Side.BUY,
                    OrderIntent.status.in_([*OPEN_SUBMITTED, OrderStatus.CREATED, OrderStatus.AWAITING_APPROVAL]))).scalars())
            results = []
            for iid in ids:
                st = rt.om.cancel(iid, "紧急停止：撤销系统挂起的入场单")
                results.append(f"{iid[:8]}→{st}")
                n += 1
            return f"已对 {n} 个入场单发出撤单（逐单结果：{', '.join(results) or '无'}）；保护性订单与持仓保留"
        if c.type == "CANCEL_ORDER":
            rt = self.runtime(pid)
            return rt.om.cancel(c.payload["intent_id"], "用户手动撤单")
        if c.type == "RECONCILE_NOW":
            rt = self.runtime(pid)
            return str(rt.om.reconcile(pid, lambda s: self._managed_symbols(s, pid)))
        if c.type == "LIQUIDATE":
            rt = self.runtime(pid)
            rt.reset_tick()
            out = []
            with write_session(self.ns) as s:
                params = self._risk_params(s, pid)
                for pos in ledger.open_positions(s, pid):
                    if c.payload.get("symbols") and pos.symbol not in c.payload["symbols"]:
                        continue
                    for it in coordinator.non_protective_sells(s, pos):
                        if it.status in OPEN_SUBMITTED:
                            self._pending_cancels.append((it.id, "手动清仓：撤销其他卖单"))
                    q = rt.quote(pos.symbol)
                    inst = rt.provider.instrument(pos.symbol)
                    it, msg = coordinator.request_exit(
                        s, pos=pos, reason=IntentReason.MANUAL_LIQUIDATE, bid=q.bid if q else None, params=params,
                        mode=rt.mode.value, account_id=rt.account_id, auth_id=None, now=rt.now(),
                        lot=inst.lot_size if inst else 1, key_suffix=f"cmd{c.id}")
                    out.append(f"{pos.symbol}: {'卖出 ' + str(it.qty) if it else msg}")
            self._flush_cancels(rt)
            self._submit_created(rt)
            return "；".join(out) or "没有系统管理的持仓"
        raise ValueError(f"未知指令 {c.type}")

    # ================================================================ 心跳
    def heartbeat(self, owner: str, pid_os: int) -> None:
        now = utcnow()
        with write_session(self.ns) as s:
            hb = s.get(WorkerHeartbeat, self.ns)
            if hb is None:
                hb = WorkerHeartbeat(name=self.ns, owner=owner, pid=pid_os, started_at=self.started_at, heartbeat_at=now)
                s.add(hb)
            hb.owner, hb.pid, hb.heartbeat_at = owner, pid_os, now
            hb.status = {"portfolios": {str(k): v for k, v in (self.last_status.get("portfolios") or {}).items()}}
