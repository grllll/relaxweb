"""星潮模拟指数：价格模型、做市商额度、印钞预算与 T+0 结算。"""
import asyncio
import math
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from estate.market import (
    DEFAULT_PRICE_CENTS,
    DEFAULT_SYMBOL,
    INVENTORY_CAP_COINS,
    INVENTORY_COEF,
    FLOW_PER_MINUTE_COINS,
    MARKET_SYMBOLS,
    SYMBOLS,
    cap_milli_for_price,
    cancel_order,
    market_day,
    market_snapshot,
    place_order,
    record_market_candles,
    tradable_milli,
    trade_market,
)
from estate.schema import init_estate
from estate.store import EstateError, ensure_estate
from server.estate.protocol import EstateProtocol


NOW = 2_000_000_000
SYMBOL = DEFAULT_SYMBOL
# 额度与自然流按标的均分：单只标的在基准点位下的额度与分钟吞吐。
PER_SYMBOL_COINS = INVENTORY_CAP_COINS // len(SYMBOLS)
CAP_MILLI = cap_milli_for_price(DEFAULT_PRICE_CENTS)
FLOW_MILLI = FLOW_PER_MINUTE_COINS * 100 * 1000 // DEFAULT_PRICE_CENTS


def adjust_coins(conn, username, delta, kind, detail="", ref=""):
    balance = conn.execute("SELECT coins FROM users WHERE username=?", (username,)).fetchone()[0] + delta
    if balance < 0:
        raise ValueError("金币不足")
    conn.execute("UPDATE users SET coins=? WHERE username=?", (balance, username))
    conn.execute("INSERT INTO coin_transactions VALUES (?,?,?,?,?)",
                 (username, delta, kind, detail, ref))
    return balance


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE users(username TEXT PRIMARY KEY,coins REAL NOT NULL)")
        self.conn.execute("CREATE TABLE coin_transactions(username TEXT,amount REAL,kind TEXT,detail TEXT,ref TEXT)")
        self.conn.execute("INSERT INTO users VALUES ('alice',10000)")
        init_estate(self.conn)
        ensure_estate(self.conn, "alice", NOW)

    def tearDown(self):
        self.conn.close()

    def randn(self, value=0.0):
        """打桩噪声：0 让 OU 状态停在中性，大值把价格推到涨跌停。"""
        return patch("estate.market._randn", return_value=value)

    def fund(self, coins):
        self.conn.execute("UPDATE users SET coins=? WHERE username='alice'", (coins,))

    def ticks(self):
        return self.conn.execute("SELECT COUNT(*) FROM estate_market_ticks "
                                 "WHERE symbol=?", (SYMBOL,)).fetchone()[0]

    def inventory(self):
        return self.conn.execute("SELECT inventory_milli FROM estate_market_symbols "
                                 "WHERE symbol='XTIDE'").fetchone()[0]

    def printed(self, day):
        row = self.conn.execute("SELECT amount_cents FROM estate_market_printed WHERE day=?",
                                (day,)).fetchone()
        return row[0] if row else 0

    # -- 价格模型 ----------------------------------------------------------

    def test_first_quote_anchors_at_default_price(self):
        with self.randn():
            first = market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
        self.assertEqual(first["price"], 1000)
        self.assertEqual(first["anchor"], 1000)
        self.assertEqual(len(first["history"]), 1)
        self.assertEqual(self.ticks(), 1)

    def test_anchor_compounds_five_percent_per_week(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            later = market_snapshot(self.conn, "alice", NOW + 7 * 24 * 3600, adjust_coins, SYMBOL)
        self.assertEqual(later["anchor"], 1050)
        self.assertEqual(later["price"], 1050)

    def test_soft_wall_pulls_the_price_back_toward_the_anchor(self):
        """软墙不是硬顶：价格能被推出去，但回归力随偏离增大而变强。"""
        with self.randn(3.0):
            start = market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            pushed = market_snapshot(self.conn, "alice", NOW + 60 * 120, adjust_coins, SYMBOL)
        with self.randn():
            back = market_snapshot(self.conn, "alice", NOW + 60 * 120 + 24 * 3600, adjust_coins, SYMBOL)
        self.assertGreater(pushed["price"], start["price"])
        self.assertLess(abs(math.log(back["price"] / back["anchor"])),
                        abs(math.log(pushed["price"] / pushed["anchor"])))

    def test_k_lines_still_aggregate_per_minute(self):
        """10 秒一格的撮合不改变图表粒度：K 线仍然每分钟一根。"""
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            market_snapshot(self.conn, "alice", NOW + 30, adjust_coins, SYMBOL)
            minute = market_snapshot(self.conn, "alice", NOW + 60, adjust_coins, SYMBOL)
        self.assertEqual(self.ticks(), 2)
        self.assertEqual([bar["time"] for bar in minute["candles"]["minute"]],
                         [(NOW // 60) * 60, (NOW // 60 + 1) * 60])
        self.assertEqual(minute["quote_slot"], (NOW + 60) // 10)

    def test_volume_accumulates_per_minute_and_per_day(self):
        """成交量是累加的：同一分钟多笔相加，价格 tick 不会把它清零。"""
        self.fund(100_000)
        with self.randn():
            empty = market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            trade_market(self.conn, "alice", "market-vol-0001", "buy", "3", NOW + 1,
                         adjust_coins, SYMBOL)
            trade_market(self.conn, "alice", "market-vol-0002", "buy", "2", NOW + 2,
                         adjust_coins, SYMBOL)
            filled = market_snapshot(self.conn, "alice", NOW + 3, adjust_coins, SYMBOL)
            # 推进价格（写 K 线）不该抹掉已经记下的量
            later = market_snapshot(self.conn, "alice", NOW + 60, adjust_coins, SYMBOL)
        self.assertEqual(empty["volume"]["minute"]["shares"], 0)
        self.assertEqual(filled["volume"]["minute"]["shares"], 5)
        self.assertEqual(filled["volume"]["day"]["shares"], 5)
        self.assertEqual(later["volume"]["minute"]["shares"], 0)
        self.assertEqual(later["volume"]["day"]["shares"], 5)
        self.assertGreater(filled["volume"]["minute"]["amount"], 0)
        self.assertEqual(later["candles"]["minute"][0]["volume"], 5)

    def test_split_keeps_the_traded_amount_but_doubles_the_share_volume(self):
        self.fund(100_000)
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            trade_market(self.conn, "alice", "market-vol-0003", "buy", "4", NOW + 1,
                         adjust_coins, SYMBOL)
            before = market_snapshot(self.conn, "alice", NOW + 2, adjust_coins, SYMBOL)
        self.conn.execute("UPDATE estate_market_symbols SET anchor_cents=anchor_cents*2,"
                          "price_cents=price_cents*2 WHERE symbol='XTIDE'")
        with self.randn():
            after = market_snapshot(self.conn, "alice", NOW + 61, adjust_coins, SYMBOL)
        self.assertEqual(after["split_count"], 1)
        self.assertEqual(after["volume"]["day"]["shares"], 8)
        self.assertAlmostEqual(after["volume"]["day"]["amount"],
                               before["volume"]["day"]["amount"], places=2)

    def test_settlement_runs_every_ten_seconds_but_flow_is_per_minute(self):
        """挂单每 10 秒结算一次；自然流预算仍按分钟计，吞吐不放大。"""
        from estate.market import SLOT_SECONDS
        self.fund(50_000)
        self.assertEqual(SLOT_SECONDS, 10)
        self.assertEqual(FLOW_MILLI, 20_000)
        with self.randn():
            placed = place_order(self.conn, "alice", "market-order-0900", "buy", "990", "30",
                                 NOW, adjust_coins, SYMBOL)
            first_slot = market_snapshot(self.conn, "alice", NOW + 10, adjust_coins, SYMBOL)
            second_slot = market_snapshot(self.conn, "alice", NOW + 20, adjust_coins, SYMBOL)
        self.assertEqual(placed["filled"], 0)
        # 价格没动，所以这 30 份挂在簿里不会被结算；但每分钟 20 份的额度一分钟内只能吃一次
        self.assertEqual(len(first_slot["orders"]), 1)
        self.assertEqual(second_slot["flow_left"]["buy"], 20)

    def test_one_tick_per_minute_and_no_fabrication(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            market_snapshot(self.conn, "alice", NOW + 30, adjust_coins, SYMBOL)
            market_snapshot(self.conn, "alice", NOW + 60, adjust_coins, SYMBOL)
        self.assertEqual(self.ticks(), 2)

    def test_downtime_backfills_every_minute(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            market_snapshot(self.conn, "alice", NOW + 60 * 60, adjust_coins, SYMBOL)
        self.assertEqual(self.ticks(), 61)

    def test_long_downtime_rerolls_without_fabricating_candles(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            market_snapshot(self.conn, "alice", NOW + 3 * 24 * 3600, adjust_coins, SYMBOL)
        # 停机期间的分钟既不补 tick 也不补 K 线，只落当前这一分钟。
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM estate_market_candles "
                                           "WHERE symbol=? AND period='minute'",
                                           (SYMBOL,)).fetchone()[0], 1)

    def test_hold_sentinel_is_gone_but_reads_are_idempotent(self):
        with self.randn():
            first = market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            again = market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
        self.assertEqual(first["price"], again["price"])
        self.assertEqual(self.ticks(), 1)

    # -- K 线 --------------------------------------------------------------

    def test_candles_aggregate_sampled_open_high_low_close(self):
        boundary = (NOW // 3600 + 1) * 3600
        with self.randn():
            first = market_snapshot(self.conn, "alice", boundary - 60, adjust_coins, SYMBOL)
            second = market_snapshot(self.conn, "alice", boundary, adjust_coins, SYMBOL)
        self.assertEqual(first["candles"]["minute"][-1]["close"], 1000)
        self.assertEqual(second["candles"]["minute"][-1]["open"], 1000)
        self.assertEqual(second["candles"]["minute"][-1]["close"], 1000)
        self.assertEqual(len(second["candles"]["hour"]), 2)
        self.assertEqual(first["symbol"], SYMBOL)

    def test_daily_candles_roll_over_at_china_midnight(self):
        boundary = (NOW // 60 + 480) // 1440 * 1440 - 480 + 1440
        record_market_candles(self.conn, SYMBOL, boundary - 1, 100000, 101000)
        record_market_candles(self.conn, SYMBOL, boundary, 101000, 99000)
        record_market_candles(self.conn, SYMBOL, boundary + 1, 99000, 102000)
        rows = self.conn.execute("SELECT start_minute,open_cents,high_cents,low_cents,close_cents "
                                 "FROM estate_market_candles WHERE symbol='XTIDE' AND period='day' "
                                 "ORDER BY start_minute").fetchall()
        self.assertEqual(rows, [(boundary - 1440, 100000, 101000, 100000, 101000),
                                (boundary, 101000, 102000, 99000, 102000)])

    # -- 玩家冲击 ----------------------------------------------------------

    def test_buying_pushes_the_quote_up_and_selling_pulls_it_down(self):
        self.fund(50_000)
        with self.randn():
            before = market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            bought = trade_market(self.conn, "alice", "market-buy-0001", "buy", "20",
                                  NOW, adjust_coins)
            self.assertEqual(self.inventory(), -20 * 1000)
            self.assertAlmostEqual(bought["market"]["price"],
                                   before["price"] * math.exp(INVENTORY_COEF * 20_000 / CAP_MILLI),
                                   places=2)
            sold = trade_market(self.conn, "alice", "market-sell-0001", "sell", "20",
                                NOW, adjust_coins)
        self.assertEqual(self.inventory(), 0)
        self.assertAlmostEqual(sold["market"]["price"], before["price"], places=2)

    def test_execution_price_averages_the_impact(self):
        self.fund(30_000)
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            result = trade_market(self.conn, "alice", "market-buy-0002", "buy", "20",
                                  NOW, adjust_coins)
        after = result["market"]["price"]
        self.assertGreater(result["average_price"], result["price"])
        self.assertLess(result["average_price"], after)
        self.assertAlmostEqual(result["amount"], result["average_price"] * 20 * 1.0025, places=1)

    def test_symbols_are_isolated_from_each_other(self):
        """买一只标的不会动到另一只的价格、库存、持仓与流水。"""
        other = SYMBOLS[2]
        with self.randn():
            before = market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            other_before = market_snapshot(self.conn, "alice", NOW, adjust_coins, other)
        with self.randn():
            trade_market(self.conn, "alice", "market-sym-0001", "buy", "5", NOW + 60,
                         adjust_coins, SYMBOL)
            after = market_snapshot(self.conn, "alice", NOW + 60, adjust_coins, SYMBOL)
            other_after = market_snapshot(self.conn, "alice", NOW + 60, adjust_coins, other)
        self.assertEqual(after["symbol"], SYMBOL)
        self.assertEqual(after["shares"], 5)
        self.assertEqual(other_after["shares"], 0)
        self.assertEqual(other_after["positions"], [])
        self.assertEqual(other_after["fills"], [])
        # 另一只标的只有锚按宏观漂移走，情绪与库存偏移完全没被动过
        self.assertAlmostEqual(other_after["price"] / other_after["anchor"],
                               other_before["price"] / other_before["anchor"], places=6)
        self.assertNotEqual(self.conn.execute(
            "SELECT inventory_milli FROM estate_market_symbols WHERE symbol=?",
            (SYMBOL,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT inventory_milli FROM estate_market_symbols WHERE symbol=?",
            (other,)).fetchone()[0], 0)
        # 每只标的的额度按标的数均分
        self.assertAlmostEqual(after["capacity_left"] * after["price"],
                               PER_SYMBOL_COINS - 5 * after["price"], delta=2000)

    def test_each_symbol_runs_its_own_order_book(self):
        other = SYMBOLS[1]
        with self.randn():
            placed = place_order(self.conn, "alice", "market-sym-0002", "buy", "980", "1",
                                 NOW, adjust_coins, other)
            orders = market_snapshot(self.conn, "alice", NOW, adjust_coins, other)["orders"]
            book = market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
        self.assertEqual(placed["market"]["symbol"], other)
        self.assertEqual(len(orders), 1)
        self.assertEqual(book["orders"], [])

    def test_first_market_call_can_be_a_trade(self):
        with self.randn():
            bought = trade_market(self.conn, "alice", "market-buy-0009", "buy", "1",
                                  NOW, adjust_coins)
        self.assertEqual(self.ticks(), 1)
        self.assertEqual(bought["market"]["shares"], 1)
        self.assertEqual(self.inventory(), -1000)

    def test_maker_capacity_rejects_oversized_trades(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
        self.assertEqual(tradable_milli(self.conn, "buy"), CAP_MILLI)
        with self.assertRaises(EstateError) as error:
            trade_market(self.conn, "alice", "market-cap-0001", "buy", "700",
                         NOW, adjust_coins)
        self.assertEqual(error.exception.code, "market_no_liquidity")

    def test_positions_are_public_and_sorted_by_size(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            trade_market(self.conn, "alice", "market-buy-0101", "buy", "5", NOW, adjust_coins)
            self.conn.execute("INSERT INTO estate_market_positions VALUES ('bob','XTIDE',12000,900000,5000)")
            book = market_snapshot(self.conn, "alice", NOW + 60, adjust_coins, SYMBOL)
        rows = book["positions"]
        self.assertEqual([row["username"] for row in rows], ["bob", "alice"])
        self.assertEqual(rows[0]["shares"], 12)
        self.assertEqual(rows[0]["realized_pnl"], 50)
        self.assertEqual(rows[1]["shares"], 5)
        self.assertAlmostEqual(rows[0]["market_value"], 12 * book["price"], places=2)
        self.assertAlmostEqual(rows[0]["unrealized_pnl"],
                               rows[0]["market_value"] - rows[0]["cost_basis"], places=2)
        # 自己的持仓也在榜上，人人都能看到
        self.assertIn("alice", [row["username"] for row in rows])

    def test_flat_positions_drop_off_the_board(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            trade_market(self.conn, "alice", "market-buy-0102", "buy", "5", NOW, adjust_coins)
            trade_market(self.conn, "alice", "market-sell-0102", "sell", "5", NOW, adjust_coins)
            after = market_snapshot(self.conn, "alice", NOW + 60, adjust_coins, SYMBOL)
        self.assertEqual(after["positions"], [])
        self.assertEqual(after["shares"], 0)
        self.assertNotEqual(after["realized_pnl"], 0)

    def test_book_never_advertises_more_than_this_minutes_flow(self):
        """盘口显示的必须是真能成交的量：自然流被用掉后盘口要同步缩水。"""
        self.fund(50_000)
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            before = market_snapshot(self.conn, "bob", NOW, adjust_coins, SYMBOL)
            trade_market(self.conn, "alice", "market-book-0001", "buy", "15", NOW + 1,
                         adjust_coins, SYMBOL)
            after = market_snapshot(self.conn, "bob", NOW + 2, adjust_coins, SYMBOL)["book"]
            flow_after = market_snapshot(self.conn, "bob", NOW + 2, adjust_coins, SYMBOL)
            fresh = market_snapshot(self.conn, "bob", NOW + 61, adjust_coins, SYMBOL)["book"]
        self.assertEqual(sum(level["quantity"] for level in before["book"]["asks"]), 20)
        self.assertEqual(flow_after["flow_left"]["buy"], 5)
        self.assertEqual(sum(level["quantity"] for level in after["asks"]), 5)
        self.assertEqual(sum(level["quantity"] for level in fresh["asks"]), 20)

    def test_book_hides_the_side_with_no_capacity_left(self):
        self.fund(50_000)
        with self.randn():
            before = market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
        self.assertEqual(len(before["book"]["asks"]), 5)
        self.assertEqual(len(before["book"]["bids"]), 5)
        # 做市商被买满（额度全部变成空头），卖档不应再显示
        self.conn.execute("UPDATE estate_market_symbols SET inventory_milli=? WHERE symbol=?",
                          (-CAP_MILLI, SYMBOL))
        full = market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
        self.assertEqual(full["book"]["asks"], [])
        self.assertEqual(len(full["book"]["bids"]), 5)
        with self.assertRaises(EstateError) as error:
            trade_market(self.conn, "alice", "market-cap-0002", "buy", "1",
                         NOW, adjust_coins)
        self.assertEqual(error.exception.code, "market_no_liquidity")

    def test_price_level_never_blocks_a_trade(self):
        """不设涨跌停：价格跑多远都能成交，唯一的限制是做市商额度。"""
        self.fund(500_000)
        with self.randn(3.0):
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            driven = market_snapshot(self.conn, "alice", NOW + 60 * 120, adjust_coins, SYMBOL)
        self.assertGreater(driven["price"] / driven["anchor"], 1.6)
        # 额度按金币计量：价格越界时份数变小，但金币口径的额度不变
        from estate.market import base_price_cents
        self.assertAlmostEqual(tradable_milli(self.conn, "buy") / 1000
                               * base_price_cents(self.conn) / 100, PER_SYMBOL_COINS, delta=2000)
        bought = trade_market(self.conn, "alice", "market-free-0001", "buy", "1",
                              NOW + 60 * 120, adjust_coins)
        self.assertEqual(bought["market"]["shares"], 1)
        self.assertGreater(bought["market"]["price"], driven["price"])

    # -- 结算 --------------------------------------------------------------

    def test_fractional_roundtrip_costs_fee_and_replay_does_not_double_spend(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            bought = trade_market(self.conn, "alice", "market-buy-0003", "buy", "0.125",
                                  NOW, adjust_coins)
            replay = trade_market(self.conn, "alice", "market-buy-0003", "buy", "0.125",
                                  NOW, adjust_coins)
            self.assertTrue(replay["replayed"])
            self.assertEqual(bought["coins"], replay["coins"])
            self.assertEqual(self.inventory(), -125)
            sold = trade_market(self.conn, "alice", "market-sell-0003", "sell", "0.125",
                                NOW, adjust_coins)
        self.assertEqual(sold["market"]["shares"], 0)
        self.assertLess(sold["coins"], 10000)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM coin_transactions").fetchone()[0], 2)

    def test_invalid_quantity_and_insufficient_holdings(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            for quantity in ("0", "-1", "0.0001", "NaN", "1e9"):
                with self.assertRaises(EstateError):
                    trade_market(self.conn, "alice", "market-invalid-0001", "buy", quantity,
                                 NOW, adjust_coins)
            with self.assertRaises(EstateError) as error:
                trade_market(self.conn, "alice", "market-short-0001", "sell", "1",
                             NOW, adjust_coins)
        self.assertEqual(error.exception.code, "market_shares")

    # -- 印钞预算 ----------------------------------------------------------

    def test_exhausted_budget_freezes_the_anchor_but_not_the_noise(self):
        with self.randn():
            first = market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            self.conn.execute("INSERT INTO estate_market_printed(day,amount_cents) VALUES (?,?)",
                              (market_day(NOW // 60), 10 ** 9))
            frozen = market_snapshot(self.conn, "alice", NOW + 7 * 24 * 3600, adjust_coins, SYMBOL)
        self.assertTrue(frozen["trend_paused"])
        self.assertEqual(frozen["anchor"], first["anchor"])
        self.assertEqual(frozen["price"], first["price"])

    def test_net_printing_is_recorded_per_china_day(self):
        day = market_day(NOW // 60)
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            bought = trade_market(self.conn, "alice", "market-buy-0004", "buy", "1",
                                  NOW, adjust_coins)
            sold = trade_market(self.conn, "alice", "market-sell-0004", "sell", "1",
                                NOW, adjust_coins)
        self.assertEqual(self.printed(day),
                         int(round(sold["amount"] * 100)) - int(round(bought["amount"] * 100)))

    def test_money_supply_counts_escrowed_positions(self):
        from estate.market import money_supply_cents
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            before = money_supply_cents(self.conn)
            trade_market(self.conn, "alice", "market-buy-0005", "buy", "1", NOW, adjust_coins)
            after = money_supply_cents(self.conn)
        # 买入销毁流通金币、同时等额变成持仓成本，货币供应基本不变（只差手续费）。
        self.assertEqual(before, 1_000_000)
        self.assertEqual(after, before)


class MarketResetTests(unittest.TestCase):
    """新市场纪元 = 重开整个股市：旧数据整体作废，不做迁移。"""

    def test_new_epoch_wipes_everything_and_reseeds_symbols(self):
        from estate.market import MARKET_EPOCH, set_market_epoch
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE users(username TEXT PRIMARY KEY,coins REAL NOT NULL)")
        conn.execute("CREATE TABLE coin_transactions(username TEXT,amount REAL,kind TEXT,detail TEXT,ref TEXT)")
        conn.execute("INSERT INTO users VALUES ('alice',10000)")
        init_estate(conn)
        conn.execute("INSERT INTO estate_market_positions VALUES ('alice','XTIDE',1500,77700,0)")
        conn.execute("INSERT INTO estate_market_ticks VALUES ('XTIDE',100,77700)")
        conn.execute("INSERT INTO estate_market_candles VALUES "
                     "('XTIDE','minute',100,77700,77700,77700,77700,1000,77700)")
        conn.execute("INSERT INTO estate_market_orders(username,symbol,side,price_cents,qty_milli,"
                     "created_minute,expires_minute) VALUES ('alice','XTIDE','buy',77700,1000,100,200)")
        conn.execute("INSERT INTO estate_market_fills(symbol,username,side,minute,price_cents,"
                     "qty_milli,amount_cents) VALUES ('XTIDE','alice','buy',100,77700,1000,77700)")
        conn.execute("INSERT INTO estate_market_printed VALUES (1,50000)")
        conn.execute("UPDATE estate_market_symbols SET anchor_cents=77700,price_cents=77700")
        conn.execute("CREATE TABLE estate_market_index (id INTEGER PRIMARY KEY, price_cents INTEGER)")

        set_market_epoch(conn, MARKET_EPOCH - 1)
        init_estate(conn)

        for table in ("estate_market_positions", "estate_market_ticks", "estate_market_candles",
                      "estate_market_orders", "estate_market_fills", "estate_market_printed"):
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        self.assertEqual(conn.execute("SELECT anchor_cents,price_cents,ou_slot,inventory_milli "
                                      "FROM estate_market_symbols WHERE symbol='XTIDE'").fetchone(),
                         (100000.0, 100000, -1, 0))
        self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master "
                                       "WHERE name='estate_market_index'").fetchone())
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM estate_market_symbols").fetchone()[0],
                         len(MARKET_SYMBOLS))
        self.assertEqual(conn.execute("SELECT value FROM estate_market_meta "
                                      "WHERE key='epoch'").fetchone()[0], str(MARKET_EPOCH))
        # 同一纪元重复初始化不会再次清库
        conn.execute("UPDATE estate_market_symbols SET price_cents=424242 WHERE symbol='XTIDE'")
        init_estate(conn)
        self.assertEqual(conn.execute("SELECT price_cents FROM estate_market_symbols "
                                      "WHERE symbol='XTIDE'").fetchone()[0], 424242)
        conn.close()


class SplitTests(unittest.TestCase):
    """1:2 拆股：份额 ×2、价格 ÷2，持仓成本与金币口径不变。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE users(username TEXT PRIMARY KEY,coins REAL NOT NULL)")
        self.conn.execute("CREATE TABLE coin_transactions(username TEXT,amount REAL,kind TEXT,detail TEXT,ref TEXT)")
        self.conn.execute("INSERT INTO users VALUES ('alice',100000)")
        init_estate(self.conn)
        ensure_estate(self.conn, "alice", NOW)

    def tearDown(self):
        self.conn.close()

    def randn(self, value=0.0):
        return patch("estate.market._randn", return_value=value)

    def tick_prices(self):
        return [row[0] for row in self.conn.execute(
            "SELECT price_cents FROM estate_market_ticks WHERE symbol='XTIDE' ORDER BY minute")]

    def symbol(self, *columns):
        return self.conn.execute(f"SELECT {','.join(columns)} FROM estate_market_symbols "
                                 "WHERE symbol='XTIDE'").fetchone()

    def test_split_halves_the_anchor_and_doubles_shares(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            bought = trade_market(self.conn, "alice", "market-buy-2001", "buy", "20",
                                  NOW, adjust_coins)
        cost = bought["market"]["cost_basis"]
        self.assertEqual(bought["market"]["shares"], 20)
        self.assertEqual(self.symbol("inventory_milli")[0], -20 * 1000)
        # 把锚顶到阈值，下一分钟触发 1:2 拆股
        self.conn.execute("UPDATE estate_market_symbols SET anchor_cents=?",
                          (2 * DEFAULT_PRICE_CENTS,))
        with self.randn():
            after = market_snapshot(self.conn, "alice", NOW + 60, adjust_coins, SYMBOL)
        self.assertEqual(after["split_count"], 1)
        self.assertTrue(1000 <= after["anchor"] < 1001, after["anchor"])
        self.assertEqual(after["shares"], 40)
        self.assertEqual(after["cost_basis"], cost)
        self.assertEqual(self.symbol("inventory_milli")[0], -40 * 1000)
        # 额度按金币计量：拆股后份数翻倍、价格减半，金币口径的额度与冲击都不变
        from estate.market import base_price_cents, inventory_cap_milli
        base = base_price_cents(self.conn) / 100
        self.assertAlmostEqual(inventory_cap_milli(self.conn) / 1000 * base,
                               PER_SYMBOL_COINS, delta=2000)
        self.assertAlmostEqual(after["capacity_left"] * base, PER_SYMBOL_COINS - 40 * base,
                               delta=4000)

    def test_split_keeps_the_chart_continuous_and_cancels_orders(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            place_order(self.conn, "alice", "market-order-2101", "buy", "980", "1",
                        NOW, adjust_coins, SYMBOL)
        # 把锚、报价与整条历史一起抬到 2000 点，模拟"价格涨到拆股线"
        self.conn.execute("UPDATE estate_market_symbols SET anchor_cents=anchor_cents*2,"
                          "price_cents=price_cents*2 WHERE symbol='XTIDE'")
        self.conn.execute("UPDATE estate_market_ticks SET price_cents=price_cents*2 "
                          "WHERE symbol='XTIDE'")
        self.conn.execute("UPDATE estate_market_candles SET open_cents=open_cents*2,"
                          "high_cents=high_cents*2,low_cents=low_cents*2,close_cents=close_cents*2 "
                          "WHERE symbol='XTIDE'")
        with self.randn():
            after = market_snapshot(self.conn, "alice", NOW + 60, adjust_coins, SYMBOL)
        self.assertEqual(after["split_count"], 1)
        self.assertEqual(after["orders"], [])
        self.assertEqual(self.conn.execute("SELECT status,reason FROM estate_market_orders "
                                           "WHERE id=1").fetchone(),
                         ("cancelled", "因 1:2 拆股自动撤销"))
        # 历史 K 线与新的一格在同一标度上：没有断崖
        prices = self.tick_prices()
        self.assertLess(max(prices) / min(prices), 1.5, prices)

    def test_split_halves_every_stored_price_and_keeps_amounts(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
        self.conn.execute("INSERT INTO estate_market_fills(symbol,username,side,minute,"
                          "price_cents,qty_milli,amount_cents) VALUES ('XTIDE','alice','buy',?,"
                          "100000,1000,100000)", (NOW // 60,))
        self.conn.execute("UPDATE estate_market_symbols SET anchor_cents=?,price_cents=? "
                          "WHERE symbol='XTIDE'", (2 * DEFAULT_PRICE_CENTS, 2 * DEFAULT_PRICE_CENTS))
        with self.randn():
            after = market_snapshot(self.conn, "alice", NOW + 60, adjust_coins, SYMBOL)
        self.assertEqual(after["split_count"], 1)
        self.assertAlmostEqual(after["anchor"], 1000, delta=1)
        # 成交流水：价格 ÷2、份数 ×2、金币金额不变
        fill = self.conn.execute("SELECT price_cents,qty_milli,amount_cents "
                                 "FROM estate_market_fills WHERE symbol='XTIDE'").fetchone()
        self.assertEqual(fill, (50000, 2000, 100000))
        self.assertLess(max(self.tick_prices()), 2 * DEFAULT_PRICE_CENTS)

    def test_coin_denominated_limits_survive_the_split(self):
        with self.randn():
            before = market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
        from estate.market import base_price_cents
        self.assertEqual(before["flow_left"]["buy"], 20)
        self.assertAlmostEqual(before["flow_left"]["buy"] * base_price_cents(self.conn) / 100,
                               20_000, delta=100)
        self.conn.execute("UPDATE estate_market_symbols SET anchor_cents=?,price_cents=?",
                          (2 * DEFAULT_PRICE_CENTS, 2 * DEFAULT_PRICE_CENTS))
        with self.randn():
            after = market_snapshot(self.conn, "alice", NOW + 60, adjust_coins, SYMBOL)
        self.assertEqual(after["split_count"], 1)
        # 拆股后份数回到基准，金币口径的分钟吞吐与额度都还是 2 万 / 200 万
        base = base_price_cents(self.conn) / 100
        self.assertEqual(after["flow_left"]["buy"], 20)
        self.assertAlmostEqual(after["flow_left"]["buy"] * base, 20_000, delta=100)
        self.assertAlmostEqual(after["capacity_left"] * base, PER_SYMBOL_COINS, delta=2000)

    def test_split_is_announced_exactly_once(self):
        from estate.market import split_announcement
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
        self.assertIsNone(split_announcement(self.conn))
        self.conn.execute("UPDATE estate_market_symbols SET anchor_cents=?",
                          (2 * DEFAULT_PRICE_CENTS,))
        with self.randn():
            market_snapshot(self.conn, "alice", NOW + 60, adjust_coins, SYMBOL)
        message = split_announcement(self.conn)
        self.assertIn("1:2 拆股", message)
        self.assertIn("第 1 次", message)
        # 标记与拆股同事务：重复调用不会再播报
        self.assertIsNone(split_announcement(self.conn))
        self.assertEqual(self.symbol("split_count")[0], 1)

    def test_repeated_split_after_a_long_offline_gap(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
        from estate.market import MINUTES_PER_WEEK
        # 锚按每周 5% 复利：离线一年会连续触发多次 1:2 拆股，点位仍回到基准附近
        with self.randn():
            later = market_snapshot(self.conn, "alice", NOW + 52 * MINUTES_PER_WEEK * 60,
                                    adjust_coins)
        self.assertGreaterEqual(later["split_count"], 3)
        self.assertTrue(1000 <= later["anchor"] < 2000, later["anchor"])


class OrderTests(unittest.TestCase):
    """限价委托：窗口、挂单、每分钟结算、撤单与过期。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE users(username TEXT PRIMARY KEY,coins REAL NOT NULL)")
        self.conn.execute("CREATE TABLE coin_transactions(username TEXT,amount REAL,kind TEXT,detail TEXT,ref TEXT)")
        self.conn.execute("INSERT INTO users VALUES ('alice',10000)")
        init_estate(self.conn)
        ensure_estate(self.conn, "alice", NOW)

    def tearDown(self):
        self.conn.close()

    def randn(self, value=0.0):
        return patch("estate.market._randn", return_value=value)

    def fund(self, coins):
        self.conn.execute("UPDATE users SET coins=? WHERE username='alice'", (coins,))

    def orders(self):
        return self.conn.execute("SELECT status,reason FROM estate_market_orders").fetchall()

    def test_market_orders_are_capped_by_the_minute_flow(self):
        self.fund(200_000)
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            with self.assertRaises(EstateError) as error:
                trade_market(self.conn, "alice", "market-flow-0001", "buy", "50",
                             NOW, adjust_coins)
            self.assertEqual(error.exception.code, "market_flow")
            first = trade_market(self.conn, "alice", "market-flow-0002", "buy", "20",
                                 NOW, adjust_coins)
            with self.assertRaises(EstateError):
                trade_market(self.conn, "alice", "market-flow-0003", "buy", "1",
                             NOW, adjust_coins)
            later = trade_market(self.conn, "alice", "market-flow-0004", "buy", "20",
                                 NOW + 60, adjust_coins)
        self.assertEqual(first["market"]["shares"], 20)
        self.assertEqual(later["market"]["shares"], 40)
        self.assertEqual(first["market"]["flow_left"]["buy"], 0)

    def test_flow_check_tolerates_a_stale_quote(self):
        """按上一分钟报价算出的"刚好买满"不该因为报价微动而被拒。"""
        self.fund(200_000)
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            trade_market(self.conn, "alice", "market-flow-0007", "buy", "20", NOW, adjust_coins)
            # 锚在这一分钟里长了 0.0005%，自然流份数随之缩水到 19.9999 份
            result = trade_market(self.conn, "alice", "market-flow-0008", "buy", "20",
                                  NOW + 60, adjust_coins)
        self.assertEqual(result["market"]["shares"], 40)

    def test_resting_buy_fills_when_the_price_drops(self):
        self.fund(50_000)
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            placed = place_order(self.conn, "alice", "market-order-0001", "buy", "990", "5",
                                 NOW, adjust_coins)
        self.assertEqual(placed["filled"], 0)
        self.assertEqual(placed["market"]["orders"][0]["remaining"], 5)
        with self.randn(-5.0):
            later = market_snapshot(self.conn, "alice", NOW + 60 * 6, adjust_coins, SYMBOL)
        self.assertEqual(later["orders"], [])
        self.assertEqual(later["shares"], 5)
        self.assertEqual(later["fills"][0]["side"], "buy")
        self.assertLess(later["fills"][0]["price"], 990)
        self.assertEqual(self.orders(), [("filled", "")])

    def test_marketable_limit_order_fills_immediately(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            placed = place_order(self.conn, "alice", "market-order-0002", "buy", "1005", "3",
                                 NOW, adjust_coins)
        self.assertEqual(placed["filled"], 3)
        self.assertEqual(placed["market"]["orders"], [])
        self.assertEqual(placed["market"]["shares"], 3)

    def test_order_window_and_price_grid_are_enforced(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            for price in ("1021", "979", "0", "10.5", "abc", ""):
                with self.assertRaises(EstateError) as error:
                    place_order(self.conn, "alice", "market-order-0003", "buy", price, "0.001",
                                NOW, adjust_coins)
                self.assertIn(error.exception.code, ("market_price", "market_price_band"))
            allowed = place_order(self.conn, "alice", "market-order-0004", "buy", "1020", "0.001",
                                  NOW, adjust_coins)
        self.assertEqual(allowed["filled"], 0.001)

    def test_cancel_order(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            placed = place_order(self.conn, "alice", "market-order-0005", "buy", "980", "1",
                                 NOW, adjust_coins)
            order_id = placed["market"]["orders"][0]["id"]
            cancelled = cancel_order(self.conn, "alice", "market-cancel-0005", order_id, NOW, adjust_coins)
        self.assertEqual(cancelled["action"], "market_cancel")
        self.assertEqual(cancelled["market"]["orders"], [])
        self.assertEqual(self.orders(), [("cancelled", "玩家撤单")])
        with self.assertRaises(EstateError) as error:
            cancel_order(self.conn, "alice", "market-cancel-0006", order_id, NOW, adjust_coins)
        self.assertEqual(error.exception.code, "market_order_missing")

    def test_order_expires_after_a_day(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            place_order(self.conn, "alice", "market-order-0006", "buy", "980", "1",
                        NOW, adjust_coins)
            later = market_snapshot(self.conn, "alice", NOW + 24 * 3600 + 60, adjust_coins, SYMBOL)
        self.assertEqual(later["orders"], [])
        self.assertEqual(self.orders(), [("expired", "超过 24 小时未成交")])

    def test_resting_order_is_cancelled_when_the_wallet_is_empty(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            place_order(self.conn, "alice", "market-order-0007", "buy", "990", "5",
                        NOW, adjust_coins)
        self.fund(100)
        with self.randn(-5.0):
            later = market_snapshot(self.conn, "alice", NOW + 60 * 6, adjust_coins, SYMBOL)
        self.assertEqual(later["orders"], [])
        self.assertEqual(later["shares"], 0)
        self.assertEqual(self.orders(), [("cancelled", "金币不足")])

    def test_sell_orders_reserve_shares(self):
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            bought = place_order(self.conn, "alice", "market-order-0008", "buy", "1005", "3",
                                 NOW, adjust_coins)
            self.assertEqual(bought["filled"], 3)
            with self.assertRaises(EstateError) as error:
                place_order(self.conn, "alice", "market-order-0009", "sell", "1010", "5",
                            NOW, adjust_coins)
            self.assertEqual(error.exception.code, "market_shares")
            first = place_order(self.conn, "alice", "market-order-0010", "sell", "1010", "2",
                                NOW, adjust_coins)
            with self.assertRaises(EstateError):
                place_order(self.conn, "alice", "market-order-0011", "sell", "1010", "2",
                            NOW, adjust_coins)
        self.assertEqual(len(first["market"]["orders"]), 1)
        self.assertEqual(first["market"]["orders"][0]["side"], "sell")

    def test_maker_fee_is_a_fifth_of_the_taker_fee(self):
        from decimal import Decimal as D
        from estate.market import MAKER_FEE_RATE, TAKER_FEE_RATE, apply_fill
        self.fund(100_000)
        with self.randn():
            price = int(market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)["price"] * 100)
        taker = apply_fill(self.conn, SYMBOL, "alice", "buy", 10_000, price, NOW, adjust_coins,
                           "fee-taker-0001")
        maker = apply_fill(self.conn, SYMBOL, "alice", "buy", 10_000, price, NOW, adjust_coins,
                           "fee-maker-0002", maker=True)
        self.assertLess(maker["cents"], taker["cents"])
        self.assertAlmostEqual(maker["cents"] / taker["cents"],
                               float((1 + MAKER_FEE_RATE) / (1 + TAKER_FEE_RATE)), places=5)
        notional = D(repr(taker["average_price"])) * 1000
        self.assertAlmostEqual(float(D(taker["cents"]) / notional - 1), float(TAKER_FEE_RATE), places=4)
        self.assertAlmostEqual(float(D(maker["cents"]) / notional - 1), float(MAKER_FEE_RATE), places=4)

    def test_large_order_fills_in_minute_slices(self):
        self.fund(100_000)
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            place_order(self.conn, "alice", "market-order-0012", "buy", "990", "50",
                        NOW, adjust_coins)
        from estate.market import base_price_cents
        with self.randn(-5.0):
            first = market_snapshot(self.conn, "alice", NOW + 60 * 3, adjust_coins, SYMBOL)
            first_base = base_price_cents(self.conn) / 100
            second = market_snapshot(self.conn, "alice", NOW + 60 * 4, adjust_coins, SYMBOL)
            second_base = base_price_cents(self.conn) / 100
        # 自然流按金币计量：价格越低同一分钟能买到的份数越多，但每分钟都是 2 万金币
        self.assertAlmostEqual(first["shares"] * first_base, 20_000, delta=100)
        self.assertAlmostEqual((second["shares"] - first["shares"]) * second_base, 20_000, delta=100)
        self.assertAlmostEqual(second["orders"][0]["remaining"], 50 - second["shares"], places=3)
        self.assertAlmostEqual(second["orders"][0]["filled"], second["shares"], places=3)

    def test_flow_budget_is_shared_between_market_and_resting_orders(self):
        from estate.market import base_price_cents
        self.fund(100_000)
        with self.randn():
            market_snapshot(self.conn, "alice", NOW, adjust_coins, SYMBOL)
            place_order(self.conn, "alice", "market-order-0013", "buy", "990", "40",
                        NOW, adjust_coins)
        with self.randn(-5.0):
            filled = market_snapshot(self.conn, "alice", NOW + 60, adjust_coins, SYMBOL)
            self.assertAlmostEqual(filled["shares"]
                                   * base_price_cents(self.conn) / 100, 20_000, delta=200)
            self.assertEqual(filled["flow_left"]["buy"], 0)
            with self.assertRaises(EstateError) as error:
                trade_market(self.conn, "alice", "market-flow-0005", "buy", "1",
                             NOW + 60, adjust_coins)
            self.assertEqual(error.exception.code, "market_flow")


class MarketWatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_advances_without_any_player_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("CREATE TABLE users(username TEXT PRIMARY KEY,coins REAL NOT NULL)")
                init_estate(conn)
            protocol = EstateProtocol(database=lambda: closing(sqlite3.connect(path)), clients={},
                                      send_json=None, send_encoded=None, presence=None)
            try:
                await asyncio.sleep(1.2)
                with closing(sqlite3.connect(path)) as conn:
                    count = conn.execute("SELECT COUNT(*) FROM estate_market_candles "
                                         "WHERE symbol='XTIDE' AND period='minute'").fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                protocol._market_watcher.cancel()
                await asyncio.gather(protocol._market_watcher, return_exceptions=True)


class InventoryMathTests(unittest.TestCase):
    def test_average_multiplier_is_the_integral_of_the_impact(self):
        from estate.market import average_execution_multiplier, cap_milli_for_price
        cap = cap_milli_for_price(DEFAULT_PRICE_CENTS)
        self.assertEqual(average_execution_multiplier(0, "buy", cap), 1.0)
        small = average_execution_multiplier(1000, "buy", cap)
        self.assertAlmostEqual(small, 1 + INVENTORY_COEF * 1000 / cap / 2, places=6)
        self.assertGreater(average_execution_multiplier(500_000, "buy", cap),
                           average_execution_multiplier(500_000, "sell", cap))

    def test_roundtrip_of_the_same_size_has_no_self_arbitrage(self):
        from estate.market import average_execution_multiplier, cap_milli_for_price, inventory_skew
        # 买入均价 = 卖出时中心价 × 卖出均价倍率：同量买卖只亏手续费，
        # 冲击不会被玩家自己套利。
        cap = cap_milli_for_price(DEFAULT_PRICE_CENTS)
        buy = average_execution_multiplier(1000, "buy", cap)
        sell = average_execution_multiplier(1000, "sell", cap)
        self.assertAlmostEqual(buy, math.exp(inventory_skew(-1000, cap)) * sell, places=12)


if __name__ == "__main__":
    unittest.main()
