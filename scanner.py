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
# ARMIN MARKET SCANNER V2
# Alert-only / paper-tracking. It does NOT place real orders.
# ============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ---- Core scan settings ----
SCAN_EVERY = int(os.getenv("SCAN_EVERY", "60"))
MIN_24H_QUOTE_VOLUME = float(os.getenv("MIN_24H_QUOTE_VOLUME", "3000000"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "160"))
ALERT_COOLDOWN = int(os.getenv("ALERT_COOLDOWN", str(45 * 60)))

# ---- Cost / risk settings ----
# User said normal Revolut may cost about €1.50 per transaction.
FEE_PER_TRANSACTION_EUR = float(os.getenv("FEE_PER_TRANSACTION_EUR", "1.50"))
TRADING_BALANCE_EUR = float(os.getenv("TRADING_BALANCE_EUR", "100"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.35"))
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "0.02"))
MIN_NET_RR = float(os.getenv("MIN_NET_RR", "1.6"))

# ---- Optional intelligence modules ----
ENABLE_NEWS = os.getenv("ENABLE_NEWS", "true").lower() == "true"
ENABLE_SEC = os.getenv("ENABLE_SEC", "false").lower() == "true"
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "").strip()
SEC_SCAN_EVERY = int(os.getenv("SEC_SCAN_EVERY", "600"))

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
session.headers.update({"User-Agent": "Armin-Market-Scanner/2.0"})

last_alert = {}
news_cache = {}
tracked = {}
last_sec_scan = 0
seen_sec_filings = set()

