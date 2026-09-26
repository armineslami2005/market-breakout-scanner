import os
import re
import time
import json
import math
import sqlite3
import statistics
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin

import requests

# ============================================================
# ARMIN MARKET SCANNER V2.1
# Quiet, action-first, stateful, interactive and self-auditing.
# Alert-only: it does NOT place real orders.
# ============================================================

VERSION = "2.1"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ---- Core scan settings ----
SCAN_EVERY = int(os.getenv("SCAN_EVERY", "60"))
MIN_24H_QUOTE_VOLUME = float(os.getenv("MIN_24H_QUOTE_VOLUME", "3000000"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "160"))
CORE_SYMBOLS = int(os.getenv("CORE_SYMBOLS", "60"))
ROTATING_SYMBOLS = int(os.getenv("ROTATING_SYMBOLS", "40"))
MIN_RECENT_3M_QUOTE_VOLUME = float(os.getenv("MIN_RECENT_3M_QUOTE_VOLUME", "25000"))
DECISION_CONFIRM_SCANS = int(os.getenv("DECISION_CONFIRM_SCANS", "2"))
ACTION_COOLDOWN = int(os.getenv("ACTION_COOLDOWN", str(3 * 60 * 60)))

# ---- Decision thresholds ----
BUY_SCORE = float(os.getenv("BUY_SCORE", "7.0"))
SPEC_SCORE = float(os.getenv("SPEC_SCORE", "7.75"))
WATCH_SCORE = float(os.getenv("WATCH_SCORE", "6.25"))

# ---- Cost / risk settings ----
# Default execution venue is Revolut X. Its fee model is percentage-based,
# unlike the regular Revolut crypto interface which may include minimum fees.
EXECUTION_VENUE = os.getenv("EXECUTION_VENUE", "REVOLUT_X").strip().upper()
REVOLUT_X_REGION = os.getenv("REVOLUT_X_REGION", "EEA").strip().upper()
REQUIRE_EXECUTION_VENUE_LISTING = os.getenv("REQUIRE_EXECUTION_VENUE_LISTING", "true").lower() == "true"
REVOLUT_X_PUBLIC = "https://revx.revolut.com/api/1.0/public"
REVOLUT_X_TAKER_FEE_RATE = float(os.getenv("REVOLUT_X_TAKER_FEE_RATE", "0.0009"))
RETAIL_FIXED_FEE_EUR = float(os.getenv("RETAIL_FIXED_FEE_EUR", "1.50"))
ESTIMATED_SPREAD_RATE = float(os.getenv("ESTIMATED_SPREAD_RATE", "0.0015"))
TRADING_BALANCE_EUR = float(os.getenv("TRADING_BALANCE_EUR", "100"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.35"))
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "0.02"))
SPEC_RISK_PER_TRADE_PCT = float(os.getenv("SPEC_RISK_PER_TRADE_PCT", "0.025"))
MIN_NET_RR = float(os.getenv("MIN_NET_RR", "1.5"))

# ---- Learning / audit settings ----
LEARNING_MIN_SAMPLES = int(os.getenv("LEARNING_MIN_SAMPLES", "20"))
BUY_AUDIT_HORIZON = int(os.getenv("BUY_AUDIT_HORIZON", str(6 * 60 * 60)))
WATCH_AUDIT_HORIZON = int(os.getenv("WATCH_AUDIT_HORIZON", str(60 * 60)))

# ---- Optional intelligence modules ----
ENABLE_NEWS = os.getenv("ENABLE_NEWS", "true").lower() == "true"
ENABLE_SEC = os.getenv("ENABLE_SEC", "false").lower() == "true"
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "").strip()
SEC_SCAN_EVERY = int(os.getenv("SEC_SCAN_EVERY", "600"))

# Assets pegged to fiat / stable-value products are not breakout candidates.
STABLE_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USDS",
    "USD1", "PYUSD", "RLUSD", "EUR", "EURC", "EURI", "AEUR", "TRY", "BRL",
}

BINANCE_ENDPOINTS = [
    "https://data-api.binance.vision",
    "https://api-gcp.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
    "https://api.binance.com",
]
active_binance = None
last_feed_warning = 0
FEED_WARNING_COOLDOWN = 30 * 60
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
SEC_CURRENT = "https://www.sec.gov/cgi-bin/browse-edgar"

session = requests.Session()
session.headers.update({"User-Agent": "Armin-Market-Scanner/2.1"})

last_alert = {}
news_cache = {}
tracked = {}                 # Positions the user explicitly says they own.
candidate_memory = {}        # Internal watch state; not pushed to Telegram.
last_action = {}             # Last action shown per symbol.
last_reason = {}             # Used by /why.
last_prices = {}
known_crypto_symbols = set()
revx_market_cache = {"ts": 0, "by_base": {}}
last_sec_scan = 0
seen_sec_filings = set()
scan_number = 0

