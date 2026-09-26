"""休闲庄园 SQLite 表结构。"""


def init_estate(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS estate_profiles (
            username TEXT PRIMARY KEY COLLATE NOCASE,
            skin_id TEXT NOT NULL DEFAULT 'berry',
            level INTEGER NOT NULL DEFAULT 1 CHECK(level >= 1),
            xp INTEGER NOT NULL DEFAULT 0 CHECK(xp >= 0),
            warehouse_level INTEGER NOT NULL DEFAULT 1 CHECK(warehouse_level >= 1),
            plot_count INTEGER NOT NULL DEFAULT 0 CHECK(plot_count >= 0),
            reserved_capacity INTEGER NOT NULL DEFAULT 0 CHECK(reserved_capacity >= 0),
            pet_level INTEGER NOT NULL DEFAULT 0 CHECK(pet_level BETWEEN 0 AND 4),
            penguin_level INTEGER NOT NULL DEFAULT 0 CHECK(penguin_level BETWEEN 0 AND 4),
            penguin_active_at INTEGER NOT NULL DEFAULT 0,
            maodie_level INTEGER NOT NULL DEFAULT 0 CHECK(maodie_level BETWEEN 0 AND 4),
            maodie_last_at INTEGER NOT NULL DEFAULT 0,
            active_pet TEXT NOT NULL DEFAULT 'doudou' CHECK(active_pet IN ('doudou','stinky_penguin','maodie')),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """)
    conn.execute("""CREATE TABLE IF NOT EXISTS estate_owned_skins (
        username TEXT NOT NULL COLLATE NOCASE, skin_id TEXT NOT NULL,
        PRIMARY KEY(username, skin_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS estate_collections (
        username TEXT NOT NULL COLLATE NOCASE, item_id TEXT NOT NULL,
        PRIMARY KEY(username, item_id))""")
    profile_columns = {row[1] for row in conn.execute("PRAGMA table_info(estate_profiles)")}
    if "skin_id" not in profile_columns:
        conn.execute("ALTER TABLE estate_profiles ADD COLUMN skin_id TEXT NOT NULL DEFAULT 'berry'")
    if "reserved_capacity" not in profile_columns:
        conn.execute("ALTER TABLE estate_profiles ADD COLUMN reserved_capacity INTEGER NOT NULL DEFAULT 0")
    if "pet_level" not in profile_columns:
        conn.execute("ALTER TABLE estate_profiles ADD COLUMN pet_level INTEGER NOT NULL DEFAULT 0")
    if "penguin_level" not in profile_columns:
        conn.execute("ALTER TABLE estate_profiles ADD COLUMN penguin_level INTEGER NOT NULL DEFAULT 0")
    if "penguin_active_at" not in profile_columns:
        conn.execute("ALTER TABLE estate_profiles ADD COLUMN penguin_active_at INTEGER NOT NULL DEFAULT 0")
    if "active_pet" not in profile_columns:
        conn.execute("ALTER TABLE estate_profiles ADD COLUMN active_pet TEXT NOT NULL DEFAULT 'doudou'")
        conn.execute("UPDATE estate_profiles SET active_pet='stinky_penguin' WHERE penguin_level>0")
    if "maodie_level" not in profile_columns:
        conn.execute("ALTER TABLE estate_profiles ADD COLUMN maodie_level INTEGER NOT NULL DEFAULT 0")
    if "maodie_last_at" not in profile_columns:
        conn.execute("ALTER TABLE estate_profiles ADD COLUMN maodie_last_at INTEGER NOT NULL DEFAULT 0")
    profile_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='estate_profiles'").fetchone()[0]
    if "'maodie'" not in profile_sql:
        # 旧表的 active_pet CHECK 不接受新宠物；SQLite 需重建该表。
        conn.execute("CREATE TABLE estate_profiles_new ("
                     "username TEXT PRIMARY KEY COLLATE NOCASE, skin_id TEXT NOT NULL DEFAULT 'berry',"
                     "level INTEGER NOT NULL DEFAULT 1 CHECK(level >= 1),"
                     "xp INTEGER NOT NULL DEFAULT 0 CHECK(xp >= 0),"
                     "warehouse_level INTEGER NOT NULL DEFAULT 1 CHECK(warehouse_level >= 1),"
                     "plot_count INTEGER NOT NULL DEFAULT 0 CHECK(plot_count >= 0),"
                     "reserved_capacity INTEGER NOT NULL DEFAULT 0 CHECK(reserved_capacity >= 0),"
                     "pet_level INTEGER NOT NULL DEFAULT 0 CHECK(pet_level BETWEEN 0 AND 4),"
                     "penguin_level INTEGER NOT NULL DEFAULT 0 CHECK(penguin_level BETWEEN 0 AND 4),"
                     "penguin_active_at INTEGER NOT NULL DEFAULT 0,"
                     "maodie_level INTEGER NOT NULL DEFAULT 0 CHECK(maodie_level BETWEEN 0 AND 4),"
                     "maodie_last_at INTEGER NOT NULL DEFAULT 0,"
                     "active_pet TEXT NOT NULL DEFAULT 'doudou' CHECK(active_pet IN "
                     "('doudou','stinky_penguin','maodie')),"
                     "version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),"
                     "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)")
        columns = ("username,skin_id,level,xp,warehouse_level,plot_count,reserved_capacity,"
                   "pet_level,penguin_level,penguin_active_at,maodie_level,maodie_last_at,"
                   "active_pet,version,created_at,updated_at")
        conn.execute(f"INSERT INTO estate_profiles_new({columns}) SELECT {columns} FROM estate_profiles")
        conn.execute("DROP TABLE estate_profiles")
        conn.execute("ALTER TABLE estate_profiles_new RENAME TO estate_profiles")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS estate_plots (
            username TEXT NOT NULL COLLATE NOCASE,
            plot_index INTEGER NOT NULL CHECK(plot_index >= 0),
            land_level INTEGER NOT NULL DEFAULT 1 CHECK(land_level >= 1),
            crop_id TEXT,
            planted_at INTEGER,
            ready_at INTEGER,
            PRIMARY KEY (username, plot_index),
            CHECK (
                (crop_id IS NULL AND planted_at IS NULL AND ready_at IS NULL) OR
                (crop_id IS NOT NULL AND planted_at IS NOT NULL AND ready_at IS NOT NULL
                 AND ready_at >= planted_at)
            )
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS estate_inventory (
            username TEXT NOT NULL COLLATE NOCASE,
            item_id TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            PRIMARY KEY (username, item_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS estate_actions (
            username TEXT NOT NULL COLLATE NOCASE,
            request_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (username, request_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_estate_actions_created "
        "ON estate_actions(username, created_at)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS estate_tools (
            username TEXT NOT NULL COLLATE NOCASE,
            tool_type TEXT NOT NULL,
            level INTEGER NOT NULL CHECK(level >= 1),
            durability INTEGER NOT NULL CHECK(durability >= 0),
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (username, tool_type)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS estate_tool_daily (
            username TEXT NOT NULL COLLATE NOCASE,
            tool_type TEXT NOT NULL,
            refill_day TEXT NOT NULL DEFAULT '',
            repair_day TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (username, tool_type)
        )
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS delete_estate_tool_daily
        AFTER DELETE ON estate_tools
        BEGIN
            DELETE FROM estate_tool_daily
            WHERE username=OLD.username AND tool_type=OLD.tool_type;
        END
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS estate_fishing_sessions (
            session_id TEXT PRIMARY KEY,
            username TEXT NOT NULL COLLATE NOCASE,
            bait_id TEXT NOT NULL,
            rod_level INTEGER NOT NULL,
            fish_id TEXT NOT NULL,
            seed INTEGER NOT NULL,
            pattern_json TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            result_json TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS estate_mining_runs (
            run_id TEXT PRIMARY KEY,
            username TEXT NOT NULL COLLATE NOCASE,
            mine_level INTEGER NOT NULL,
            pickaxe_level INTEGER NOT NULL,
            seed INTEGER NOT NULL,
            board_json TEXT NOT NULL,
            revealed_json TEXT NOT NULL DEFAULT '[]',
            loot_json TEXT NOT NULL DEFAULT '{}',
            strikes_left INTEGER NOT NULL,
            started_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            result_json TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fishing_user ON estate_fishing_sessions(username,status)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS estate_fishing_daily (
            username TEXT PRIMARY KEY COLLATE NOCASE,
            fishing_day TEXT NOT NULL,
            retained_count INTEGER NOT NULL DEFAULT 0 CHECK(retained_count >= 0),
            notice_shown INTEGER NOT NULL DEFAULT 0 CHECK(notice_shown IN (0,1))
        )
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS delete_estate_fishing_daily
        AFTER DELETE ON estate_profiles
        BEGIN
            DELETE FROM estate_fishing_daily WHERE username=OLD.username;
        END
    """)
    mining_columns = {row[1] for row in conn.execute("PRAGMA table_info(estate_mining_runs)")}
    if "reserved_slots" not in mining_columns:
        conn.execute("ALTER TABLE estate_mining_runs ADD COLUMN reserved_slots INTEGER NOT NULL DEFAULT 12")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mining_user ON estate_mining_runs(username,status)")
    # 股市：市场纪元变化时整体重建所有行情表，不做旧数据迁移。
    conn.execute("""CREATE TABLE IF NOT EXISTS estate_market_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    from estate.market import (
        MARKET_EPOCH, market_epoch, market_tables, seed_market, set_market_epoch,
    )
    if market_epoch(conn) != MARKET_EPOCH:
        for table in market_tables():
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("""CREATE TABLE IF NOT EXISTS estate_market_symbols (
        symbol TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        anchor_cents REAL NOT NULL,
        ou_state REAL NOT NULL DEFAULT 0,
        ou_slot INTEGER NOT NULL DEFAULT -1,
        settled_slot INTEGER NOT NULL DEFAULT -1,
        price_cents INTEGER NOT NULL,
        inventory_milli INTEGER NOT NULL DEFAULT 0,
        available INTEGER NOT NULL DEFAULT 1 CHECK(available IN (0,1)),
        flow_minute INTEGER NOT NULL DEFAULT -1,
        flow_buy_milli INTEGER NOT NULL DEFAULT 0,
        flow_sell_milli INTEGER NOT NULL DEFAULT 0,
        split_count INTEGER NOT NULL DEFAULT 0,
        last_split_minute INTEGER NOT NULL DEFAULT -1,
        announced_splits INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS estate_market_printed (
        day INTEGER PRIMARY KEY,
        amount_cents INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS estate_market_ticks (
        symbol TEXT NOT NULL,
        minute INTEGER NOT NULL,
        price_cents INTEGER NOT NULL CHECK(price_cents > 0),
        PRIMARY KEY(symbol,minute)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS estate_market_candles (
        symbol TEXT NOT NULL,
        period TEXT NOT NULL CHECK(period IN ('minute','hour','day')),
        start_minute INTEGER NOT NULL,
        open_cents INTEGER NOT NULL,
        high_cents INTEGER NOT NULL,
        low_cents INTEGER NOT NULL,
        close_cents INTEGER NOT NULL,
        volume_milli INTEGER NOT NULL DEFAULT 0,
        volume_cents INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(symbol,period,start_minute)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS estate_market_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL COLLATE NOCASE,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('buy','sell')),
        price_cents INTEGER NOT NULL CHECK(price_cents > 0),
        qty_milli INTEGER NOT NULL CHECK(qty_milli > 0),
        filled_milli INTEGER NOT NULL DEFAULT 0 CHECK(filled_milli >= 0),
        status TEXT NOT NULL DEFAULT 'open'
            CHECK(status IN ('open','filled','cancelled','expired')),
        reason TEXT NOT NULL DEFAULT '',
        created_minute INTEGER NOT NULL,
        expires_minute INTEGER NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_market_orders_open "
                 "ON estate_market_orders(symbol,status,side,price_cents,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_market_orders_user "
                 "ON estate_market_orders(username,id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS estate_market_fills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        username TEXT NOT NULL COLLATE NOCASE,
        side TEXT NOT NULL CHECK(side IN ('buy','sell')),
        minute INTEGER NOT NULL,
        price_cents INTEGER NOT NULL,
        qty_milli INTEGER NOT NULL,
        amount_cents INTEGER NOT NULL,
        order_id INTEGER
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_market_fills_user "
                 "ON estate_market_fills(symbol,username,id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS estate_market_positions (
        username TEXT NOT NULL COLLATE NOCASE,
        symbol TEXT NOT NULL,
        shares_milli INTEGER NOT NULL DEFAULT 0 CHECK(shares_milli >= 0),
        cost_basis_cents INTEGER NOT NULL DEFAULT 0 CHECK(cost_basis_cents >= 0),
        realized_pnl_cents INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(username,symbol)
    )""")
    seed_market(conn)
    set_market_epoch(conn, MARKET_EPOCH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS estate_thefts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_username TEXT NOT NULL COLLATE NOCASE,
            visitor_username TEXT NOT NULL COLLATE NOCASE,
            plot_index INTEGER NOT NULL CHECK(plot_index >= 0),
            crop_id TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            steal_day TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            read_at INTEGER,
            request_id TEXT NOT NULL,
            UNIQUE(visitor_username, request_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_estate_thefts_owner_day "
        "ON estate_thefts(owner_username, steal_day)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_estate_thefts_pair_day "
        "ON estate_thefts(visitor_username, owner_username, steal_day)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_estate_thefts_unread "
        "ON estate_thefts(owner_username, read_at, created_at)"
    )
    theft_columns = {row[1] for row in conn.execute("PRAGMA table_info(estate_thefts)")}
    if "outcome" not in theft_columns:
        conn.execute("ALTER TABLE estate_thefts ADD COLUMN outcome TEXT NOT NULL DEFAULT 'stolen'")
    if "coins_dropped" not in theft_columns:
        conn.execute("ALTER TABLE estate_thefts ADD COLUMN coins_dropped REAL NOT NULL DEFAULT 0")
    if "visitor_read_at" not in theft_columns:
        conn.execute("ALTER TABLE estate_thefts ADD COLUMN visitor_read_at INTEGER")
