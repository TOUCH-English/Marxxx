"""验收 14：美股夏令时、跨午夜、马股午休/假日的固定样例；价格档位与费用。"""
from datetime import date, datetime, timezone
from decimal import Decimal

from app.core.calendar import TradingCalendar, myt_day, next_myt_midnight
from app.core.money import TEST_FEE_SCHEDULES, calc_fees, is_valid_tick, round_lot_down, round_to_tick, tick_size

cal = TradingCalendar()
U = timezone.utc


def test_us_dst_shift():
    # 2026-03-06（周五，冬令时）开盘 = 14:30 UTC；2026-03-09（周一，夏令时）开盘 = 13:30 UTC
    assert cal.session_open("US", date(2026, 3, 6)) == datetime(2026, 3, 6, 14, 30, tzinfo=U)
    assert cal.session_open("US", date(2026, 3, 9)) == datetime(2026, 3, 9, 13, 30, tzinfo=U)
    # 2026-11-02（周一，已回到冬令时）
    assert cal.session_open("US", date(2026, 11, 2)) == datetime(2026, 11, 2, 14, 30, tzinfo=U)


def test_us_half_day_and_holiday():
    assert not cal.is_session_day("US", date(2026, 11, 26))  # 感恩节
    assert cal.session_close("US", date(2026, 11, 27)) == datetime(2026, 11, 27, 18, 0, tzinfo=U)  # 半日市 13:00 ET


def test_my_lunch_break_and_holidays():
    d = date(2026, 10, 21)
    assert cal.is_open("MY", datetime(2026, 10, 21, 2, 0, tzinfo=U))      # 10:00 MYT
    assert not cal.is_open("MY", datetime(2026, 10, 21, 5, 0, tzinfo=U))  # 13:00 MYT 午休
    assert cal.market_status("MY", datetime(2026, 10, 21, 5, 0, tzinfo=U)) == "LUNCH_BREAK"
    assert cal.is_open("MY", datetime(2026, 10, 21, 7, 0, tzinfo=U))      # 15:00 MYT
    assert not cal.is_open("MY", datetime(2026, 10, 21, 8, 50, tzinfo=U))  # 16:50 MYT 收市竞价阶段
    assert not cal.is_session_day("MY", date(2026, 2, 17))  # 农历新年
    assert not cal.is_session_day("MY", date(2026, 10, 24))  # 周六
    assert len(cal.windows("MY", d)) == 2


def test_cross_midnight_day_boundary_is_myt():
    # 美股 2026-10-21 收盘 20:00 UTC = 吉隆坡 2026-10-22 04:00 → 日损益归 10-22（马来西亚日界）
    t = datetime(2026, 10, 21, 19, 59, tzinfo=U)
    assert myt_day(t) == "2026-10-22"
    assert myt_day(datetime(2026, 10, 21, 15, 59, tzinfo=U)) == "2026-10-21"
    assert next_myt_midnight(datetime(2026, 10, 21, 15, 59, tzinfo=U)) == datetime(2026, 10, 21, 16, 0, tzinfo=U)
    # 已收盘交易日：美东 10-21 收盘前，最近已收盘是 10-20
    assert cal.last_closed_session("US", datetime(2026, 10, 21, 19, 0, tzinfo=U)) == date(2026, 10, 20)
    assert cal.last_closed_session("US", datetime(2026, 10, 21, 20, 1, tzinfo=U)) == date(2026, 10, 21)


def test_ticks_lots_fees():
    assert tick_size("MY", Decimal("0.995")) == Decimal("0.005")
    assert tick_size("MY", Decimal("5")) == Decimal("0.01")
    assert tick_size("MY", Decimal("50")) == Decimal("0.02")
    assert round_to_tick("MY", Decimal("1.234"), "down") == Decimal("1.23")
    assert round_to_tick("MY", Decimal("0.997"), "up") == Decimal("1.00")
    assert is_valid_tick("US", Decimal("10.01")) and not is_valid_tick("US", Decimal("10.015"))
    assert round_lot_down(250, 100) == 200
    f = calc_fees(TEST_FEE_SCHEDULES["MY"], "BUY", 100, Decimal("2.00"))
    assert f == Decimal("3.00") + Decimal("0.06") + Decimal("0.20")  # 最低佣金 3 + 清算 0.03% + 印花 0.1%
    assert calc_fees(None, "BUY", 1, Decimal(1)) is None