DB_PATH = os.getenv("DB_PATH", "scanner_v21.db")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _ensure_column(con, table, name, ddl):
    cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    if name not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def db_init():
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                price REAL,
                score REAL,
                details TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_positions (
                symbol TEXT PRIMARY KEY,
                entry_ts TEXT,
                entry_price REAL,
                high_price REAL,
                stop_price REAL,
                partial_taken INTEGER DEFAULT 0,
                status TEXT,
                last_update TEXT
            )
            """
        )
        _ensure_column(con, "paper_positions", "target_price", "REAL")
        _ensure_column(con, "paper_positions", "quantity", "REAL DEFAULT 0")
        _ensure_column(con, "paper_positions", "source", "TEXT DEFAULT 'USER'")
        _ensure_column(con, "paper_positions", "user_managed", "INTEGER DEFAULT 1")

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                ts_iso TEXT NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                entry_price REAL NOT NULL,
                target_price REAL,
                stop_price REAL,
                score REAL,
                details TEXT,
                status TEXT DEFAULT 'OPEN',
                max_gain_pct REAL DEFAULT 0,
                max_drawdown_pct REAL DEFAULT 0,
                result TEXT,
                resolved_ts TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_state (
                symbol TEXT PRIMARY KEY,
                action TEXT,
                ts REAL,
                reason TEXT,
                price REAL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )


def log_alert(symbol, alert_type, price=None, score=None, details=None):
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "INSERT INTO alerts(ts,symbol,alert_type,price,score,details) VALUES(?,?,?,?,?,?)",
                (utc_now(), symbol, alert_type, price, score, json.dumps(details or {})),
            )
    except Exception as exc:
        print("DB alert error:", exc)


def save_position(symbol, p):
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                """
                INSERT INTO paper_positions(
                    symbol,entry_ts,entry_price,high_price,stop_price,partial_taken,
                    status,last_update,target_price,quantity,source,user_managed
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                    entry_ts=excluded.entry_ts,
                    entry_price=excluded.entry_price,
                    high_price=excluded.high_price,
                    stop_price=excluded.stop_price,
                    partial_taken=excluded.partial_taken,
                    status=excluded.status,
                    last_update=excluded.last_update,
                    target_price=excluded.target_price,
                    quantity=excluded.quantity,
                    source=excluded.source,
                    user_managed=excluded.user_managed
                """,
                (
                    symbol, p["entry_ts"], p["entry"], p["high"], p["stop"],
                    int(p.get("partial_taken", False)), p["status"], utc_now(),
                    p.get("target"), p.get("quantity", 0.0), p.get("source", "USER"),
                    int(p.get("user_managed", True)),
                ),
            )
    except Exception as exc:
        print("DB position error:", exc)


def load_positions():
    try:
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute(
                """
                SELECT symbol,entry_ts,entry_price,high_price,stop_price,partial_taken,
                       status,target_price,quantity,source,user_managed
                FROM paper_positions WHERE status='OPEN' AND user_managed=1
                """
            ).fetchall()
        for row in rows:
            symbol = row[0]
            tracked[symbol] = {
                "entry_ts": row[1], "entry": row[2], "high": row[3] or row[2],
                "stop": row[4], "partial_taken": bool(row[5]), "status": row[6],
                "target": row[7], "quantity": row[8] or 0.0, "source": row[9] or "USER",
                "user_managed": bool(row[10]), "weak_hits": 0,
            }
    except Exception as exc:
        print("DB load positions error:", exc)


def load_signal_state():
    try:
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute("SELECT symbol,action,ts,reason FROM signal_state").fetchall()
        for symbol, action, ts, reason in rows:
            last_action[symbol] = {"action": action, "ts": ts or 0}
            if reason:
                last_reason[symbol] = reason
    except Exception as exc:
        print("DB state load error:", exc)


def save_signal_state(symbol, action, reason, price):
    last_action[symbol] = {"action": action, "ts": time.time()}
    last_reason[symbol] = reason
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                """
                INSERT INTO signal_state(symbol,action,ts,reason,price) VALUES(?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                    action=excluded.action, ts=excluded.ts, reason=excluded.reason, price=excluded.price
                """,
                (symbol, action, time.time(), reason, price),
            )
    except Exception as exc:
        print("DB state save error:", exc)


def get_meta(key, default=None):
    try:
        with sqlite3.connect(DB_PATH) as con:
            row = con.execute("SELECT value FROM bot_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
    except Exception:
        return default


def set_meta(key, value):
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "INSERT INTO bot_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
    except Exception as exc:
        print("DB meta error:", exc)


def telegram(message, reply_markup=None, chat_id=None):
    target = str(chat_id or CHAT_ID or "").strip()
    if not BOT_TOKEN or not target:
        print("[TELEGRAM NOT CONFIGURED]")
        print(message)
        return False

    payload = {"chat_id": target, "text": message}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=12,
        )
        if not r.ok:
            print("Telegram error:", r.status_code, r.text[:200])
            return False
        return True
    except Exception as exc:
        print("Telegram exception:", exc)
        return False


def telegram_answer_callback(callback_id, text):
    if not BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text[:180]},
            timeout=10,
        )
    except Exception as exc:
        print("Callback answer error:", exc)


def request_json(url, params=None, timeout=12, headers=None):
    r = session.get(url, params=params, timeout=timeout, headers=headers)
    if r.status_code in (418, 429):
        retry = int(r.headers.get("Retry-After", "60"))
        print(f"Rate limited: sleeping {retry}s")
        time.sleep(min(retry, 120))
        return None
    r.raise_for_status()
    return r.json()


def binance_json(path, params=None, timeout=12):
    global active_binance, last_feed_warning

    endpoints = list(BINANCE_ENDPOINTS)
    if active_binance in endpoints:
        endpoints.remove(active_binance)
        endpoints.insert(0, active_binance)

    previous = active_binance
    last_error = None

    for endpoint in endpoints:
        try:
            r = session.get(endpoint + path, params=params, timeout=timeout)
            if r.status_code in (418, 429):
                print(f"[MARKET FEED] {endpoint} rate limited ({r.status_code}); trying backup.")
                last_error = RuntimeError(f"HTTP {r.status_code}")
                continue
            r.raise_for_status()
            data = r.json()

            if active_binance != endpoint:
                active_binance = endpoint
                print(f"[MARKET FEED] Binance endpoint active: {endpoint}")
                if previous is not None and previous != endpoint:
                    telegram("🟢 Market feed recovered. Scanner is running normally again.")
            return data
        except (requests.RequestException, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            print(f"[MARKET FEED] {endpoint} failed" + (f" ({status})" if status else "") + f": {exc}")
            last_error = exc

    now = time.time()
    if now - last_feed_warning >= FEED_WARNING_COOLDOWN:
        telegram("🔴 Market data is offline. I'll keep retrying automatically.")
        last_feed_warning = now
    raise RuntimeError(f"All Binance market-data endpoints failed: {last_error}")


def base_symbol(symbol):
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def normalize_crypto_symbol(text):
    s = re.sub(r"[^A-Za-z0-9]", "", text or "").upper()
    if not s:
        return ""
    if not s.endswith("USDT"):
        s += "USDT"
    return s


def get_revolut_x_market(force=False):
    """Public Revolut X market list/prices. No API key is required."""
    now = time.time()
    if not force and revx_market_cache["by_base"] and now - revx_market_cache["ts"] < 60:
        return revx_market_cache["by_base"]
    try:
        r = session.get(
            REVOLUT_X_PUBLIC + "/tickers",
            params={"region": REVOLUT_X_REGION},
            timeout=12,
        )
        if r.status_code == 429:
            return revx_market_cache["by_base"]
        r.raise_for_status()
        data = r.json().get("data", [])
        by_base = {}
        quote_rank = {"USD": 0, "USDC": 1, "EUR": 2, "USDT": 3}
        for item in data:
            raw_symbol = str(item.get("symbol", "")).upper().replace("-", "/")
            if "/" not in raw_symbol:
                continue
            base, quote = raw_symbol.split("/", 1)
            if base in STABLE_BASES:
                continue
            def num(*keys):
                for key in keys:
                    value = item.get(key)
                    if value not in (None, ""):
                        try:
                            return float(value)
                        except Exception:
                            pass
                return 0.0
            bid = num("bid", "best_bid", "bestBid", "bid_price", "bidPrice")
            ask = num("ask", "best_ask", "bestAsk", "ask_price", "askPrice")
            last = num("last", "last_price", "lastPrice", "price", "mid_price", "midPrice")
            spread_pct = ((ask / bid - 1) * 100) if bid > 0 and ask > 0 else 0.0
            candidate = {
                "symbol": raw_symbol, "quote": quote, "bid": bid, "ask": ask,
                "last": last, "spread_pct": spread_pct,
            }
            existing = by_base.get(base)
            if existing is None or quote_rank.get(quote, 99) < quote_rank.get(existing["quote"], 99):
                by_base[base] = candidate
        if by_base:
            revx_market_cache["by_base"] = by_base
            revx_market_cache["ts"] = now
        return revx_market_cache["by_base"]
    except Exception as exc:
        print("Revolut X public market error:", exc)
        return revx_market_cache["by_base"]


def execution_market_for(symbol):
    if EXECUTION_VENUE != "REVOLUT_X":
        return None
    return get_revolut_x_market().get(base_symbol(symbol))


def get_symbols():
    data = binance_json("/api/v3/ticker/24hr", timeout=20)
    if not isinstance(data, list):
        return []

    excluded_suffixes = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
    revx_bases = set(get_revolut_x_market().keys()) if EXECUTION_VENUE == "REVOLUT_X" else set()
    rows = []

    for x in data:
        symbol = x.get("symbol", "")
        if not symbol.endswith("USDT") or symbol.endswith(excluded_suffixes):
            continue
        base = base_symbol(symbol)
        if base in STABLE_BASES:
            continue
        if EXECUTION_VENUE == "REVOLUT_X" and REQUIRE_EXECUTION_VENUE_LISTING and revx_bases and base not in revx_bases:
            continue

        try:
            quote_volume = float(x["quoteVolume"])
            price = float(x["lastPrice"])
        except Exception:
            continue

        if quote_volume < MIN_24H_QUOTE_VOLUME or price <= 0:
            continue
        rows.append((symbol, quote_volume))

    rows.sort(key=lambda z: z[1], reverse=True)
    symbols = [s for s, _ in rows[:MAX_SYMBOLS]]
    known_crypto_symbols.clear()
    known_crypto_symbols.update(symbols)
    return symbols


def select_symbols_for_scan(symbols):
    global scan_number
    if len(symbols) <= CORE_SYMBOLS + ROTATING_SYMBOLS:
        return symbols

    core = symbols[:CORE_SYMBOLS]
    rest = symbols[CORE_SYMBOLS:]
    width = min(ROTATING_SYMBOLS, len(rest))
    if width <= 0:
        return core
    start = (scan_number * width) % len(rest)
    rotation = [rest[(start + i) % len(rest)] for i in range(width)]
    return list(dict.fromkeys(core + rotation))


def get_klines(symbol, limit=45):
    return binance_json(
        "/api/v3/klines",
        params={"symbol": symbol, "interval": "1m", "limit": limit},
        timeout=10,
    )


def safe_mean(xs):
    return statistics.mean(xs) if xs else 0.0


def pct(a, b):
    if b == 0:
        return 0.0
    return (a / b - 1.0) * 100.0


def analyse(symbol):
    candles = get_klines(symbol, 45)
    if not isinstance(candles, list) or len(candles) < 40:
        return None

    closes = [float(x[4]) for x in candles]
    highs = [float(x[2]) for x in candles]
    lows = [float(x[3]) for x in candles]
    volumes = [float(x[5]) for x in candles]
    taker_buy = [float(x[9]) for x in candles]
    trades = [float(x[8]) for x in candles]

    price = closes[-1]
    baseline_vol = safe_mean(volumes[-40:-10])
    recent3_vol = safe_mean(volumes[-3:])
    prev3_vol = safe_mean(volumes[-6:-3])
    if baseline_vol <= 0:
        return None

    volume_ratio = recent3_vol / baseline_vol
    volume_accel = recent3_vol / max(prev3_vol, 1e-12)
    recent3_trades = safe_mean(trades[-3:])
    prev3_trades = safe_mean(trades[-6:-3])
    trade_accel = recent3_trades / max(prev3_trades, 1e-12)

    recent_volume_sum = sum(volumes[-3:])
    recent_buy_sum = sum(taker_buy[-3:])
    buy_ratio = recent_buy_sum / recent_volume_sum if recent_volume_sum > 0 else 0.0
    recent_quote_volume_3m = sum(volumes[i] * closes[i] for i in range(len(candles)-3, len(candles)))

    move_1m = pct(price, closes[-2])
    move_3m = pct(price, closes[-4])
    move_5m = pct(price, closes[-6])
    move_15m = pct(price, closes[-16])

    previous_high = max(highs[-35:-6])
    previous_low = min(lows[-35:-6])
    breakout_pct = pct(price, previous_high)

    prior20_high = max(highs[-35:-15])
    prior20_low = min(lows[-35:-15])
    recent10_high = max(highs[-15:-5])
    recent10_low = min(lows[-15:-5])
    prior_range = (prior20_high - prior20_low) / max(prior20_low, 1e-12)
    recent_range = (recent10_high - recent10_low) / max(recent10_low, 1e-12)
    compression = recent_range < prior_range * 0.75 if prior_range > 0 else False

    score = 0.0
    if volume_ratio >= 1.8: score += 1
    if volume_ratio >= 3.0: score += 1
    if volume_ratio >= 5.0: score += 1
    if volume_accel >= 1.5: score += 1
    if trade_accel >= 1.4: score += 1
    if buy_ratio >= 0.58: score += 1
    if buy_ratio >= 0.68: score += 1
    if move_3m >= 0.7: score += 1
    if price >= previous_high:
        score += 1.5
    elif breakout_pct >= -0.7:
        score += 0.5
    if compression and move_3m > 0.25:
        score += 0.5
    if move_1m < -2.0:
        score -= 1.5
    if buy_ratio < 0.45:
        score -= 1.0
    if recent_quote_volume_3m < MIN_RECENT_3M_QUOTE_VOLUME:
        score -= 1.0

    rapid_move = move_5m >= 8.0 or move_3m >= 6.0

    return {
        "symbol": symbol, "price": price, "volume_ratio": volume_ratio,
        "volume_accel": volume_accel, "trade_accel": trade_accel,
        "buy_ratio": buy_ratio, "recent_quote_volume_3m": recent_quote_volume_3m,
        "move_1m": move_1m, "move_3m": move_3m, "move_5m": move_5m,
        "move_15m": move_15m, "previous_high": previous_high,
        "previous_low": previous_low, "breakout_pct": breakout_pct,
        "compression": compression, "score": score, "rapid_move": rapid_move,
    }


def can_alert(key, cooldown=ACTION_COOLDOWN):
    previous = last_alert.get(key, 0)
    return time.time() - previous > cooldown


def mark_alert(key):
    last_alert[key] = time.time()


# ---------------- NEWS / CATALYST INTELLIGENCE ----------------

CATALYST_WORDS = {
    "listing": 2,
    "listed": 2,
    "launch": 2,
    "mainnet": 3,
    "upgrade": 2,
    "partnership": 2,
    "integration": 2,
    "investment": 2,
    "funding": 2,
    "acquisition": 3,
    "approval": 3,
    "etf": 3,
    "contract": 2,
    "adoption": 2,
    "regulation": 1,
    "regulatory": 1,
    "lawsuit": -2,
    "hack": -4,
    "exploit": -4,
    "delist": -4,
    "unlock": -1,
}


def catalyst_score(title):
    t = title.lower()
    return sum(weight for word, weight in CATALYST_WORDS.items() if word in t)


def get_candidate_news(base_symbol):
    if not ENABLE_NEWS:
        return []

    cache_key = base_symbol.upper()
    cached = news_cache.get(cache_key)
    if cached and time.time() - cached["ts"] < 30 * 60:
        return cached["items"]

    # Add "cryptocurrency" to reduce symbol ambiguity.
    query = f'"{base_symbol}" cryptocurrency'
    params = {
        "query": query,
        "mode": "ArtList",
        "maxrecords": "12",
        "format": "json",
        "sort": "HybridRel",
        "timespan": "24h",
    }

    try:
        data = request_json(GDELT, params=params, timeout=15)
        articles = (data or {}).get("articles", [])
        items = []
        for a in articles:
            title = (a.get("title") or "").strip()
            if not title:
                continue
            s = catalyst_score(title)
            if s == 0:
                continue
            items.append(
                {
                    "title": title[:160],
                    "url": a.get("url", ""),
                    "domain": a.get("domain", ""),
                    "score": s,
                    "seen": a.get("seendate", ""),
                }
            )

        items.sort(key=lambda x: abs(x["score"]), reverse=True)
        items = items[:3]
        news_cache[cache_key] = {"ts": time.time(), "items": items}
        return items
    except Exception as exc:
        print("News error", base_symbol, exc)
        news_cache[cache_key] = {"ts": time.time(), "items": []}
        return []





# ---------------- ACTION ENGINE / FEES / LEARNING ----------------

def estimate_costs(amount):
    if EXECUTION_VENUE == "REVOLUT_X":
        trading_fees = amount * REVOLUT_X_TAKER_FEE_RATE * 2
    else:
        trading_fees = RETAIL_FIXED_FEE_EUR * 2
    spread = amount * ESTIMATED_SPREAD_RATE
    return trading_fees, spread


def trade_plan(x, action):
    if action == "SPEC_BUY":
        stop_pct, target_pct, risk_pct = 0.07, 0.25, SPEC_RISK_PER_TRADE_PCT
    elif x["score"] >= 8.5:
        stop_pct, target_pct, risk_pct = 0.055, 0.22, RISK_PER_TRADE_PCT
    else:
        stop_pct, target_pct, risk_pct = 0.06, 0.18, RISK_PER_TRADE_PCT

    risk_budget = TRADING_BALANCE_EUR * risk_pct
    risk_based_size = risk_budget / stop_pct
    cap_size = TRADING_BALANCE_EUR * MAX_POSITION_PCT
    amount = max(0.0, min(risk_based_size, cap_size))

    execution = execution_market_for(x["symbol"])
    entry_price = (execution or {}).get("ask") or (execution or {}).get("last") or x["price"]
    fees, spread = estimate_costs(amount)
    if execution and execution.get("spread_pct", 0) > 0:
        spread = amount * max(ESTIMATED_SPREAD_RATE, execution["spread_pct"] / 100.0)
    costs = fees + spread
    gross_upside = amount * target_pct
    gross_downside = amount * stop_pct
    net_upside = gross_upside - costs
    net_downside = gross_downside + costs
    net_rr = net_upside / net_downside if net_downside > 0 else 0
    cost_share = costs / gross_upside if gross_upside > 0 else 1.0

    sensible = amount >= 10 and net_upside > 0 and net_rr >= MIN_NET_RR and cost_share <= 0.30
    return {
        "amount_eur": round(amount, 2), "stop_pct": stop_pct, "target_pct": target_pct,
        "entry_price": entry_price,
        "stop_price": entry_price * (1 - stop_pct),
        "target_price": entry_price * (1 + target_pct),
        "fees_eur": round(fees, 3), "spread_eur": round(spread, 3),
        "net_rr": round(net_rr, 2), "sensible": sensible,
    }


def record_signal_event(symbol, action, x, plan=None, reason=""):
    # Avoid creating duplicate open events for the same symbol/action.
    try:
        with sqlite3.connect(DB_PATH) as con:
            row = con.execute(
                "SELECT id,ts FROM signal_events WHERE symbol=? AND action=? AND status='OPEN' ORDER BY id DESC LIMIT 1",
                (symbol, action),
            ).fetchone()
            if row and time.time() - row[1] < 45 * 60:
                return
            target = plan.get("target_price") if plan else x["price"] * 1.08
            stop = plan.get("stop_price") if plan else x["price"] * 0.95
            con.execute(
                """
                INSERT INTO signal_events(ts,ts_iso,symbol,action,entry_price,target_price,stop_price,score,details)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    time.time(), utc_now(), symbol, action, x["price"], target, stop, x["score"],
                    json.dumps({"reason": reason, "signal": x, "plan": plan or {}}),
                ),
            )
    except Exception as exc:
        print("Signal event error:", exc)


