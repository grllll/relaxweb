"""星潮股市：多只标的的纯游戏内行情、做市商额度与 T+0 交易。

每只标的价格都是三层结构（推导与标定见 ``docs/stock-market-design.md``）：

    P = 锚 · exp(x + s)

- **锚** ``A(t) = A₀ · (1+周漂移)^(周数)``：长期期望，不是保底。
- **x**：均值回归的市场情绪（OU 过程），半衰期与 σ 逐标的配置，
  越靠近 ±60% 软墙回归越强——**没有硬顶也没有涨跌停**，价格可以越过它。
- **s**：做市商净持仓造成的偏移，``s = −0.25 · I / 额度``。
  玩家买入把价格推高、卖出把价格压低，且不会自动复原。

做市商是系统唯一的对手盘：**全市场总额度按标的数均分**，每只标的另有
每分钟自然流上限。系统唯一的印钞来源是玩家已实现盈利，按滚动 4 周预算
封顶；预算全市场共享，耗尽时所有标的的锚一起暂停上移，波动照常。
"""
import math
import re
import secrets
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from estate.store import EstateError, credit, debit, estate_error, run_action


# 吃单（立即成交）与挂单（等自然流分批成交）分别计费，鼓励报单提供流动性。
TAKER_FEE_RATE = Decimal("0.0025")
MAKER_FEE_RATE = Decimal("0.0005")

# 标的目录：漂移越高、σ 越大、半衰期越短 = 越刺激。
MARKET_SYMBOLS = (
    {"symbol": "XTIDE", "name": "星潮模拟指数", "weekly_growth": 0.05,
     "sigma": 0.22, "half_life_minutes": 2.5 * 1440,
     "blurb": "全市场情绪合成，波动与漂移都是基准档。"},
    {"symbol": "XCROP", "name": "庄园农业板", "weekly_growth": 0.035,
     "sigma": 0.15, "half_life_minutes": 4 * 1440,
     "blurb": "收成与仓库容量驱动，走势稳、回撤浅。"},
    {"symbol": "XORE", "name": "深矿资源板", "weekly_growth": 0.07,
     "sigma": 0.30, "half_life_minutes": 1.5 * 1440,
     "blurb": "矿脉品位与事故消息驱动，波动最大。"},
)
SYMBOL_INFO = {item["symbol"]: item for item in MARKET_SYMBOLS}
SYMBOLS = tuple(item["symbol"] for item in MARKET_SYMBOLS)
DEFAULT_SYMBOL = SYMBOLS[0]

# 市场纪元：改动它就等于重开整个股市——旧持仓、K 线、委托、流水与印钞预算
# 全部重置，不做任何旧数据迁移（见 docs/stock-market-design.md §9）。
MARKET_EPOCH = 4
QUANTITY_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]{1,3})?$")
CANDLE_LIMITS = {"minute": 60, "hour": 72, "day": 90}

# 时间粒度：行情每 SLOT_SECONDS 推进一步，K 线仍然按分钟聚合落库。
SLOT_SECONDS = 10
SLOTS_PER_MINUTE = 60 // SLOT_SECONDS
SLOTS_PER_DAY = 1440 * SLOTS_PER_MINUTE
SLOTS_PER_WEEK = 7 * SLOTS_PER_DAY

# 价格模型参数。
MINUTES_PER_WEEK = 10080
BAND_MULTIPLIER = 1.6
BAND = math.log(BAND_MULTIPLIER)
WALL_FROM = 0.60
WALL_K = 12.0
INVENTORY_COEF = 0.25
DEFAULT_PRICE_CENTS = 100000
# 额度与自然流都按**金币**计量，再按当时价格折算成份数：价格（含拆股）变化时
# 金币口径的额度、冲击与吞吐都恒定——10 万金币的买入永远推动价格 1.26%。
INVENTORY_CAP_COINS = 2_000_000           # 全市场总额度，按标的数均分
FLOW_PER_MINUTE_COINS = 20_000            # 每只标的每分钟自然流
FLOW_TOLERANCE = 0.01
# 拆股：锚到达基准点位两倍时 1:2 拆股，把点位打回基准。
SPLIT_AT_CENTS = 2 * DEFAULT_PRICE_CENTS
SPLIT_FACTOR = 2
MAX_BACKFILL_SLOTS = 2 * SLOTS_PER_DAY

# 自然盘与挂单（阶段 2 简化版，见 docs/stock-market-design.md §11.2）。
NATURAL_LEVELS = ((1, 1), (2, 2), (3, 3), (4, 6), (5, 8))
ORDER_WINDOW_CENTS = 20 * 100
ORDER_TTL_MINUTES = 24 * 60
MAX_OPEN_ORDERS = 10
FILL_HISTORY_LIMIT = 4000

# 印钞预算：滚动 4 周的净印钞不得超过 4 个周预算，全市场共享（§7.2）。
WEEKLY_BUDGET_RATE = Decimal("0.05")
BUDGET_WINDOW_DAYS = 28

# 基准点位下的份数口径，仅供换算与测试引用。
INVENTORY_CAP_MILLI = INVENTORY_CAP_COINS * 100 * 1000 // DEFAULT_PRICE_CENTS
FLOW_PER_MINUTE_MILLI = FLOW_PER_MINUTE_COINS * 100 * 1000 // DEFAULT_PRICE_CENTS


def symbol_name(symbol):
    return SYMBOL_INFO.get(symbol, {}).get("name", symbol)


def symbol_or_default(symbol):
    """把客户端传来的标的收敛到目录内的合法值，未知标的直接拒绝。"""
    value = str(symbol or "").strip().upper()
    if not value:
        return DEFAULT_SYMBOL
    if value not in SYMBOL_INFO:
        raise estate_error(("market_symbol", "标的不存在"))
    return value


def market_tables():
    return ("estate_market_positions", "estate_market_ticks", "estate_market_candles",
            "estate_market_orders", "estate_market_fills", "estate_market_printed",
            "estate_market_symbols", "estate_market_index")