# Lightweight persistent paper journal. Railway's filesystem may reset on redeploy
# unless you attach a persistent volume.
DB_PATH = os.getenv("DB_PATH", "scanner_v2.db")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


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
                INSERT INTO paper_positions(symbol,entry_ts,entry_price,high_price,stop_price,
                                            partial_taken,status,last_update)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                    high_price=excluded.high_price,
                    stop_price=excluded.stop_price,
                    partial_taken=excluded.partial_taken,
                    status=excluded.status,
                    last_update=excluded.last_update
                """,
                (
                    symbol,
                    p["entry_ts"],
                    p["entry"],
                    p["high"],
                    p["stop"],
                    int(p["partial_taken"]),
                    p["status"],
                    utc_now(),
                ),
            )
    except Exception as exc:
        print("DB position error:", exc)


def telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("[TELEGRAM NOT CONFIGURED]")
        print(message)
        return

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message},
            timeout=12,
        )
        if not r.ok:
            print("Telegram error:", r.status_code, r.text[:200])
    except Exception as exc:
        print("Telegram exception:", exc)


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
                    telegram(f"🟢 MARKET FEED RECOVERED\nUsing {endpoint}")
            return data
        except (requests.RequestException, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            print(f"[MARKET FEED] {endpoint} failed" + (f" ({status})" if status else "") + f": {exc}")
            last_error = exc

    now = time.time()
    if now - last_feed_warning >= FEED_WARNING_COOLDOWN:
        telegram("🔴 MARKET DATA OFFLINE\nAll configured Binance public-data endpoints failed. Scanner will keep retrying.")
        last_feed_warning = now
    raise RuntimeError(f"All Binance market-data endpoints failed: {last_error}")


def get_symbols():
    data = binance_json("/api/v3/ticker/24hr", timeout=20)
    if not isinstance(data, list):
        return []

    excluded_suffixes = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
    rows = []

    for x in data:
        symbol = x.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        if symbol.endswith(excluded_suffixes):
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
    return [s for s, _ in rows[:MAX_SYMBOLS]]


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

    # Baseline excludes the latest 10 minutes.
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

    move_1m = pct(price, closes[-2])
    move_3m = pct(price, closes[-4])
    move_5m = pct(price, closes[-6])
    move_15m = pct(price, closes[-16])

    previous_high = max(highs[-35:-6])
    previous_low = min(lows[-35:-6])
    breakout_pct = pct(price, previous_high)

    # Compression: compare recent 10m range with prior 20m range.
    prior20_high = max(highs[-35:-15])
    prior20_low = min(lows[-35:-15])
    recent10_high = max(highs[-15:-5])
    recent10_low = min(lows[-15:-5])

    prior_range = (prior20_high - prior20_low) / max(prior20_low, 1e-12)
    recent_range = (recent10_high - recent10_low) / max(recent10_low, 1e-12)
    compression = recent_range < prior_range * 0.75 if prior_range > 0 else False

    score = 0.0

    # Volume and acceleration
    if volume_ratio >= 1.8:
        score += 1
    if volume_ratio >= 3.0:
        score += 1
    if volume_ratio >= 5.0:
        score += 1
    if volume_accel >= 1.5:
        score += 1
    if trade_accel >= 1.4:
        score += 1

    # Buying pressure
    if buy_ratio >= 0.58:
        score += 1
    if buy_ratio >= 0.68:
        score += 1

    # Price structure
    if move_3m >= 0.7:
        score += 1
    if price >= previous_high:
        score += 1.5
    elif breakout_pct >= -0.7:
        score += 0.5

    # Compression -> expansion is useful pre-breakout context
    if compression and move_3m > 0.25:
        score += 0.5

    # Penalize obviously deteriorating action.
    if move_1m < -2.0:
        score -= 1.5
    if buy_ratio < 0.45:
        score -= 1.0

    # Internal "market mood" state. This is a state machine, not real emotion.
    if score >= 8 and buy_ratio >= 0.62:
        mood = "CONVICTION"
    elif score >= 6:
        mood = "BUILDING"
    elif score >= 4.5:
        mood = "INTERESTED"
    elif move_3m < -1.5 or buy_ratio < 0.45:
        mood = "DEFENSIVE"
    else:
        mood = "NEUTRAL"

    # Separate rapid move classification instead of silently rejecting >8%.
    rapid_move = move_5m >= 8.0 or move_3m >= 6.0

    if rapid_move and volume_ratio >= 2.2:
        stage = "RAPID"
    elif score >= 7.0 and move_5m >= 0.8 and price >= previous_high:
        stage = "CONFIRMED"
    elif score >= 5.0 and move_5m < 6.0:
        stage = "EARLY"
    else:
        stage = "NONE"

    return {
        "symbol": symbol,
        "price": price,
        "volume_ratio": volume_ratio,
        "volume_accel": volume_accel,
        "trade_accel": trade_accel,
        "buy_ratio": buy_ratio,
        "move_1m": move_1m,
        "move_3m": move_3m,
        "move_5m": move_5m,
        "move_15m": move_15m,
        "previous_high": previous_high,
        "breakout_pct": breakout_pct,
        "compression": compression,
        "score": score,
        "mood": mood,
        "stage": stage,
    }


def can_alert(key, cooldown=ALERT_COOLDOWN):
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


# ---------------- FEE-AWARE POSITION SIZING ----------------

def trade_plan(x):
    # Dynamic reference stop/target. Still only a model, not a prediction.
    if x["stage"] == "RAPID":
        stop_pct = 0.075
        target_pct = 0.28
    elif x["score"] >= 8.5:
        stop_pct = 0.060
        target_pct = 0.24
    else:
        stop_pct = 0.065
        target_pct = 0.20

    risk_budget = TRADING_BALANCE_EUR * RISK_PER_TRADE_PCT
    risk_based_size = risk_budget / stop_pct
    cap_size = TRADING_BALANCE_EUR * MAX_POSITION_PCT
    amount = max(0.0, min(risk_based_size, cap_size))

    round_trip_fee = 2 * FEE_PER_TRANSACTION_EUR
    gross_upside = amount * target_pct
    gross_downside = amount * stop_pct

    # Fees are counted in both outcomes. Spread remains asset-dependent, so
    # add a modest model allowance of 0.5% of position value.
    estimated_spread = amount * 0.005
    costs = round_trip_fee + estimated_spread

    net_upside = gross_upside - costs
    net_downside = gross_downside + costs
    net_rr = net_upside / net_downside if net_downside > 0 else 0

    # Also reject when costs eat too much of plausible gross upside.
    cost_share = costs / gross_upside if gross_upside > 0 else 1.0

    economically_sensible = (
        amount >= 10
        and net_upside > 0
        and net_rr >= MIN_NET_RR
        and cost_share <= 0.35
    )

    return {
        "amount_eur": round(amount, 2),
        "stop_pct": stop_pct,
        "target_pct": target_pct,
        "stop_price": x["price"] * (1 - stop_pct),
        "target_price": x["price"] * (1 + target_pct),
        "round_trip_fee_eur": round(round_trip_fee, 2),
        "spread_allowance_eur": round(estimated_spread, 2),
        "net_rr": round(net_rr, 2),
        "sensible": economically_sensible,
    }


def short_news_line(news):
    if not news:
        return ""
    top = news[0]
    direction = "positive" if top["score"] > 0 else "negative"
    return f"\nNews: {direction} catalyst headline detected"


def alert_early(x, news):
    base = x["symbol"].replace("USDT", "")
    return (
        f"🟡 EARLY WATCH — {base}\n"
        f"Price {x['price']:.8g} | 5m {x['move_5m']:+.1f}%\n"
        f"Vol {x['volume_ratio']:.1f}× | accel {x['volume_accel']:.1f}× | buy {x['buy_ratio']*100:.0f}%\n"
        f"State: {x['mood']}"
        f"{short_news_line(news)}"
    )


def alert_confirmed(x, plan, news):
    base = x["symbol"].replace("USDT", "")
    if not plan["sensible"]:
        return (
            f"🟡 BREAKOUT — {base}\n"
            f"Price {x['price']:.8g} | score {x['score']:.1f}\n"
            f"Signal strong, but fees/risk make this unattractive at €{TRADING_BALANCE_EUR:.0f} balance."
            f"{short_news_line(news)}"
        )

    return (
        f"🟢 BUY CANDIDATE — {base}\n"
        f"€{plan['amount_eur']:.0f} @ ~{x['price']:.8g}\n"
        f"Target {plan['target_price']:.8g} | stop {plan['stop_price']:.8g}\n"
        f"Est. fees €{plan['round_trip_fee_eur']:.2f} + spread | R:R ~{plan['net_rr']:.1f}\n"
        f"Reason: breakout + {x['volume_ratio']:.1f}× volume + {x['buy_ratio']*100:.0f}% buy pressure"
        f"{short_news_line(news)}"
    )


def alert_rapid(x, news):
    base = x["symbol"].replace("USDT", "")
    return (
        f"⚡ RAPID MOVE — {base}\n"
        f"Price {x['price']:.8g} | 5m {x['move_5m']:+.1f}%\n"
        f"Vol {x['volume_ratio']:.1f}× | buy {x['buy_ratio']*100:.0f}%\n"
        f"Do not chase automatically; analyzing continuation risk."
        f"{short_news_line(news)}"
    )


# ---------------- PAPER POSITION TRACKING / DYNAMIC EXITS ----------------

def start_tracking(x, plan):
    symbol = x["symbol"]
    tracked[symbol] = {
        "entry_ts": utc_now(),
        "entry": x["price"],
        "high": x["price"],
        "stop": plan["stop_price"],
        "target": plan["target_price"],
        "partial_taken": False,
        "status": "OPEN",
        "last_signal": "ENTRY",
        "last_signal_ts": time.time(),
    }
    save_position(symbol, tracked[symbol])


def update_position(x):
    symbol = x["symbol"]
    p = tracked.get(symbol)
    if not p or p["status"] != "OPEN":
        return

    price = x["price"]
    p["high"] = max(p["high"], price)

    gain = pct(price, p["entry"])
    drawdown_from_high = pct(price, p["high"])

    # Trailing stop activates only after the trade has earned room.
    if gain >= 10:
        p["stop"] = max(p["stop"], p["high"] * 0.92)
    if gain >= 20:
        p["stop"] = max(p["stop"], p["high"] * 0.94)

    base = symbol.replace("USDT", "")

    # Hard invalidation / trailing stop
    if price <= p["stop"]:
        msg = (
            f"🔴 EXIT ALL — {base}\n"
            f"Price {price:.8g} | from entry {gain:+.1f}%\n"
            f"Reason: stop/invalidation hit."
        )
        telegram(msg)
        log_alert(symbol, "EXIT_ALL", price, x["score"], {"gain_pct": gain})
        p["status"] = "CLOSED"
        save_position(symbol, p)
        return

    # Momentum failure even before hard stop.
    if (
        x["move_3m"] <= -2.2
        and x["buy_ratio"] < 0.43
        and x["volume_ratio"] >= 1.8
        and gain > -3
    ):
        msg = (
            f"🔴 EXIT ALL — {base}\n"
            f"Price {price:.8g} | from entry {gain:+.1f}%\n"
            f"Reason: strong sell pressure + momentum failure."
        )
        telegram(msg)
        log_alert(symbol, "EXIT_ALL", price, x["score"], {"gain_pct": gain})
        p["status"] = "CLOSED"
        save_position(symbol, p)
        return

    # Partial profit only once.
    weakening = x["buy_ratio"] < 0.52 or x["move_1m"] < -1.0 or drawdown_from_high <= -5.0

    if gain >= 18 and weakening and not p["partial_taken"]:
        msg = (
            f"🟠 SELL / TAKE PROFIT — {base}\n"
            f"Take ~50% | price {price:.8g} | gain {gain:+.1f}%\n"
            f"Reason: strong gain, momentum now weakening."
        )
        telegram(msg)
        log_alert(symbol, "TAKE_PROFIT_50", price, x["score"], {"gain_pct": gain})
        p["partial_taken"] = True
        p["last_signal"] = "TP50"
        p["last_signal_ts"] = time.time()
        save_position(symbol, p)
        return

    if gain >= 30 and not p["partial_taken"]:
        msg = (
            f"🟠 SELL / TAKE PROFIT — {base}\n"
            f"Take ~50% | price {price:.8g} | gain {gain:+.1f}%\n"
            f"Reason: outsized move; bank part and trail the rest."
        )
        telegram(msg)
        log_alert(symbol, "TAKE_PROFIT_50", price, x["score"], {"gain_pct": gain})
        p["partial_taken"] = True
        p["last_signal"] = "TP50"
        p["last_signal_ts"] = time.time()
        save_position(symbol, p)
        return

    save_position(symbol, p)


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

def process_candidate(x):
    symbol = x["symbol"]
    base = symbol.replace("USDT", "")

    # Existing paper position gets priority so exits are never ignored.
    if symbol in tracked and tracked[symbol]["status"] == "OPEN":
        update_position(x)

    if x["stage"] == "NONE":
        return

    news = []
    if x["score"] >= 5 or x["stage"] == "RAPID":
        news = get_candidate_news(base)

    if x["stage"] == "RAPID":
        key = f"{symbol}:RAPID"
        if can_alert(key, 30 * 60):
            telegram(alert_rapid(x, news))
            log_alert(symbol, "RAPID_MOVE", x["price"], x["score"], x)
            mark_alert(key)
        return

    if x["stage"] == "EARLY":
        key = f"{symbol}:EARLY"
        if can_alert(key, 45 * 60):
            telegram(alert_early(x, news))
            log_alert(symbol, "EARLY_WATCH", x["price"], x["score"], x)
            mark_alert(key)
        return

    if x["stage"] == "CONFIRMED":
        plan = trade_plan(x)
        key = f"{symbol}:CONFIRMED"

        if can_alert(key, 60 * 60):
            telegram(alert_confirmed(x, plan, news))
            log_alert(symbol, "CONFIRMED", x["price"], x["score"], {"signal": x, "plan": plan})
            mark_alert(key)

            # Track only economically sensible candidates as paper positions.
            if plan["sensible"] and symbol not in tracked:
                start_tracking(x, plan)


def scan():
    symbols = get_symbols()
    print(f"[{utc_now()}] Scanning {len(symbols)} liquid USDT markets...")

    results = []

    for symbol in symbols:
        try:
            x = analyse(symbol)
            if x:
                results.append(x)
            # Keeps request pace moderate.
            time.sleep(0.06)
        except requests.HTTPError as exc:
            print(symbol, "HTTP", exc)
        except Exception as exc:
            print(symbol, exc)

    # First update every open tracked position, even if it isn't a new candidate.
    by_symbol = {x["symbol"]: x for x in results}
    for symbol in list(tracked.keys()):
        if tracked[symbol]["status"] != "OPEN":
            continue
        if symbol in by_symbol:
            update_position(by_symbol[symbol])
        else:
            # Fetch directly because the symbol could have fallen below volume ranking.
            try:
                x = analyse(symbol)
                if x:
                    update_position(x)
            except Exception as exc:
                print("Tracked update error", symbol, exc)

    candidates = [x for x in results if x["stage"] != "NONE"]
    candidates.sort(
        key=lambda x: (
            1 if x["stage"] == "CONFIRMED" else 0,
            x["score"],
            x["volume_ratio"],
            x["buy_ratio"],
        ),
        reverse=True,
    )

    # Process a limited number of strongest candidates to control news/API usage.
    for x in candidates[:8]:
        process_candidate(x)

    scan_sec_insiders()


def main():
    db_init()

    telegram(
        "🟢 Armin Market Scanner V2 ONLINE\n"
        "Pre-breakout + rapid-move detection + fee-aware paper tracking + dynamic exits + market-feed failover."
    )

    if ENABLE_SEC and not SEC_USER_AGENT:
        print("SEC scanner disabled until SEC_USER_AGENT is configured.")

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