def update_signal_outcomes(price_map):
    try:
        now = time.time()
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute(
                """
                SELECT id,ts,symbol,action,entry_price,target_price,stop_price,max_gain_pct,max_drawdown_pct
                FROM signal_events WHERE status='OPEN'
                """
            ).fetchall()
            for row in rows:
                event_id, ts, symbol, action, entry, target, stop, max_gain, max_dd = row
                price = price_map.get(symbol)
                if not price or not entry:
                    continue
                gain = pct(price, entry)
                max_gain = max(max_gain or 0, gain)
                max_dd = min(max_dd or 0, gain)
                result = None

                if action in ("BUY_NOW", "SPEC_BUY"):
                    if target and price >= target:
                        result = "WIN"
                    elif stop and price <= stop:
                        result = "LOSS"
                    elif now - ts >= BUY_AUDIT_HORIZON:
                        if gain >= 3.0:
                            result = "WIN"
                        elif gain <= -3.0:
                            result = "LOSS"
                        else:
                            result = "FLAT"
                elif action == "WATCH":
                    if max_gain >= 8.0:
                        result = "MISSED"
                    elif now - ts >= WATCH_AUDIT_HORIZON:
                        result = "QUIET"

                if result:
                    con.execute(
                        "UPDATE signal_events SET status='RESOLVED',max_gain_pct=?,max_drawdown_pct=?,result=?,resolved_ts=? WHERE id=?",
                        (max_gain, max_dd, result, utc_now(), event_id),
                    )
                else:
                    con.execute(
                        "UPDATE signal_events SET max_gain_pct=?,max_drawdown_pct=? WHERE id=?",
                        (max_gain, max_dd, event_id),
                    )
    except Exception as exc:
        print("Outcome audit error:", exc)