def seed_market(conn):
    """建表后写入标的行；已存在的标的保留自己的行情状态。"""
    for item in MARKET_SYMBOLS:
        conn.execute("INSERT OR IGNORE INTO estate_market_symbols"
                     "(symbol,name,anchor_cents,price_cents) VALUES (?,?,?,?)",
                     (item["symbol"], item["name"], float(DEFAULT_PRICE_CENTS),
                      DEFAULT_PRICE_CENTS))


def _meta_get(conn, key, default=0):
    row = conn.execute("SELECT value FROM estate_market_meta WHERE key=?", (key,)).fetchone()
    try:
        return int(row[0]) if row else default
    except (TypeError, ValueError):
        return default


def _meta_set(conn, key, value):
    conn.execute("INSERT INTO estate_market_meta(key,value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def market_epoch(conn):
    return _meta_get(conn, "epoch", None)


def set_market_epoch(conn, epoch):
    _meta_set(conn, "epoch", epoch)


def splits_so_far(conn, symbol=DEFAULT_SYMBOL):
    row = conn.execute("SELECT split_count FROM estate_market_symbols WHERE symbol=?",
                       (symbol,)).fetchone()
    return row[0] if row else 0


def split_announcement(conn):
    """有还没播报的拆股时返回全服公告文本并标记已播报，否则返回 None。

    标记与拆股写在同一个库连接里，因此无论谁触发（后台采样、读快照、下单）
    都只会播报一次，不会漏也不会重复。多只标的的公告会合并成一条。
    """
    pending = []
    for symbol in SYMBOLS:
        splits = splits_so_far(conn, symbol)
        row = conn.execute("SELECT announced_splits FROM estate_market_symbols WHERE symbol=?",
                           (symbol,)).fetchone()
        if row is None or splits <= row[0]:
            continue
        conn.execute("UPDATE estate_market_symbols SET announced_splits=? WHERE symbol=?",
                     (splits, symbol))
        pending.append(f"{symbol_name(symbol)}（第 {splits} 次）")
    if not pending:
        return None
    return (f"📈 {'、'.join(pending)}触发 1:{SPLIT_FACTOR} 拆股："
            f"份额 ×{SPLIT_FACTOR}、价格 ÷{SPLIT_FACTOR}，持仓成本与金币金额不变；"
            "未成交的委托已自动撤销，打开庄园 → 股市可以查看新的持仓。")


def _randn():
    """标准正态噪声；测试打桩这一个函数即可复现整条价格路径。"""
    return secrets.SystemRandom().gauss(0.0, 1.0)


# ---------------------------------------------------------------------------
# 价格模型（纯函数部分，不碰数据库，便于统计检验）
# ---------------------------------------------------------------------------


def theta_effective(deviation, half_life_steps):
    """软墙：偏离越接近 ±60% 回归越快；没有硬顶，价格仍可越过。"""
    z = abs(deviation) / BAND
    return (math.log(2) / half_life_steps) * (1 + WALL_K * max(0.0, z - WALL_FROM) ** 2)


def step_deviation(x, inventory_skew, randn, sigma, half_life_steps):
    """把 OU 状态推进一步（一步 = ``SLOT_SECONDS`` 秒），返回 ``(新的 x, 总偏离 u)``。

    ``half_life_steps`` 是与步长同单位的半衰期；σ 仍是稳态标准差，
    因此改步长只改粒度、不改日/周波动。
    """
    theta = theta_effective(x + inventory_skew, half_life_steps)
    decay = math.exp(-theta)
    x = x * decay + sigma * math.sqrt(1 - decay * decay) * randn
    return x, x + inventory_skew


def price_from(anchor_cents, deviation):
    return max(1, int(round(anchor_cents * math.exp(deviation))))


def inventory_skew(inventory_milli, cap_milli):
    """做市商净持仓的价格偏移；买入（库存转负）抬高价格。"""
    return -INVENTORY_COEF * inventory_milli / max(1, cap_milli)


def average_execution_multiplier(quantity_milli, side, cap_milli):
    """把一笔成交内的库存变化积分成平均价倍率。

    中心价是成交前的价格，成交过程中库存线性变化，因此平均价倍率是
    ``∫exp(±k·q/CAP)dq / q``；用 ``expm1`` 保持小额成交的数值精度。
    """
    ratio = INVENTORY_COEF * quantity_milli / max(1, cap_milli)
    if ratio <= 0:
        return 1.0
    return math.expm1(ratio) / ratio if side == "buy" else -math.expm1(-ratio) / ratio


# ---------------------------------------------------------------------------
# 标的行情状态
# ---------------------------------------------------------------------------


def _symbol_row(conn, symbol):
    row = conn.execute("SELECT anchor_cents,ou_state,ou_slot,price_cents,inventory_milli,"
                       "split_count,last_split_minute,announced_splits "
                       "FROM estate_market_symbols WHERE symbol=?", (symbol,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO estate_market_symbols"
                     "(symbol,name,anchor_cents,price_cents) VALUES (?,?,?,?)",
                     (symbol, symbol_name(symbol), float(DEFAULT_PRICE_CENTS),
                      DEFAULT_PRICE_CENTS))
        return (float(DEFAULT_PRICE_CENTS), 0.0, -1, DEFAULT_PRICE_CENTS, 0, 0, -1, 0)
    return row


def _inventory_milli(conn, symbol):
    row = conn.execute("SELECT inventory_milli FROM estate_market_symbols WHERE symbol=?",
                       (symbol,)).fetchone()
    return row[0] if row else 0


def base_price_cents(conn, symbol=DEFAULT_SYMBOL):
    """不含库存偏移的中价；额度与自然流都按它折算成份数。"""
    anchor, state = _symbol_row(conn, symbol)[:2]
    return price_from(anchor, state)


def cap_milli_for_price(price_cents):
    """把金币额度折算成份数：价格减半则份数翻倍，金币口径恒定。"""
    budget = INVENTORY_CAP_COINS // len(MARKET_SYMBOLS)
    return max(1000, budget * 100 * 1000 // max(1, price_cents))


def inventory_cap_milli(conn, symbol=DEFAULT_SYMBOL):
    return cap_milli_for_price(base_price_cents(conn, symbol))


def flow_cap_milli(conn, symbol=DEFAULT_SYMBOL):
    return max(1000, FLOW_PER_MINUTE_COINS * 100 * 1000
               // max(1, base_price_cents(conn, symbol)))


def tradable_milli(conn, side, symbol=DEFAULT_SYMBOL):
    """这一侧还能成交多少（milli 份）：只受做市商额度限制，没有涨跌停。"""
    inventory = _inventory_milli(conn, symbol)
    cap = inventory_cap_milli(conn, symbol)
    return max(0, cap + inventory if side == "buy" else cap - inventory)


def symbol_list(conn):
    """给前端的标的目录，附带各自最新报价。"""
    result = []
    for item in MARKET_SYMBOLS:
        row = conn.execute("SELECT price_cents FROM estate_market_symbols WHERE symbol=?",
                           (item["symbol"],)).fetchone()
        result.append({"symbol": item["symbol"], "name": item["name"],
                       "blurb": item["blurb"], "weekly_growth": item["weekly_growth"],
                       "price": (row[0] if row else DEFAULT_PRICE_CENTS) / 100})
    return result


# ---------------------------------------------------------------------------
# K 线
# ---------------------------------------------------------------------------


def record_market_candles(conn, symbol, minute, open_cents, close_cents):
    """按服务端每分钟采样构成 K 线；缺失的分钟不伪造行情。"""
    high = max(open_cents, close_cents)
    low = min(open_cents, close_cents)
    for period, start in (("minute", minute), ("hour", minute // 60 * 60),
                          ("day", (minute + 480) // 1440 * 1440 - 480)):
        conn.execute("INSERT INTO estate_market_candles "
                     "(symbol,period,start_minute,open_cents,high_cents,low_cents,close_cents) "
                     "VALUES (?,?,?,?,?,?,?) ON CONFLICT(symbol,period,start_minute) DO UPDATE SET "
                     "high_cents=MAX(high_cents,excluded.high_cents),"
                     "low_cents=MIN(low_cents,excluded.low_cents),"
                     "close_cents=excluded.close_cents",
                     (symbol, period, start, open_cents, high, low, close_cents))


def store_market_ticks(conn, symbol, rows):
    """批量写入 ``(minute, open, high, low, close)``，并同步三档 K 线与保留策略。"""
    if not rows:
        return
    conn.executemany("INSERT OR REPLACE INTO estate_market_ticks(symbol,minute,price_cents) "
                     "VALUES (?,?,?)",
                     [(symbol, minute, closing) for minute, _, _, _, closing in rows])
    candles = []
    for minute, opening, high, low, closing in rows:
        candles.append((symbol, "minute", minute, opening, high, low, closing))
        candles.append((symbol, "hour", minute // 60 * 60, opening, high, low, closing))
        candles.append((symbol, "day", (minute + 480) // 1440 * 1440 - 480,
                        opening, high, low, closing))
    conn.executemany("INSERT INTO estate_market_candles(symbol,period,start_minute,open_cents,"
                     "high_cents,low_cents,close_cents) VALUES (?,?,?,?,?,?,?) "
                     "ON CONFLICT(symbol,period,start_minute) DO UPDATE SET "
                     "high_cents=MAX(high_cents,excluded.high_cents),"
                     "low_cents=MIN(low_cents,excluded.low_cents),"
                     "close_cents=excluded.close_cents", candles)
    last = rows[-1][0]
    conn.execute("DELETE FROM estate_market_ticks WHERE symbol=? AND minute<?",
                 (symbol, last - 1440,))
    for period, age in (("minute", 2 * 1440), ("hour", 14 * 1440), ("day", 90 * 1440)):
        conn.execute("DELETE FROM estate_market_candles WHERE symbol=? AND period=? "
                     "AND start_minute<?", (symbol, period, last - age))


def _market_candles(conn, symbol):
    result = {}
    for period, limit in CANDLE_LIMITS.items():
        rows = conn.execute("SELECT start_minute,open_cents,high_cents,low_cents,close_cents,"
                            "volume_milli,volume_cents FROM estate_market_candles "
                            "WHERE symbol=? AND period=? ORDER BY start_minute DESC LIMIT ?",
                            (symbol, period, limit)).fetchall()
        result[period] = [
            {"time": start * 60, "open": opening / 100, "high": high / 100,
             "low": low / 100, "close": closing / 100,
             "volume": volume / 1000, "amount": amount / 100}
            for start, opening, high, low, closing, volume, amount in reversed(rows)]
    return result


def record_market_volume(conn, symbol, minute, qty_milli, amount_cents):
    """把一笔成交累加进三档 K 线的成交量与成交额。

    与报价不同，成交量是**累加**的：同一个分钟里多笔成交会叠加，
    而 ``store_market_ticks`` 的 upsert 只更新高低收，不会把量清零。
    """
    for period, start in (("minute", minute), ("hour", minute // 60 * 60),
                          ("day", (minute + 480) // 1440 * 1440 - 480)):
        conn.execute("INSERT INTO estate_market_candles(symbol,period,start_minute,open_cents,"
                     "high_cents,low_cents,close_cents,volume_milli,volume_cents) "
                     "VALUES (?,?,?,0,0,0,0,?,?) ON CONFLICT(symbol,period,start_minute) "
                     "DO UPDATE SET volume_milli=volume_milli+excluded.volume_milli,"
                     "volume_cents=volume_cents+excluded.volume_cents",
                     (symbol, period, start, qty_milli, amount_cents))


def market_volume(conn, symbol, minute):
    """本分钟与今日的成交量（份）与成交额（金币）。"""
    day_start = (minute + 480) // 1440 * 1440 - 480
    row = conn.execute("SELECT volume_milli,volume_cents FROM estate_market_candles "
                       "WHERE symbol=? AND period='day' AND start_minute=?",
                       (symbol, day_start)).fetchone()
    day = row or (0, 0)
    row = conn.execute("SELECT volume_milli,volume_cents FROM estate_market_candles "
                       "WHERE symbol=? AND period='minute' AND start_minute=?",
                       (symbol, minute)).fetchone()
    current = row or (0, 0)
    return {"minute": {"shares": current[0] / 1000, "amount": current[1] / 100},
            "day": {"shares": day[0] / 1000, "amount": day[1] / 100}}


def _split_market(conn, symbol, minute, anchor_cents, price_cents):
    """1:2 拆股：份额 ×2、所有价格 ÷2；金币金额、持仓成本与相对冲击不变。

    锚与报价以参数传入（数据库里还是上一分钟的旧值），返回拆股后的两者。
    """
    factor = SPLIT_FACTOR
    anchor_cents /= factor
    price_cents = max(1, int(round(price_cents / factor)))

    def halve(column):
        return f"MAX(1,CAST(ROUND({column}/{factor}) AS INTEGER))"

    conn.execute("UPDATE estate_market_symbols SET anchor_cents=?,price_cents=?,"
                 "inventory_milli=inventory_milli*?,flow_buy_milli=flow_buy_milli*?,"
                 "flow_sell_milli=flow_sell_milli*?,split_count=split_count+1,"
                 "last_split_minute=? WHERE symbol=?",
                 (anchor_cents, price_cents, factor, factor, factor, minute, symbol))
    conn.execute("UPDATE estate_market_positions SET shares_milli=shares_milli*? WHERE symbol=?",
                 (factor, symbol))
    conn.execute(f"UPDATE estate_market_ticks SET price_cents={halve('price_cents')} "
                 "WHERE symbol=?", (symbol,))
    conn.execute("UPDATE estate_market_candles SET "
                 f"open_cents={halve('open_cents')},high_cents={halve('high_cents')},"
                 f"low_cents={halve('low_cents')},close_cents={halve('close_cents')},"
                 "volume_milli=volume_milli*? WHERE symbol=?", (factor, symbol))
    conn.execute(f"UPDATE estate_market_fills SET price_cents={halve('price_cents')},"
                 "qty_milli=qty_milli*? WHERE symbol=?", (factor, symbol))
    # 拆股后价格网格会出现 0.5 元，未成交委托无法平移，统一撤销并说明原因。
    conn.execute("UPDATE estate_market_orders SET status='cancelled',"
                 "reason='因 1:2 拆股自动撤销' WHERE status='open' AND symbol=?", (symbol,))
    return anchor_cents, price_cents


def _advance_symbol(conn, item, slot):
    """把单只标的推进到指定时间格，返回该格的报价（分）。

    每一步只走 ``SLOT_SECONDS`` 秒，K 线在落库前按分钟聚合成开高低收，
    所以提高撮合频率不会改变日/周波动，也不会改变图表粒度。
    """
    symbol = item["symbol"]
    anchor, state, state_slot, price_cents, inventory = _symbol_row(conn, symbol)[:5]
    if state_slot >= 0 and slot <= state_slot:
        return price_cents
    skew = inventory_skew(inventory, inventory_cap_milli(conn, symbol))
    elapsed = slot - state_slot if state_slot >= 0 else 0
    if elapsed > 0 and budget_room(conn, slot * SLOT_SECONDS):
        anchor *= (1 + item["weekly_growth"]) ** (elapsed / SLOTS_PER_WEEK)
    if state_slot < 0 or elapsed > MAX_BACKFILL_SLOTS:
        # 首次采样或长时间停机：从稳态重抽情绪，停机期间的 K 线留空不补造。
        state = _randn() * item["sigma"]
        slots = [slot]
    else:
        slots = range(state_slot + 1, slot + 1)
    half_life_steps = item["half_life_minutes"] * SLOTS_PER_MINUTE
    opening = price_cents
    minutes = {}
    for step_slot in slots:
        state, deviation = step_deviation(state, skew, _randn(), item["sigma"],
                                          half_life_steps)
        price_cents = price_from(anchor, deviation)
        minute = step_slot * SLOT_SECONDS // 60
        bars = minutes.get(minute)
        if bars is None:
            minutes[minute] = [opening, price_cents, price_cents, price_cents]
        else:
            bars[1] = max(bars[1], price_cents)
            bars[2] = min(bars[2], price_cents)
            bars[3] = price_cents
        opening = price_cents
    store_market_ticks(conn, symbol, [(minute, *bars)
                                      for minute, bars in sorted(minutes.items())])
    # 拆股放在写完之后：它要把这一格刚写下的 K 线一起折算，否则会被旧的价位盖回去。
    while anchor >= SPLIT_AT_CENTS:
        anchor, price_cents = _split_market(conn, symbol, slot * SLOT_SECONDS // 60,
                                            anchor, price_cents)
    conn.execute("UPDATE estate_market_symbols SET anchor_cents=?,ou_state=?,ou_slot=?,"
                 "price_cents=?,available=1 WHERE symbol=?",
                 (anchor, state, slot, price_cents, symbol))
    return price_cents


def advance_market(conn, now):
    """把所有标的推进到 ``now`` 所在的时间格，返回 ``{symbol: 报价分}``。"""
    slot = int(now) // SLOT_SECONDS
    return {item["symbol"]: _advance_symbol(conn, item, slot) for item in MARKET_SYMBOLS}


# ---------------------------------------------------------------------------
# 印钞预算（全市场共享）
# ---------------------------------------------------------------------------


def money_supply_cents(conn):
    """货币供应 = 流通金币 + 市场托管（持仓成本）。"""
    coins = conn.execute("SELECT COALESCE(SUM(coins),0) FROM users").fetchone()[0] or 0
    basis = conn.execute("SELECT COALESCE(SUM(cost_basis_cents),0) "
                         "FROM estate_market_positions").fetchone()[0] or 0
    return int(round(float(coins) * 100)) + int(basis)


def market_day(minute):
    """按北京时间 0 点换日，与日 K 线口径一致。"""
    return (int(minute) + 480) // 1440


def budget_limit_cents(conn):
    weeks = BUDGET_WINDOW_DAYS // 7
    return int(Decimal(money_supply_cents(conn)) * WEEKLY_BUDGET_RATE * weeks)


def budget_room(conn, now):
    """滚动窗口内还有印钞额度时，锚才继续上移；``now`` 是秒级时间戳。"""
    used = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM estate_market_printed "
                        "WHERE day>?", (market_day(int(now) // 60) - BUDGET_WINDOW_DAYS,)
                        ).fetchone()[0]
    return used < budget_limit_cents(conn)


def record_printed(conn, now, delta_cents):
    """按天累计净印钞：卖出为增发、买入为销毁，两者相抵才是真实通胀。"""
    conn.execute("INSERT INTO estate_market_printed(day,amount_cents) VALUES (?,?) "
                 "ON CONFLICT(day) DO UPDATE SET "
                 "amount_cents=amount_cents+excluded.amount_cents",
                 (market_day(int(now) // 60), int(delta_cents)))


# ---------------------------------------------------------------------------
# 读模型
# ---------------------------------------------------------------------------


def _position(conn, username, symbol):
    row = conn.execute(
        "SELECT shares_milli,cost_basis_cents,realized_pnl_cents "
        "FROM estate_market_positions WHERE username=? AND symbol=?", (username, symbol),
    ).fetchone()
    return row or (0, 0, 0)


def orders_of(conn, username, symbol):
    rows = conn.execute(
        "SELECT id,side,price_cents,qty_milli,filled_milli,expires_minute "
        "FROM estate_market_orders WHERE username=? AND symbol=? AND status='open' "
        "ORDER BY id DESC LIMIT ?", (username, symbol, MAX_OPEN_ORDERS)).fetchall()
    return [{"id": row[0], "side": row[1], "price": row[2] / 100,
             "quantity": row[3] / 1000, "filled": row[4] / 1000,
             "remaining": (row[3] - row[4]) / 1000, "expires_minute": row[5]} for row in rows]


def public_positions(conn, price_cents, symbol):
    """全服持仓榜：持仓完全公开，按市值（即份额）从大到小。

    只列还持有份额的账号；成本、浮动盈亏与已实现盈亏一并公开。
    """
    rows = conn.execute(
        "SELECT username,shares_milli,cost_basis_cents,realized_pnl_cents "
        "FROM estate_market_positions WHERE symbol=? AND shares_milli>0 "
        "ORDER BY shares_milli DESC,username LIMIT 100", (symbol,)).fetchall()
    return [{"username": row[0], "shares": row[1] / 1000,
             "market_value": round(price_cents * row[1] / 100000, 2),
             "cost_basis": row[2] / 100,
             "unrealized_pnl": round(price_cents * row[1] / 100000 - row[2] / 100, 2),
             "realized_pnl": row[3] / 100} for row in rows]


def recent_fills(conn, symbol, username=None, limit=20):
    """成交流水；``username`` 为空时给该标的的全服明细（做市商始终匿名）。"""
    if username is None:
        rows = conn.execute(
            "SELECT minute,username,side,price_cents,qty_milli,amount_cents "
            "FROM estate_market_fills WHERE symbol=? ORDER BY id DESC LIMIT ?",
            (symbol, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT minute,username,side,price_cents,qty_milli,amount_cents "
            "FROM estate_market_fills WHERE symbol=? AND username=? ORDER BY id DESC LIMIT ?",
            (symbol, username, limit)).fetchall()
    return [{"time": row[0] * 60, "username": row[1], "side": row[2],
             "price": row[3] / 100, "quantity": row[4] / 1000, "amount": row[5] / 100}
            for row in rows]


def market_snapshot(conn, username, now, adjust_coins, symbol=DEFAULT_SYMBOL):
    """读快照前先推进全部标的并结算到期的挂单；行情面板与交易都走这一个入口。"""
    symbol = symbol_or_default(symbol)
    prices = refresh_market(conn, now, adjust_coins)
    price_cents = prices[symbol]
    anchor, _, _, _, inventory, splits, last_split, _ = _symbol_row(conn, symbol)
    cap = inventory_cap_milli(conn, symbol)
    shares, basis, realized = _position(conn, username, symbol)
    minute = int(now) // 60
    history = conn.execute(
        "SELECT minute,price_cents FROM estate_market_ticks WHERE symbol=? AND minute>=? "
        "ORDER BY minute DESC LIMIT 120", (symbol, minute - 119),
    ).fetchall()
    detail = SYMBOL_INFO[symbol]
    return {"symbol": symbol, "name": detail["name"], "blurb": detail["blurb"],
            "symbols": symbol_list(conn),
            "price": price_cents / 100, "anchor": round(anchor / 100, 2),
            "book": market_book(conn, price_cents, symbol, now),
            "flow_left": {"buy": flow_left(conn, "buy", minute, symbol) / 1000,
                          "sell": flow_left(conn, "sell", minute, symbol) / 1000},
            "orders": orders_of(conn, username, symbol),
            "fills": recent_fills(conn, symbol),
            "my_fills": recent_fills(conn, symbol, username),
            "positions": public_positions(conn, price_cents, symbol),
            "coins": (conn.execute("SELECT coins FROM users WHERE username=?",
                                   (username,)).fetchone() or [0])[0],
            "quote_minute": minute, "quote_slot": int(now) // SLOT_SECONDS,
            "available": True, "fee_rate": float(TAKER_FEE_RATE),
            "maker_fee_rate": float(MAKER_FEE_RATE),
            "volume": market_volume(conn, symbol, minute),
            "capacity_left": (cap - abs(inventory)) / 1000,
            "split_count": splits, "last_split_minute": last_split,
            "tradable_buy": tradable_milli(conn, "buy", symbol) / 1000,
            "tradable_sell": tradable_milli(conn, "sell", symbol) / 1000,
            "trend_paused": not budget_room(conn, now),
            "shares": shares / 1000, "cost_basis": basis / 100,
            "market_value": round(price_cents * shares / 100000, 2),
            "realized_pnl": realized / 100,
            "history": [{"time": entry * 60, "price": value / 100}
                        for entry, value in reversed(history)],
            "candles": _market_candles(conn, symbol)}


# ---------------------------------------------------------------------------
# 自然盘与挂单
#
# 三处刻意的最小化实现，见 docs/stock-market-design.md §11.2：
#   1. 自然盘不落库，是当前报价的纯函数，因此没有挂单重建/过期；
#   2. 挂单不冻结资金与份额，成交时校验，不足则撤单；
#   3. 不做玩家之间的撮合，对手方始终是做市商。
# ---------------------------------------------------------------------------


def natural_quotes(price_cents):
    """自然盘：现价上下各 5 档、档距 1 元，量 1/2/3/6/8 份（每侧 20 份/分钟）。"""
    base = price_cents // 100
    return {"bids": [{"price": base - offset, "quantity": quantity}
                     for offset, quantity in NATURAL_LEVELS],
            "asks": [{"price": base + offset, "quantity": quantity}
                     for offset, quantity in NATURAL_LEVELS]}


def market_book(conn, price_cents, symbol=DEFAULT_SYMBOL, now=None):
    """盘口：自然盘报价按做市商剩余额度与**本分钟剩余自然流**双重裁剪。

    两边都是玩家点下去会真的被拒的硬约束，所以盘口不能显示超出它们的量：
    额度吃满、或这一分钟的自然流被别人用掉之后，对应的一侧就不再挂档。
    """
    minute = int(now) // 60 if now is not None else None
    book = natural_quotes(price_cents)
    for side, key in (("buy", "asks"), ("sell", "bids")):
        room = tradable_milli(conn, side, symbol)
        if minute is not None:
            room = min(room, flow_left(conn, side, minute, symbol))
        levels = []
        for level in book[key]:
            quantity = min(level["quantity"] * 1000, room)
            if quantity <= 0:
                break
            levels.append({"price": level["price"], "quantity": quantity / 1000})
            room -= quantity
        book[key] = levels
    return book


def _flow_used(conn, side, minute, symbol):
    row = conn.execute("SELECT flow_minute,flow_buy_milli,flow_sell_milli "
                       "FROM estate_market_symbols WHERE symbol=?", (symbol,)).fetchone()
    if row is None or row[0] != minute:
        return 0
    return row[1] if side == "buy" else row[2]


def flow_left(conn, side, minute, symbol=DEFAULT_SYMBOL):
    """本分钟这一侧还剩多少自然流（milli 份）；按金币口径折算，拆股不影响。"""
    return max(0, flow_cap_milli(conn, symbol) - _flow_used(conn, side, minute, symbol))


def _spend_flow(conn, symbol, side, amount_milli):
    column = "flow_buy_milli" if side == "buy" else "flow_sell_milli"
    conn.execute(f"UPDATE estate_market_symbols SET {column}={column}+? WHERE symbol=?",
                 (amount_milli, symbol))


def _record_fill(conn, symbol, username, side, minute, price_cents, amount_milli, cents,
                 order_id):
    conn.execute("INSERT INTO estate_market_fills(symbol,username,side,minute,price_cents,"
                 "qty_milli,amount_cents,order_id) VALUES (?,?,?,?,?,?,?,?)",
                 (symbol, username, side, minute, price_cents, amount_milli, cents, order_id))


def apply_fill(conn, symbol, username, side, amount_milli, price_cents, now, adjust_coins,
               request_id, order_id=None, maker=False):
    """一笔成交的全部账务：钱包、持仓、做市商库存、报价、净印钞与成交流水。

    成交均价按库存变化积分（``average_execution_multiplier``），因此大额成交
    会自己把价格推走，同量买卖只亏手续费。
    """
    label = symbol_name(symbol)
    cap = inventory_cap_milli(conn, symbol)
    multiplier = average_execution_multiplier(amount_milli, side, cap)
    notional = (Decimal(price_cents) * Decimal(amount_milli) / 1000
                * Decimal(repr(multiplier)))
    fee_rate = MAKER_FEE_RATE if maker else TAKER_FEE_RATE
    shares, basis, realized = _position(conn, username, symbol)
    inventory = _inventory_milli(conn, symbol)
    if side == "buy":
        cents = int((notional * (1 + fee_rate)).to_integral_value(rounding=ROUND_CEILING))
        balance = debit(adjust_coins, conn, username, cents / 100,
                        f"{label}买入 {amount_milli / 1000:g} 份", request_id)
        shares += amount_milli
        basis += cents
        inventory -= amount_milli
    else:
        if amount_milli > shares:
            raise estate_error(("market_shares", "持有份额不足"))
        cents = int((notional * (1 - fee_rate)).to_integral_value(rounding=ROUND_FLOOR))
        removed = basis if amount_milli == shares else basis * amount_milli // shares
        balance = credit(adjust_coins, conn, username, cents / 100,
                         f"{label}卖出 {amount_milli / 1000:g} 份", request_id)
        shares -= amount_milli
        basis -= removed
        realized += cents - removed
        inventory += amount_milli
    conn.execute(
        "INSERT INTO estate_market_positions"
        "(username,symbol,shares_milli,cost_basis_cents,realized_pnl_cents) "
        "VALUES (?,?,?,?,?) ON CONFLICT(username,symbol) DO UPDATE SET "
        "shares_milli=excluded.shares_milli,cost_basis_cents=excluded.cost_basis_cents,"
        "realized_pnl_cents=excluded.realized_pnl_cents",
        (username, symbol, shares, basis, realized),
    )
    conn.execute("UPDATE estate_market_symbols SET inventory_milli=? WHERE symbol=?",
                 (inventory, symbol))
    record_printed(conn, now, cents if side == "sell" else -cents)
    anchor, state = _symbol_row(conn, symbol)[:2]
    moved = price_from(anchor, state + inventory_skew(inventory,
                                                      inventory_cap_milli(conn, symbol)))
    conn.execute("UPDATE estate_market_symbols SET price_cents=? WHERE symbol=?", (moved, symbol))
    store_market_ticks(conn, symbol, [(int(now) // 60, price_cents,
                                       max(price_cents, moved), min(price_cents, moved), moved)])
    _record_fill(conn, symbol, username, side, int(now) // 60, price_cents, amount_milli, cents,
                 order_id)
    record_market_volume(conn, symbol, int(now) // 60, amount_milli, cents)
    return {"cents": cents, "balance": balance, "price": moved,
            "average_price": float(notional / Decimal(amount_milli) * 1000 / 100)}


def _order_price_cents(value):
    raw = str(value or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        raise estate_error(("market_price", "挂单价必须是不小于 1 元的整数"))
    return int(raw) * 100


def _available_shares(conn, username, symbol):
    """可用份额 = 持仓 − 未成交卖单剩余量（不冻结，只做占用统计）。"""
    shares = _position(conn, username, symbol)[0]
    reserved = conn.execute(
        "SELECT COALESCE(SUM(qty_milli - filled_milli),0) FROM estate_market_orders "
        "WHERE username=? AND symbol=? AND side='sell' AND status='open'",
        (username, symbol)).fetchone()[0]
    return shares - reserved


def _check_order_capacity(conn, username, side, limit_cents, amount_milli, symbol):
    if side == "sell":
        if amount_milli > _available_shares(conn, username, symbol):
            raise estate_error(("market_shares", "可用份额不足（已挂出的卖单会占用份额）"))
        return
    needed = int((Decimal(limit_cents) * Decimal(amount_milli) / 1000
                  * (1 + TAKER_FEE_RATE)).to_integral_value(rounding=ROUND_CEILING))
    balance = conn.execute("SELECT coins FROM users WHERE username=?", (username,)).fetchone()
    if balance is None or int(round(float(balance[0]) * 100)) < needed:
        raise estate_error(("insufficient_coins", "金币不足"))


def _next_marketable_order(conn, symbol, side, price_cents):
    comparison = ">=" if side == "buy" else "<="
    ordering = "price_cents DESC,id" if side == "buy" else "price_cents ASC,id"
    return conn.execute(
        "SELECT id,username,price_cents,qty_milli,filled_milli FROM estate_market_orders "
        f"WHERE status='open' AND symbol=? AND side=? AND price_cents{comparison}? "
        f"ORDER BY {ordering} LIMIT 1", (symbol, side, price_cents)).fetchone()


def _fill_order(conn, symbol, order, side, quantity_milli, price_cents, minute, adjust_coins):
    order_id, username, _, quantity_milli_total, filled_milli = order
    try:
        result = apply_fill(conn, symbol, username, side, quantity_milli, price_cents, minute * 60,
                            adjust_coins, f"market-order-{order_id}", order_id, maker=True)
    except EstateError as error:
        # 余额或份额不足：只撤掉这一单，不影响同一分钟里的其它委托。
        conn.execute("UPDATE estate_market_orders SET status='cancelled',reason=? WHERE id=?",
                     (error.message, order_id))
        return price_cents, False
    filled = filled_milli + quantity_milli
    conn.execute("UPDATE estate_market_orders SET filled_milli=?,status=? WHERE id=?",
                 (filled, "filled" if filled >= quantity_milli_total else "open", order_id))
    _spend_flow(conn, symbol, side, quantity_milli)
    return result["price"], True


def expire_orders(conn, minute):
    conn.execute("UPDATE estate_market_orders SET status='expired',reason='超过 24 小时未成交' "
                 "WHERE status='open' AND expires_minute<=?", (minute,))


def settle_orders(conn, symbol, now, price_cents, adjust_coins):
    """每 ``SLOT_SECONDS`` 秒结算一次：按价格优先把可成交的挂单成交掉。

    结算频率是 10 秒，但自然流预算仍按**分钟**计（每分钟 2 万金币），
    所以提高频率只让挂单成交更及时，不会放大吞吐。
    """
    slot = int(now) // SLOT_SECONDS
    minute = int(now) // 60
    row = conn.execute("SELECT settled_slot,flow_minute FROM estate_market_symbols "
                       "WHERE symbol=?", (symbol,)).fetchone()
    if row is None or row[0] == slot:
        return price_cents
    if row[1] == minute:
        conn.execute("UPDATE estate_market_symbols SET settled_slot=? WHERE symbol=?",
                     (slot, symbol))
    else:
        conn.execute("UPDATE estate_market_symbols SET settled_slot=?,flow_minute=?,"
                     "flow_buy_milli=0,flow_sell_milli=0 WHERE symbol=?", (slot, minute, symbol))
    for side in ("buy", "sell"):
        while True:
            budget = min(flow_left(conn, side, minute, symbol),
                         tradable_milli(conn, side, symbol))
            if budget <= 0:
                break
            order = _next_marketable_order(conn, symbol, side, price_cents)
            if order is None:
                break
            quantity = min(order[3] - order[4], budget)
            price_cents, _ = _fill_order(conn, symbol, order, side, quantity, price_cents,
                                         minute, adjust_coins)
    return price_cents


def refresh_market(conn, now, adjust_coins):
    """宿主在每次访问行情前调用：推进全部标的并结算各自到期的挂单。"""
    prices = advance_market(conn, now)
    expire_orders(conn, int(now) // 60)
    conn.execute("DELETE FROM estate_market_fills WHERE id<?",
                 (conn.execute("SELECT COALESCE(MAX(id),0) FROM estate_market_fills").fetchone()[0]
                  - FILL_HISTORY_LIMIT,))
    for item in MARKET_SYMBOLS:
        symbol = item["symbol"]
        prices[symbol] = settle_orders(conn, symbol, now, prices[symbol], adjust_coins)
    return prices


# ---------------------------------------------------------------------------
# 交易
# ---------------------------------------------------------------------------


def _quantity_milli(value):
    raw = str(value or "").strip()
    if not QUANTITY_PATTERN.fullmatch(raw):
        raise estate_error(("market_quantity", "请输入最多三位小数的交易份额"))
    quantity = int(Decimal(raw) * 1000)
    if not 1 <= quantity <= 1_000_000_000:
        raise estate_error(("market_quantity", "交易份额超出允许范围"))
    return quantity


def _liquidity_error(side):
    action = "买入" if side == "buy" else "卖出"
    return estate_error(("market_no_liquidity", f"做市商额度已用尽，暂时无法{action}"))


def _flow_error(conn, side, symbol):
    action = "买入" if side == "buy" else "卖出"
    cap = flow_cap_milli(conn, symbol) / 1000
    return estate_error(("market_flow", f"本分钟自然流已用尽（每分钟最多 {cap:g} 份），"
                                       f"请改用限价委托分批{action}"))


def _side_of(value):
    side = str(value or "")
    if side not in ("buy", "sell"):
        raise estate_error(("market_side", "交易方向无效"))
    return side


def trade_market(conn, username, request_id, side, quantity, now, adjust_coins,
                 symbol=DEFAULT_SYMBOL):
    """市价成交：只吃该标本分钟剩余的自然流，超出请改用限价委托。"""
    side = _side_of(side)
    symbol = symbol_or_default(symbol)
    amount_milli = _quantity_milli(quantity)

    def mutate():
        prices = refresh_market(conn, now, adjust_coins)
        price_cents = prices[symbol]
        minute = int(now) // 60
        if amount_milli > tradable_milli(conn, side, symbol):
            raise _liquidity_error(side)
        if amount_milli > flow_left(conn, side, minute, symbol) * (1 + FLOW_TOLERANCE):
            raise _flow_error(conn, side, symbol)
        result = apply_fill(conn, symbol, username, side, amount_milli, price_cents, now,
                            adjust_coins, request_id)
        _spend_flow(conn, symbol, side, amount_milli)
        return {"action": "market_trade", "symbol": symbol, "side": side,
                "quantity": amount_milli / 1000,
                "price": price_cents / 100, "average_price": result["average_price"],
                "amount": result["cents"] / 100, "coins": result["balance"],
                "market": market_snapshot(conn, username, now, adjust_coins, symbol)}

    return run_action(conn, username, request_id, "market_trade",
                      {"symbol": symbol, "side": side, "quantity_milli": amount_milli},
                      now, mutate)


def place_order(conn, username, request_id, side, price, quantity, now, adjust_coins,
                symbol=DEFAULT_SYMBOL):
    """限价委托：可成交部分立即成交，剩余挂簿，由每分钟的自然流分批成交。"""
    side = _side_of(side)
    symbol = symbol_or_default(symbol)
    amount_milli = _quantity_milli(quantity)
    limit_cents = _order_price_cents(price)

    def mutate():
        prices = refresh_market(conn, now, adjust_coins)
        price_cents = prices[symbol]
        minute = int(now) // 60
        if abs(limit_cents - price_cents) > ORDER_WINDOW_CENTS:
            raise estate_error(("market_price_band",
                                f"挂单价必须在现价上下 {ORDER_WINDOW_CENTS // 100} 元以内"))
        open_orders = conn.execute("SELECT COUNT(*) FROM estate_market_orders "
                                   "WHERE username=? AND status='open'",
                                   (username,)).fetchone()[0]
        if open_orders >= MAX_OPEN_ORDERS:
            raise estate_error(("market_order_limit", f"最多同时挂 {MAX_OPEN_ORDERS} 张委托"))
        marketable = limit_cents >= price_cents if side == "buy" else limit_cents <= price_cents
        filled_milli = 0
        if marketable:
            usable = min(amount_milli, flow_left(conn, side, minute, symbol),
                         tradable_milli(conn, side, symbol))
            if usable > 0:
                apply_fill(conn, symbol, username, side, usable, price_cents, now, adjust_coins,
                           f"market-order-{request_id}")
                _spend_flow(conn, symbol, side, usable)
                filled_milli = usable
        remaining = amount_milli - filled_milli
        if remaining:
            _check_order_capacity(conn, username, side, limit_cents, remaining, symbol)
            conn.execute(
                "INSERT INTO estate_market_orders"
                "(username,symbol,side,price_cents,qty_milli,filled_milli,status,created_minute,"
                "expires_minute) VALUES (?,?,?,?,?,?,'open',?,?)",
                (username, symbol, side, limit_cents, amount_milli, filled_milli, minute,
                 minute + ORDER_TTL_MINUTES))
        return {"action": "market_order", "symbol": symbol, "side": side,
                "price": limit_cents / 100, "quantity": amount_milli / 1000,
                "filled": filled_milli / 1000,
                "market": market_snapshot(conn, username, now, adjust_coins, symbol)}

    return run_action(conn, username, request_id, "market_order",
                      {"symbol": symbol, "side": side, "price_cents": limit_cents,
                       "quantity_milli": amount_milli}, now, mutate)


def cancel_order(conn, username, request_id, order_id, now, adjust_coins):
    def mutate():
        row = conn.execute("SELECT id,symbol FROM estate_market_orders WHERE id=? AND username=? "
                           "AND status='open'", (order_id, username)).fetchone()
        if row is None:
            raise estate_error(("market_order_missing", "委托单不存在或已结束"))
        conn.execute("UPDATE estate_market_orders SET status='cancelled',reason='玩家撤单' "
                     "WHERE id=?", (row[0],))
        return {"action": "market_cancel", "order_id": row[0], "symbol": row[1],
                "market": market_snapshot(conn, username, now, adjust_coins, row[1])}

    return run_action(conn, username, request_id, "market_cancel", {"order_id": order_id},
                      now, mutate)
