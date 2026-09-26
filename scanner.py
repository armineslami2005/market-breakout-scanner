import os
import time
import requests
from statistics import mean

# ---------- SETTINGS ----------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SCAN_EVERY = 60                 # seconds
MIN_24H_QUOTE_VOLUME = 5_000_000
MIN_PRICE = 0.000001

# Signal requirements
MIN_VOLUME_RATIO = 3.0
MIN_5M_MOVE = 1.0
MAX_5M_MOVE = 8.0              # don't chase something already vertical
MIN_BUY_RATIO = 0.60
MIN_SCORE = 4

# Avoid spamming the same coin
ALERT_COOLDOWN = 60 * 60       # 1 hour

BINANCE = "https://api.binance.com"

last_alert = {}


def telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram credentials not configured")
        print(message)
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message,
        },
        timeout=10,
    )


def get_symbols():
    data = requests.get(
        BINANCE + "/api/v3/ticker/24hr",
        timeout=15,
    ).json()

    symbols = []

    excluded = (
        "UPUSDT", "DOWNUSDT",
        "BULLUSDT", "BEARUSDT"
    )

    for x in data:
        symbol = x["symbol"]

        if not symbol.endswith("USDT"):
            continue

        if symbol.endswith(excluded):
            continue

        try:
            quote_volume = float(x["quoteVolume"])
            price = float(x["lastPrice"])
        except Exception:
            continue

        if quote_volume < MIN_24H_QUOTE_VOLUME:
            continue

        if price < MIN_PRICE:
            continue

        symbols.append(symbol)

    return symbols


def get_klines(symbol):
    params = {
        "symbol": symbol,
        "interval": "1m",
        "limit": 35,
    }

    r = requests.get(
        BINANCE + "/api/v3/klines",
        params=params,
        timeout=10,
    )

    return r.json()


def analyse(symbol):
    candles = get_klines(symbol)

    if not isinstance(candles, list) or len(candles) < 30:
        return None

    closes = [float(x[4]) for x in candles]
    volumes = [float(x[5]) for x in candles]

    # Binance gives taker-buy base volume here
    taker_buy = [float(x[9]) for x in candles]

    current_price = closes[-1]

    # Ignore current candle for baseline
    baseline_volume = mean(volumes[-26:-6])

    recent_volume = mean(volumes[-5:])

    if baseline_volume <= 0:
        return None

    volume_ratio = recent_volume / baseline_volume

    five_min_ago = closes[-6]

    move_5m = (
        (current_price / five_min_ago) - 1
    ) * 100

    total_recent_volume = sum(volumes[-5:])
    total_recent_buy = sum(taker_buy[-5:])

    if total_recent_volume <= 0:
        return None

    buy_ratio = total_recent_buy / total_recent_volume

    previous_high = max(closes[-26:-6])

    distance_from_high = (
        (current_price / previous_high) - 1
    ) * 100

    score = 0

    if volume_ratio >= 3:
        score += 2

    if volume_ratio >= 5:
        score += 1

    if move_5m >= MIN_5M_MOVE:
        score += 1

    if buy_ratio >= MIN_BUY_RATIO:
        score += 1

    if current_price >= previous_high:
        score += 1

    # Reject coins that may already be pumping too hard
    if move_5m > MAX_5M_MOVE:
        return None

    if score < MIN_SCORE:
        return None

    return {
        "symbol": symbol,
        "price": current_price,
        "volume_ratio": volume_ratio,
        "move": move_5m,
        "buy_ratio": buy_ratio,
        "distance": distance_from_high,
        "score": score,
    }


def make_alert(x):
    symbol = x["symbol"].replace("USDT", "")

    # These are risk-management reference levels,
    # not predictions.
    entry = x["price"]
    stop = entry * 0.93
    target = entry * 1.25

    return (
        f"🚨 EARLY BREAKOUT\n\n"
        f"🟢 WATCH / POSSIBLE BUY — {symbol}\n\n"
        f"Price: {entry:.8g}\n"
        f"5m move: +{x['move']:.2f}%\n"
        f"Volume: {x['volume_ratio']:.1f}× normal\n"
        f"Buy pressure: {x['buy_ratio'] * 100:.0f}%\n"
        f"Signal score: {x['score']}/6\n\n"
        f"Reference stop: {stop:.8g} (-7%)\n"
        f"Reference target: {target:.8g} (+25%)\n\n"
        f"⚠️ Verify before entering — breakout signals can fail."
    )


def can_alert(symbol):
    previous = last_alert.get(symbol, 0)

    return time.time() - previous > ALERT_COOLDOWN


def scan():
    symbols = get_symbols()

    print(f"Scanning {len(symbols)} liquid USDT markets...")

    candidates = []

    for symbol in symbols:
        try:
            result = analyse(symbol)

            if result:
                candidates.append(result)

            time.sleep(0.04)

        except Exception as e:
            print(symbol, e)

    candidates.sort(
        key=lambda x: (
            x["score"],
            x["volume_ratio"],
            x["buy_ratio"],
        ),
        reverse=True,
    )

    # Only alert the strongest few signals
    for candidate in candidates[:3]:

        symbol = candidate["symbol"]

        if can_alert(symbol):
            telegram(make_alert(candidate))
            last_alert[symbol] = time.time()


def main():
    telegram(
        "🟢 Armin Market Scanner ONLINE\n"
        "Scanning liquid crypto markets every minute."
    )

    while True:

        try:
            scan()

        except Exception as e:
            print("SCAN ERROR:", e)

        time.sleep(SCAN_EVERY)


if __name__ == "__main__":
    main()