def learning_bias():
    # Small, bounded calibration only after enough evidence. It never rewrites
    # the strategy after a single loss.
    try:
        with sqlite3.connect(DB_PATH) as con:
            buys = con.execute(
                """
                SELECT result FROM signal_events
                WHERE action IN ('BUY_NOW','SPEC_BUY') AND status='RESOLVED'
                ORDER BY id DESC LIMIT 60
                """
            ).fetchall()
            watches = con.execute(
                """
                SELECT result FROM signal_events
                WHERE action='WATCH' AND status='RESOLVED'
                ORDER BY id DESC LIMIT 60
                """
            ).fetchall()

        bias = 0.0
        if len(buys) >= LEARNING_MIN_SAMPLES:
            win_rate = sum(1 for r in buys if r[0] == "WIN") / len(buys)
            if win_rate < 0.40:
                bias += 0.50
            elif win_rate < 0.50:
                bias += 0.25
            elif win_rate > 0.68:
                bias -= 0.15

        if len(watches) >= LEARNING_MIN_SAMPLES:
            missed_rate = sum(1 for r in watches if r[0] == "MISSED") / len(watches)
            if missed_rate > 0.30:
                bias -= 0.20

        return max(-0.35, min(0.75, bias))
    except Exception as exc:
        print("Learning bias error:", exc)
        return 0.0


