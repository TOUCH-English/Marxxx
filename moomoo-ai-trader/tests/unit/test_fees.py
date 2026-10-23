"""费用计算：向上取整到分、每单固定费、按金额分档的印花税（马币计、折美元）、费用项校验。

费率取自 Codex 在用户 Mac 上读到的 moomoo 马来西亚美股收费页（2026-10-23），汇率为 BNM 当日 4.0840。
"""
from decimal import Decimal

from app.core.money import calc_fees, validate_fee_items

US_OFFICIAL = {"items": [
    {"name": "佣金", "rate": "0.0003", "rounding": "UP", "side": "BOTH"},
    {"name": "平台费", "fixed": "0.99", "side": "BOTH"},
    {"name": "结算费", "per_share": "0.003", "max_rate_of_value": "0.01", "rounding": "UP", "side": "BOTH"},
    {"name": "SEC 费", "rate": "0.0000206", "min": "0.01", "side": "SELL"},
    {"name": "CAT 费", "per_share": "0.000003", "rounding": "UP", "side": "BOTH"},
    {"name": "印花税", "block_fee": "1", "block_value": "1000", "block_currency": "MYR", "fx_rate": "4.0840",
     "block_max": "1000", "rounding": "UP", "side": "BOTH"},
]}


def test_buy_one_aapl_share():
    # 348.22 USD：佣金 0.104466→0.11；平台 0.99；结算 0.003→0.01；CAT→0.01；
    # 印花税 348.22×4.084=1422.13 MYR → 2 档 → 2 MYR → 0.4897→0.49
    assert calc_fees(US_OFFICIAL, "BUY", 1, Decimal("348.22")) == Decimal("1.61")


def test_sell_adds_sec_fee_with_minimum():
    assert calc_fees(US_OFFICIAL, "SELL", 1, Decimal("348.22")) == Decimal("1.62")


def test_stamp_duty_blocks_and_cap_in_myr():
    sched = {"items": [US_OFFICIAL["items"][-1]]}
    # 正好 1000 MYR → 1 档；多 1 分 → 2 档
    assert calc_fees(sched, "BUY", 1, Decimal("1000") / Decimal("4.0840")) == Decimal("0.25")
    assert calc_fees(sched, "BUY", 1, Decimal("244.87")) == Decimal("0.49")
    # 上限 1000 MYR：超大成交额也只收 1000 MYR = 244.8580 USD → 向上取整 244.86
    assert calc_fees(sched, "BUY", 100000, Decimal("100")) == Decimal("244.86")


def test_round_up_vs_default_half_up():
    up = {"items": [{"name": "c", "rate": "0.0003", "rounding": "UP"}]}
    half = {"items": [{"name": "c", "rate": "0.0003"}]}
    assert calc_fees(up, "BUY", 1, Decimal("100")) == Decimal("0.03")
    assert calc_fees(up, "BUY", 1, Decimal("101")) == Decimal("0.04")  # 0.0303 → 0.04
    assert calc_fees(half, "BUY", 1, Decimal("101")) == Decimal("0.03")


def test_settlement_fee_capped_at_one_percent_of_value():
    sched = {"items": [US_OFFICIAL["items"][2]]}
    assert calc_fees(sched, "BUY", 1000, Decimal("0.20")) == Decimal("2.00")  # 3.00 被 1% 上限截到 2.00


def test_validation_rejects_ambiguous_or_incomplete_items():
    assert validate_fee_items(US_OFFICIAL["items"]) == []
    assert validate_fee_items([{"name": "x"}])  # 没有计费方式
    assert validate_fee_items([{"name": "x", "rate": "0.1", "fixed": "1"}])  # 两种
    assert validate_fee_items([{"name": "x", "block_fee": "1", "block_value": "1000"}])  # 缺汇率
    assert validate_fee_items([{"name": "x", "rate": "-0.1"}])
    assert validate_fee_items([{"name": "x", "rate": "0.1", "rounding": "DOWN"}])
    assert validate_fee_items([{"name": "x", "rate": "0.1", "side": "BOTHWAYS"}])
    assert validate_fee_items([{"name": "x", "fixed": "Infinity"}])  # 会让费用计算崩溃
    assert validate_fee_items([{"name": "x", "per_share": "1e40"}])
    assert validate_fee_items([{"name": "x", "rate": "2"}])  # 比例大于 1
