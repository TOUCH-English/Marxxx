"""模型供应商适配器。

- AnthropicProvider：调用 Claude Messages API，要求 JSON Schema 结构化输出；不提供任何工具。
- FixtureProvider：密钥缺失时使用的「固定测试响应」——用确定性规则生成与 AI 相同格式的输出，
  只用于验证自动交易流程，不代表任何 AI 投资表现，界面会明确标示。
- DisabledProvider：AI 关闭；不会产生新的 AI 订单，已有保护规则照常运行。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

from ..config import get_settings
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_user_payload
from .schema import OUTPUT_JSON_SCHEMA


@dataclass
class AIResult:
    status: str  # OK / TIMEOUT / ERROR / REFUSAL / DISABLED
    text: str = ""
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    error: str = ""


class DisabledProvider:
    name = "disabled"
    model = "-"
    label = "AI 已关闭"
    is_fixture = False
    prompt_version = PROMPT_VERSION

    def complete(self, payload: dict[str, Any]) -> AIResult:
        return AIResult("DISABLED", provider=self.name, model=self.model, error="AI 已关闭或预算耗尽")


class AnthropicProvider:
    name = "anthropic"
    is_fixture = False
    prompt_version = PROMPT_VERSION

    def __init__(self, api_key: str, model: str, timeout_s: float, max_retries: int):
        import anthropic

        self._anthropic = anthropic
        self.model = model
        self.label = f"Claude（{model}）"
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=max_retries)

    def complete(self, payload: dict[str, Any]) -> AIResult:
        a = self._anthropic
        t0 = time.monotonic()
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_user_payload(payload)}],
                output_config={"effort": "medium",
                               "format": {"type": "json_schema", "schema": OUTPUT_JSON_SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except a.APITimeoutError as e:
            return AIResult("TIMEOUT", provider=self.name, model=self.model, error=str(e),
                            latency_ms=int((time.monotonic() - t0) * 1000))
        except a.AuthenticationError:
            return AIResult("ERROR", provider=self.name, model=self.model, error="AI 密钥无效（AuthenticationError）")
        except a.RateLimitError as e:
            return AIResult("ERROR", provider=self.name, model=self.model, error=f"限流：{e}")
        except a.APIStatusError as e:
            return AIResult("ERROR", provider=self.name, model=self.model, error=f"API 错误 {e.status_code}")
        except a.APIConnectionError as e:
            return AIResult("ERROR", provider=self.name, model=self.model, error=f"网络错误：{e}")
        latency = int((time.monotonic() - t0) * 1000)
        usage = getattr(resp, "usage", None)
        it, ot = (getattr(usage, "input_tokens", 0) or 0), (getattr(usage, "output_tokens", 0) or 0)
        if resp.stop_reason == "refusal":
            return AIResult("REFUSAL", provider=self.name, model=resp.model, input_tokens=it, output_tokens=ot,
                            latency_ms=latency, error="模型拒绝回答（refusal）")
        if resp.stop_reason == "max_tokens":
            return AIResult("ERROR", provider=self.name, model=resp.model, input_tokens=it, output_tokens=ot,
                            latency_ms=latency, error="输出被截断（max_tokens）")
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        return AIResult("OK", text=text, provider=self.name, model=resp.model, input_tokens=it, output_tokens=ot,
                        latency_ms=latency)


def _q(x: Decimal, places: str = "0.0001") -> str:
    return str(x.quantize(Decimal(places), rounding=ROUND_DOWN))


class FixtureProvider:
    """固定测试响应：确定性规则模拟 AI 输出格式。不是 AI，不代表投资表现。"""

    name = "fixture"
    model = "fixture-rules-v1"
    label = "固定测试响应（非真实 AI）"
    is_fixture = True
    prompt_version = PROMPT_VERSION

    def complete(self, payload: dict[str, Any]) -> AIResult:
        p = payload.get("policy", {}).get("fixture_params", {})
        min_score = float(p.get("entry_min_score", 1.5))
        stop_mult = Decimal(str(p.get("stop_atr_mult", "2.0")))
        tp_mult = Decimal(str(p.get("take_profit_atr_mult", "4.0")))
        hold_days = int(p.get("max_holding_days", 10))
        tgt = str(p.get("target_position_pct", "0.08"))
        reduce_gain = Decimal(str(p.get("reduce_gain_atr", "1.5")))
        reduce_frac = str(p.get("reduce_fraction", "0.5"))
        exit_ret5 = float(p.get("exit_ret_5d_below", -0.04))
        max_new = int(p.get("max_new_per_call", 1))
        decisions = []
        ranked = []
        for c in payload.get("candidates", []):
            f = c.get("features", {})
            ev = c.get("evidence_ids", [])
            last = c.get("quote", {}).get("last")
            atr = f.get("atr_14")
            score = c.get("prescore", 0.0)
            ok = (last is not None and atr and (f.get("ret_20d") or 0) > 0 and f.get("sma_20")
                  and float(last) > f["sma_20"] and score >= min_score)
            if ok:
                ranked.append((score, c))
            else:
                why = []
                if (f.get("ret_20d") or 0) <= 0:
                    why.append("20 日动量不为正")
                if f.get("sma_20") and last is not None and float(last) <= f["sma_20"]:
                    why.append("价格未站上 20 日均线")
                if score < min_score:
                    why.append(f"综合动量分 {score:.2f} 低于门槛 {min_score}")
                decisions.append(self._no_trade(c["symbol"], ev, "；".join(why) or "数据不足"))
        ranked.sort(key=lambda x: -x[0])
        for i, (score, c) in enumerate(ranked):
            f = c["features"]
            if i >= max_new:
                decisions.append(self._no_trade(c["symbol"], c.get("evidence_ids", []),
                                                f"满足条件但本轮只新开 {max_new} 个，排名靠后"))
                continue
            last = Decimal(c["quote"]["last"])
            atr = Decimal(str(f["atr_14"]))
            decisions.append({
                "symbol": c["symbol"], "action": "BUY_PROPOSAL",
                "rationale": f"【固定测试响应】20 日收益 {f.get('ret_20d')}，价格高于 20 日均线，综合动量分 {score:.2f}。"
                             f"止损设在 {stop_mult} 倍 ATR 下方，持有不超过 {hold_days} 天。",
                "evidence_ids": c.get("evidence_ids", [])[:3], "risk_flags": ["固定测试响应", "无新闻源"],
                "entry_type": "IMMEDIATE", "entry_price": None, "max_buy_price": None,
                "target_position_pct": tgt, "stop_loss_price": _q(last - stop_mult * atr),
                "take_profit_price": _q(last + tp_mult * atr), "max_holding_days": hold_days,
                "sell_fraction": None, "min_sell_price": None, "valid_minutes": 30,
            })
        for pos in payload.get("positions", []):
            f = pos.get("features", {})
            ev = pos.get("evidence_ids", [])
            last = pos.get("last")
            atr = f.get("atr_14")
            gain_atr = None
            if last is not None and atr:
                gain_atr = (Decimal(last) - Decimal(pos["avg_cost"])) / Decimal(str(atr))
            if gain_atr is not None and gain_atr >= reduce_gain and not pos.get("reduced_before"):
                decisions.append(self._sell(pos["symbol"], ev, reduce_frac,
                                            f"【固定测试响应】浮盈约 {gain_atr:.1f} 倍 ATR，先减仓锁定部分利润。"))
            elif (f.get("ret_5d") is not None and f["ret_5d"] <= exit_ret5) or (
                    last is not None and f.get("sma_20") and float(last) < f["sma_20"] * 0.99):
                decisions.append(self._sell(pos["symbol"], ev, "1",
                                            "【固定测试响应】短期动量转弱（5 日收益或跌破 20 日均线），全部退出。"))
            else:
                decisions.append({**self._no_trade(pos["symbol"], ev, "【固定测试响应】趋势未破坏，继续持有。"),
                                  "action": "HOLD"})
        out = {"as_of_utc": payload.get("as_of_utc"), "strategy_version": payload.get("strategy_version"),
               "market_view": "【固定测试响应】按确定性规则生成，用于验证自动流程，不代表 AI 判断。",
               "decisions": decisions}
        return AIResult("OK", text=json.dumps(out, ensure_ascii=False), provider=self.name, model=self.model)

    @staticmethod
    def _no_trade(sym: str, ev: list, why: str) -> dict:
        return {"symbol": sym, "action": "NO_TRADE", "rationale": why, "evidence_ids": ev[:1], "risk_flags": [],
                "entry_type": "NONE", "entry_price": None, "max_buy_price": None, "target_position_pct": None,
                "stop_loss_price": None, "take_profit_price": None, "max_holding_days": None,
                "sell_fraction": None, "min_sell_price": None, "valid_minutes": 30}

    @staticmethod
    def _sell(sym: str, ev: list, frac: str, why: str) -> dict:
        return {"symbol": sym, "action": "SELL_PROPOSAL", "rationale": why, "evidence_ids": ev[:3],
                "risk_flags": ["固定测试响应"], "entry_type": "NONE", "entry_price": None, "max_buy_price": None,
                "target_position_pct": None, "stop_loss_price": None, "take_profit_price": None,
                "max_holding_days": None, "sell_fraction": frac, "min_sell_price": None, "valid_minutes": 30}


def get_provider(override: str | None = None):
    s = get_settings()
    choice = (override or s.ai_provider or "auto").lower()
    if choice == "auto":
        choice = "anthropic" if s.anthropic_api_key else "fixture"
    if choice == "anthropic":
        if not s.anthropic_api_key:
            return DisabledProvider()
        return AnthropicProvider(s.anthropic_api_key, s.ai_model, s.ai_timeout_s, s.ai_max_retries)
    if choice == "fixture":
        return FixtureProvider()
    return DisabledProvider()