def learning_summary():
    try:
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute(
                "SELECT action,result FROM signal_events WHERE status='RESOLVED'"
            ).fetchall()
        buys = [r for r in rows if r[0] in ("BUY_NOW", "SPEC_BUY")]
        wins = sum(1 for _, result in buys if result == "WIN")
        losses = sum(1 for _, result in buys if result == "LOSS")
        watches = [r for r in rows if r[0] == "WATCH"]
        missed = sum(1 for _, result in watches if result == "MISSED")
        if not buys and not watches:
            return "🧠 Learning: collecting evidence. No completed samples yet."
        win_rate = (wins / len(buys) * 100) if buys else 0
        return f"🧠 Learning: {len(buys)} trades audited • {win_rate:.0f}% wins • {missed} missed moves found."
    except Exception:
        return "🧠 Learning data isn't available right now."


def update_candidate_memory(x):
    symbol = x["symbol"]
    now = time.time()
    strong = (
        x["score"] >= WATCH_SCORE
        and x["recent_quote_volume_3m"] >= MIN_RECENT_3M_QUOTE_VOLUME
        and not x["rapid_move"]
    )
    previous = candidate_memory.get(symbol)
    if strong:
        if previous and now - previous["last_seen"] <= 180:
            hits = previous["hits"] + 1
        else:
            hits = 1
        candidate_memory[symbol] = {
            "hits": min(hits, 10), "last_seen": now, "score": x["score"],
            "price": x["price"], "breakout_pct": x["breakout_pct"],
        }
    elif previous:
        previous["hits"] = max(0, previous["hits"] - 1)
        previous["last_seen"] = now
    return candidate_memory.get(symbol, {}).get("hits", 0)


def market_context(results):
    btc = next((x for x in results if x["symbol"] == "BTCUSDT"), None)
    if not btc:
        return {"risk_off": False, "btc_15m": 0.0}
    return {"risk_off": btc["move_15m"] <= -1.8, "btc_15m": btc["move_15m"]}


def decide_action(x, context):
    hits = update_candidate_memory(x)
    bias = context.get("learning_bias", 0.0)
    risk_penalty = 0.50 if context.get("risk_off") and x["symbol"] not in ("BTCUSDT", "ETHUSDT") else 0.0

    # Extended pumps are not entries. Weak buyer control makes them especially dangerous.
    if x["rapid_move"]:
        if x["move_5m"] >= 10.0 and x["buy_ratio"] < 0.52:
            return "AVOID_CHASE", "The move is already extended and buyer control is weak."
        return "NONE", "The move is already extended; waiting for a reset."

    buy_threshold = BUY_SCORE + bias + risk_penalty
    confirmed = (
        hits >= DECISION_CONFIRM_SCANS
        and x["score"] >= buy_threshold
        and x["volume_ratio"] >= 1.8
        and x["buy_ratio"] >= 0.56
        and x["trade_accel"] >= 1.05
        and 0.5 <= x["move_5m"] <= 5.5
        and x["breakout_pct"] >= -0.15
        and x["recent_quote_volume_3m"] >= MIN_RECENT_3M_QUOTE_VOLUME
    )
    if confirmed:
        return "BUY_NOW", "The breakout held across repeated scans with sustained buying pressure."

    speculative = (
        not context.get("risk_off")
        and hits >= DECISION_CONFIRM_SCANS
        and x["score"] >= SPEC_SCORE + bias
        and x["volume_ratio"] >= 2.8
        and x["volume_accel"] >= 1.6
        and x["trade_accel"] >= 1.20
        and x["buy_ratio"] >= 0.68
        and -0.2 <= x["move_5m"] <= 2.8
        and x["recent_quote_volume_3m"] >= MIN_RECENT_3M_QUOTE_VOLUME
    )
    if speculative:
        return "SPEC_BUY", "Strong early accumulation persisted before a full breakout."

    if x["score"] >= WATCH_SCORE and x["recent_quote_volume_3m"] >= MIN_RECENT_3M_QUOTE_VOLUME:
        return "WATCH", "Interesting, but not strong enough for an entry yet."
    return "NONE", "No actionable edge right now."


def should_notify(symbol, action):
    prev = last_action.get(symbol)
    if not prev:
        return True
    if prev["action"] != action:
        return True
    return time.time() - prev["ts"] >= ACTION_COOLDOWN


def buy_keyboard(symbol, price):
    return {
        "inline_keyboard": [[
            {"text": "✅ I bought", "callback_data": f"BOUGHT:{symbol}:{price:.12g}"},
            {"text": "Why?", "callback_data": f"WHY:{symbol}"},
        ]]
    }


def action_message(action, x, plan):
    base = base_symbol(x["symbol"])
    variant = sum(ord(c) for c in base) % 3
    if action == "BUY_NOW":
        phrases = [
            "Setup confirmed. Buy near {e} • Stop {s} • Target {t}.",
            "This one has confirmed. Enter near {e} • Stop {s} • Target {t}.",
            "Momentum confirmed. Entry around {e} • Stop {s} • Target {t}.",
        ]
        line = phrases[variant].format(
            e=f"{plan['entry_price']:.8g}", s=f"{plan['stop_price']:.8g}", t=f"{plan['target_price']:.8g}"
        )
        return f"🟢 BUY NOW — {base}\n{line}"
    if action == "SPEC_BUY":
        phrases = [
            "Early setup is strong. Small entry near {e} • Stop {s} • Target {t}.",
            "Higher-risk early entry. Keep it small near {e} • Stop {s} • Target {t}.",
            "Early momentum is holding. Small entry ~{e} • Stop {s} • Target {t}.",
        ]
        line = phrases[variant].format(
            e=f"{plan['entry_price']:.8g}", s=f"{plan['stop_price']:.8g}", t=f"{plan['target_price']:.8g}"
        )
        return f"⚡ SPECULATIVE BUY — {base}\n{line}"
    if action == "SKIP_FEES":
        return f"🔴 SKIP — {base}\nThe setup is good, but your current fee mode makes the trade poor value."
    if action == "AVOID_CHASE":
        return f"🔴 DON'T CHASE — {base}\nIt's already stretched. Wait for a reset instead."
    return ""


# ---------------- USER PORTFOLIO / DIRECT EXIT MANAGEMENT ----------------

def is_live_crypto_symbol(symbol):
    symbol = normalize_crypto_symbol(symbol)
    if symbol in known_crypto_symbols:
        return True
    if EXECUTION_VENUE == "REVOLUT_X" and base_symbol(symbol) in get_revolut_x_market():
        return True
    try:
        data = binance_json("/api/v3/ticker/price", params={"symbol": symbol}, timeout=8)
        return isinstance(data, dict) and float(data.get("price", 0)) > 0
    except Exception:
        return False


def latest_signal_plan(symbol):
    try:
        with sqlite3.connect(DB_PATH) as con:
            row = con.execute(
                "SELECT target_price,stop_price FROM signal_events WHERE symbol=? AND action IN ('BUY_NOW','SPEC_BUY') ORDER BY id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        if row:
            return {"target": row[0], "stop": row[1]}
    except Exception:
        pass
    return {}


def add_user_position(symbol, entry, quantity=0.0, stop=None, target=None):
    symbol = normalize_crypto_symbol(symbol)
    if not symbol:
        return False, "I couldn't read that symbol."
    if not is_live_crypto_symbol(symbol):
        return False, f"I don't have a live market feed for {base_symbol(symbol)} yet."

    stop = float(stop) if stop else entry * 0.92
    target = float(target) if target else entry * 1.20
    tracked[symbol] = {
        "entry_ts": utc_now(), "entry": float(entry), "high": float(entry),
        "stop": stop, "target": target, "partial_taken": False,
        "status": "OPEN", "quantity": float(quantity or 0), "source": "USER",
        "user_managed": True, "weak_hits": 0,
    }
    save_position(symbol, tracked[symbol])
    return True, f"✅ Tracking {base_symbol(symbol)}. I'll stay quiet unless your position needs action."


def close_user_position(symbol):
    symbol = normalize_crypto_symbol(symbol)
    p = tracked.get(symbol)
    if not p or p.get("status") != "OPEN":
        return False, f"I'm not tracking an open {base_symbol(symbol)} position."
    p["status"] = "CLOSED"
    save_position(symbol, p)
    tracked.pop(symbol, None)
    return True, f"✅ {base_symbol(symbol)} removed from your open positions."


def portfolio_message():
    opens = [(s, p) for s, p in tracked.items() if p.get("status") == "OPEN"]
    if not opens:
        return "📂 No open positions are being tracked."
    lines = ["📂 YOUR POSITIONS"]
    for symbol, p in opens[:12]:
        price = last_prices.get(symbol)
        if price:
            gain = pct(price, p["entry"])
            lines.append(f"{base_symbol(symbol)} {gain:+.1f}% • entry {p['entry']:.8g}")
        else:
            lines.append(f"{base_symbol(symbol)} • entry {p['entry']:.8g}")
    return "\n".join(lines)


def update_position(x):
    symbol = x["symbol"]
    p = tracked.get(symbol)
    if not p or p.get("status") != "OPEN" or not p.get("user_managed", True):
        return

    price = x["price"]
    p["high"] = max(p.get("high", p["entry"]), price)
    gain = pct(price, p["entry"])
    drawdown_from_high = pct(price, p["high"])

    # Earned trailing protection.
    if gain >= 8:
        p["stop"] = max(p["stop"], p["high"] * 0.93)
    if gain >= 18:
        p["stop"] = max(p["stop"], p["high"] * 0.95)

    base = base_symbol(symbol)

    if price <= p["stop"]:
        telegram(f"🔴 SELL NOW — {base}\nYour stop/invalidation was hit. Exit near {price:.8g}.")
        log_alert(symbol, "SELL_NOW", price, x["score"], {"gain_pct": gain, "reason": "stop"})
        p["status"] = "CLOSED"
        save_position(symbol, p)
        tracked.pop(symbol, None)
        return

    weak_now = x["move_3m"] <= -1.8 and x["buy_ratio"] < 0.44 and x["volume_ratio"] >= 1.6
    p["weak_hits"] = p.get("weak_hits", 0) + 1 if weak_now else 0
    if p["weak_hits"] >= 2 and gain > -4:
        telegram(f"🔴 SELL NOW — {base}\nMomentum has failed across two scans. Exit near {price:.8g}.")
        log_alert(symbol, "SELL_NOW", price, x["score"], {"gain_pct": gain, "reason": "momentum_failure"})
        p["status"] = "CLOSED"
        save_position(symbol, p)
        tracked.pop(symbol, None)
        return

    weakening = x["buy_ratio"] < 0.50 or x["move_1m"] < -0.8 or drawdown_from_high <= -4.5
    if gain >= 15 and weakening and not p.get("partial_taken"):
        telegram(f"🟠 TAKE PROFIT — {base}\nSell about half near {price:.8g}. I'll manage the rest.")
        log_alert(symbol, "TAKE_PROFIT", price, x["score"], {"gain_pct": gain})
        p["partial_taken"] = True
        p["stop"] = max(p["stop"], p["entry"] * 1.01)
        save_position(symbol, p)
        return

    if p.get("target") and price >= p["target"] and not p.get("partial_taken"):
        telegram(f"🟠 TAKE PROFIT — {base}\nTarget reached. Sell about half near {price:.8g}; let the rest run.")
        log_alert(symbol, "TAKE_PROFIT", price, x["score"], {"gain_pct": gain})
        p["partial_taken"] = True
        p["stop"] = max(p["stop"], p["entry"] * 1.01)

    save_position(symbol, p)


def command_reason(symbol):
    symbol = normalize_crypto_symbol(symbol)
    reason = last_reason.get(symbol)
    if not reason:
        return f"I don't have a recent decision for {base_symbol(symbol)}."
    return f"💬 {base_symbol(symbol)}: {reason}"


def handle_text_command(text, chat_id):
    raw = (text or "").strip()
    low = raw.lower()

    if low in ("/portfolio", "portfolio", "my portfolio"):
        telegram(portfolio_message(), chat_id=chat_id)
        return
    if low in ("/status", "status"):
        telegram(f"🟢 Scanner V{VERSION} active. Tracking {len(tracked)} open position(s).", chat_id=chat_id)
        return
    if low in ("/learning", "learning"):
        telegram(learning_summary(), chat_id=chat_id)
        return
    if low in ("/help", "help"):
        telegram(
            "Commands: /bought SOL 150.20 0.30 • /sold SOL • /portfolio • /why SOL • /learning",
            chat_id=chat_id,
        )
        return

    m = re.match(r"^/(?:why)\s+([A-Za-z0-9]+)$", raw, flags=re.I) or re.match(r"^why\s+([A-Za-z0-9]+)$", raw, flags=re.I)
    if m:
        telegram(command_reason(m.group(1)), chat_id=chat_id)
        return

    m = re.match(r"^/(?:sold)\s+([A-Za-z0-9]+)$", raw, flags=re.I) or re.match(r"^(?:i\s+)?sold\s+([A-Za-z0-9]+)$", raw, flags=re.I)
    if m:
        _, msg = close_user_position(m.group(1))
        telegram(msg, chat_id=chat_id)
        return

    m = re.match(
        r"^/(?:bought)\s+([A-Za-z0-9]+)\s+([0-9.]+)(?:\s+([0-9.]+))?$",
        raw, flags=re.I,
    ) or re.match(
        r"^(?:i\s+)?bought\s+([A-Za-z0-9]+)(?:\s+at)?\s+([0-9.]+)(?:\s+([0-9.]+))?$",
        raw, flags=re.I,
    )
    if m:
        symbol, entry, qty = m.group(1), float(m.group(2)), float(m.group(3) or 0)
        _, msg = add_user_position(symbol, entry, qty)
        telegram(msg, chat_id=chat_id)
        return

    if raw.startswith("/"):
        telegram("I didn't understand that command. Send /help.", chat_id=chat_id)


def handle_callback(callback):
    data = callback.get("data", "")
    callback_id = callback.get("id", "")
    message = callback.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if CHAT_ID and chat_id != str(CHAT_ID):
        telegram_answer_callback(callback_id, "Not authorized.")
        return

    if data.startswith("WHY:"):
        symbol = data.split(":", 1)[1]
        telegram_answer_callback(callback_id, "Reason sent.")
        telegram(command_reason(symbol), chat_id=chat_id)
        return

    if data.startswith("BOUGHT:"):
        parts = data.split(":")
        if len(parts) >= 3:
            symbol = parts[1]
            try:
                entry = float(parts[2])
            except Exception:
                entry = last_prices.get(symbol, 0)
            plan = latest_signal_plan(symbol)
            ok, msg = add_user_position(symbol, entry, stop=plan.get("stop"), target=plan.get("target"))
            telegram_answer_callback(callback_id, "Tracking it." if ok else "Couldn't track it.")
            telegram(msg, chat_id=chat_id)
        return


def poll_telegram_updates():
    if not BOT_TOKEN:
        return
    try:
        offset = int(get_meta("telegram_offset", "0") or 0)
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 0, "limit": 20}, timeout=10,
        )
        if not r.ok:
            print("Telegram getUpdates error:", r.status_code, r.text[:160])
            return
        for update in r.json().get("result", []):
            update_id = update.get("update_id", 0)
            set_meta("telegram_offset", update_id + 1)
            if "callback_query" in update:
                handle_callback(update["callback_query"])
                continue
            message = update.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            if CHAT_ID and chat_id != str(CHAT_ID):
                continue
            text = message.get("text")
            if text:
                handle_text_command(text, chat_id)
    except Exception as exc:
        print("Telegram polling error:", exc)


# ---------------- VERIFIED SEC INSIDER PURCHASE/SALE MODULE ----------------

def sec_headers():
    # SEC requests should identify the automated client with a real contact.
    if not SEC_USER_AGENT:
        return None
    return {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}


def get_sec_current_form4():
    headers = sec_headers()
    if not headers:
        return []

    params = {
        "action": "getcurrent",
        "type": "4",
        "owner": "include",
        "count": "40",
        "output": "atom",
    }

    r = session.get(SEC_CURRENT, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.text)

    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = []

    for entry in root.findall("a:entry", ns):
        title = entry.findtext("a:title", default="", namespaces=ns)
        link_el = entry.find("a:link", ns)
        href = link_el.attrib.get("href", "") if link_el is not None else ""
        updated = entry.findtext("a:updated", default="", namespaces=ns)
        if href:
            entries.append({"title": title, "url": href, "updated": updated})

    return entries


def parse_form4_from_index(index_url):
    headers = sec_headers()
    if not headers:
        return None

    r = session.get(index_url, headers=headers, timeout=20)
    r.raise_for_status()

    # Find candidate XML ownership docs from filing index.
    hrefs = re.findall(r'href="([^"]+\.xml)"', r.text, flags=re.I)
    if not hrefs:
        return None

    for href in hrefs:
        direct = href.replace("/xslF345X05/", "/")
        xml_url = urljoin("https://www.sec.gov", direct)

        try:
            x = session.get(xml_url, headers=headers, timeout=20)
            if not x.ok or "<ownershipDocument" not in x.text:
                continue

            root = ET.fromstring(x.text)
            symbol = (root.findtext(".//issuerTradingSymbol") or "").strip().upper()
            owner = (root.findtext(".//reportingOwner/rptOwnerName") or "").strip()

            transactions = []
            for tx in root.findall(".//nonDerivativeTransaction"):
                code = (tx.findtext(".//transactionCode") or "").strip().upper()
                if code not in ("P", "S"):
                    continue

                shares_text = tx.findtext(".//transactionShares/value") or "0"
                price_text = tx.findtext(".//transactionPricePerShare/value") or "0"
                ad = (tx.findtext(".//transactionAcquiredDisposedCode/value") or "").strip().upper()

                try:
                    shares = float(shares_text)
                except Exception:
                    shares = 0.0
                try:
                    price = float(price_text)
                except Exception:
                    price = 0.0

                transactions.append(
                    {
                        "code": code,
                        "shares": shares,
                        "price": price,
                        "value": shares * price,
                        "ad": ad,
                    }
                )

            if symbol and owner and transactions:
                return {
                    "symbol": symbol,
                    "owner": owner,
                    "transactions": transactions,
                    "source": index_url,
                }

        except Exception as exc:
            print("SEC XML parse error:", exc)
            continue

    return None


def scan_sec_insiders():
    global last_sec_scan

    if not ENABLE_SEC or not SEC_USER_AGENT:
        return

    if time.time() - last_sec_scan < SEC_SCAN_EVERY:
        return

    last_sec_scan = time.time()

    try:
        entries = get_sec_current_form4()
        for entry in entries[:20]:
            key = entry["url"]
            if key in seen_sec_filings:
                continue
            seen_sec_filings.add(key)

            filing = parse_form4_from_index(key)
            time.sleep(0.12)  # stay polite with SEC access

            if not filing:
                continue

            buys = [t for t in filing["transactions"] if t["code"] == "P" and t["ad"] == "A"]
            sells = [t for t in filing["transactions"] if t["code"] == "S" and t["ad"] == "D"]

            buy_value = sum(t["value"] for t in buys)
            sell_value = sum(t["value"] for t in sells)

            # Only surface material transactions. This is a disclosure, not a recommendation.
            if buy_value >= 250_000:
                telegram(
                    f"📄 VERIFIED INSIDER PURCHASE — {filing['symbol']}\n"
                    f"{filing['owner']} | ~${buy_value:,.0f}\n"
                    f"SEC Form 4. Treat as one signal, not a BUY instruction."
                )
                log_alert(
                    filing["symbol"],
                    "SEC_INSIDER_PURCHASE",
                    details={"owner": filing["owner"], "value": buy_value, "source": filing["source"]},
                )

            if sell_value >= 1_000_000:
                telegram(
                    f"📄 VERIFIED INSIDER SALE — {filing['symbol']}\n"
                    f"{filing['owner']} | ~${sell_value:,.0f}\n"
                    f"SEC Form 4. Sale motive is not inferred."
                )
                log_alert(
                    filing["symbol"],
                    "SEC_INSIDER_SALE",
                    details={"owner": filing["owner"], "value": sell_value, "source": filing["source"]},
                )

    except Exception as exc:
        print("SEC scanner error:", exc)





# ---------------- MAIN SCANNER ----------------

def process_candidate(x, context):
    symbol = x["symbol"]
    action, reason = decide_action(x, context)
    last_reason[symbol] = reason

    # Watches are intentionally silent. They are stored only for later auditing.
    if action == "WATCH":
        record_signal_event(symbol, "WATCH", x, reason=reason)
        return
    if action == "NONE":
        return

    if action == "AVOID_CHASE":
        if should_notify(symbol, action):
            telegram(action_message(action, x, {}))
            save_signal_state(symbol, action, reason, x["price"])
            log_alert(symbol, action, x["price"], x["score"], {"reason": reason})
        return

    # Only now spend a news request, after the market setup has already qualified.
    news = get_candidate_news(base_symbol(symbol)) if ENABLE_NEWS else []
    if news and min(item["score"] for item in news) <= -4:
        reason = "A severe negative catalyst was detected, so the entry was cancelled."
        if should_notify(symbol, "AVOID_NEWS"):
            telegram(f"🔴 SKIP — {base_symbol(symbol)}\nA serious negative catalyst just appeared. Don't enter now.")
            save_signal_state(symbol, "AVOID_NEWS", reason, x["price"])
        return

    plan = trade_plan(x, action)
    record_signal_event(symbol, action, x, plan, reason)

    if not plan["sensible"]:
        action = "SKIP_FEES"
        reason = "The market setup qualified, but the configured fees and position size destroy the risk/reward."

    if should_notify(symbol, action):
        telegram(
            action_message(action, x, plan),
            reply_markup=buy_keyboard(symbol, plan["entry_price"]) if action in ("BUY_NOW", "SPEC_BUY") else None,
        )
        save_signal_state(symbol, action, reason, x["price"])
        log_alert(symbol, action, x["price"], x["score"], {"reason": reason, "plan": plan})


def scan():
    global scan_number
    all_symbols = get_symbols()
    symbols = select_symbols_for_scan(all_symbols)
    scan_number += 1
    print(f"[{utc_now()}] Scanning {len(symbols)} markets ({len(all_symbols)} eligible)...")

    results = []
    for symbol in symbols:
        try:
            x = analyse(symbol)
            if x:
                results.append(x)
                last_prices[symbol] = x["price"]
            time.sleep(0.08)
        except requests.HTTPError as exc:
            print(symbol, "HTTP", exc)
        except Exception as exc:
            print(symbol, exc)

    # User-owned positions always get checked, even outside the rotating universe.
    by_symbol = {x["symbol"]: x for x in results}
    for symbol in list(tracked.keys()):
        if tracked[symbol].get("status") != "OPEN":
            continue
        x = by_symbol.get(symbol)
        if not x:
            try:
                x = analyse(symbol)
                if x:
                    by_symbol[symbol] = x
                    results.append(x)
                    last_prices[symbol] = x["price"]
            except Exception as exc:
                print("Tracked update error", symbol, exc)
        if x:
            update_position(x)

    context = market_context(results)
    context["learning_bias"] = learning_bias()
    price_map = {x["symbol"]: x["price"] for x in results}
    update_signal_outcomes(price_map)

    # Strongest first, but Telegram remains silent unless an actionable state is reached.
    candidates = [x for x in results if x["score"] >= WATCH_SCORE or x["rapid_move"]]
    candidates.sort(key=lambda x: (x["score"], x["volume_ratio"], x["buy_ratio"]), reverse=True)
    for x in candidates[:18]:
        process_candidate(x, context)

    scan_sec_insiders()
    poll_telegram_updates()


def main():
    db_init()
    load_positions()
    load_signal_state()

    telegram(
        f"🟢 Armin Market Scanner V{VERSION} ONLINE\n"
        "Quiet mode is on. I'll message only when there's an action worth taking."
    )

    if ENABLE_SEC and not SEC_USER_AGENT:
        print("SEC scanner disabled until SEC_USER_AGENT is configured.")

    # Pull old bot commands once at startup so interaction works immediately.
    poll_telegram_updates()

    while True:
        started = time.time()
        try:
            scan()
        except Exception as exc:
            print("SCAN ERROR:", exc)

        elapsed = time.time() - started
        sleep_for = max(5, SCAN_EVERY - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
