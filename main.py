import csv
import json
import os
import re
import time
from datetime import datetime, timezone

import requests
from tokenearly import Client

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CG_KEY = os.environ.get("COINGECKO_API_KEY", "")
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS") or 0)
STATE_FILE = "tracking.json"
CSV_FILE = "results.csv"
CG = "https://api.coingecko.com/api/v3"

ONLY_EXCHANGES = []   # khali = saare. Ya ["binance", "mexc"]
MAX_NEW_PER_RUN = 40  # ek run mein kitne naye listings uthane hain
GRACE = 21600         # data na mile to itne second tak dobara try
H, D = 3600, 86400

# (naam, ref, offset). ref "a" = announcement time, "l" = trading start
CHECKPOINTS = [("A-3d", "a", -3 * D), ("A-1h", "a", -H), ("A", "a", 0), ("A+1h", "a", H),
               ("L-24h", "l", -D), ("L-1h", "l", -H), ("L", "l", 0), ("L+1h", "l", H),
               ("L+3h", "l", 3 * H), ("L+6h", "l", 6 * H), ("L+24h", "l", D), ("L+3d", "l", 3 * D)]
BTC = []


def cg_get(path, params=None):
    headers = {"x-cg-demo-api-key": CG_KEY} if CG_KEY else {}
    for _ in range(3):
        time.sleep(2)
        try:
            r = requests.get(CG + path, params=params, headers=headers, timeout=20)
            if r.status_code == 429:
                time.sleep(25)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"CoinGecko error {path}: {e}")
    return None


def find_coin(symbol):
    data = cg_get("/search", {"query": symbol})
    for c in (data or {}).get("coins", []):
        if c.get("symbol", "").upper() == symbol.upper():
            return c["id"]
    return None


def exchange_count(coin_id):
    d = cg_get(f"/coins/{coin_id}", {"localization": "false", "tickers": "true",
                                     "market_data": "false", "community_data": "false",
                                     "developer_data": "false", "sparkline": "false"})
    if not d:
        return None
    return len({t.get("market", {}).get("identifier") for t in d.get("tickers", [])})


def coin_series(coin_id, lo, hi):
    d = cg_get(f"/coins/{coin_id}/market_chart/range",
               {"vs_currency": "usd", "from": int(lo), "to": int(hi)})
    if not d:
        return []
    pr, mc, vo = d.get("prices", []), d.get("market_caps", []), d.get("total_volumes", [])
    return [(t / 1000, p, mc[i][1] if i < len(mc) else None, vo[i][1] if i < len(vo) else None)
            for i, (t, p) in enumerate(pr)]


def btc_series():
    d = cg_get("/coins/bitcoin/market_chart", {"vs_currency": "usd", "days": "90"})
    return [(t / 1000, p, None, None) for t, p in (d or {}).get("prices", [])]


def nearest(series, ts, tol=5400):
    best = min(series, key=lambda s: abs(s[0] - ts), default=None)
    if best is None or abs(best[0] - ts) > tol:
        return None
    return best


def to_ts(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v / 1000) if v > 1e11 else int(v)
    try:
        dt = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def parse_time(text):
    # Sirf saaf "UTC" wala time (UTC+8 jaisa nahi, warna galat ho jata hai)
    text = text or ""
    m = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})\s*\(?UTC(?![+\-\d])", text)
    if m:
        pattern, s = "%Y-%m-%d %H:%M", f"{m.group(1)} {m.group(2)}"
    else:
        m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})(?: at)?\s+(\d{2}:\d{2})\s*\(?UTC(?![+\-\d])", text)
        if not m:
            return None
        pattern, s = "%B %d, %Y %H:%M", f"{m.group(1)} {m.group(2)}"
    try:
        return int(datetime.strptime(s, pattern).replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return None


def listing_time(item):
    ts = parse_time(item.headline("en"))
    if ts:
        return ts
    try:
        r = requests.get(item.source_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        return parse_time(re.sub(r"<[^>]+>", " ", r.text))
    except Exception as e:
        print("Page fetch fail:", e)
        return None


def pct(a, b):
    return (a / b - 1) * 100 if a and b else None


def fmt(x):
    return "n/a" if x is None else f"{x:+.1f}%"


def ret(it, name, base):
    """name ka return base se (%), aur BTC-adjusted return."""
    r = pct(it["p"].get(name), it["p"].get(base))
    br = pct(it["b"].get(name), it["b"].get(base))
    return r, (r - br if r is not None and br is not None else None)


def row(it, label, name, base):
    if name not in it["p"] or base not in it["p"]:
        return f"{label}: abhi baaki"
    r, adj = ret(it, name, base)
    return f"{label}: {fmt(r)} (BTC-adj {fmt(adj)})"


def hint(pre, p24):
    if p24 is None:
        return "Data kam hai, andaza nahi"
    if pre is None:
        return "Data kam hai, andaza nahi"
    if pre >= 10 and p24 < 0:
        return "Trading se pehle hi pump ho chuka tha, baad mein giravat (sell-the-news)"
    if pre < 5 and p24 >= 10:
        return "Pehle shaant tha, listing ke baad naya demand aaya"
    if abs(p24) < 3:
        return "Koi khaas asar nahi dikha"
    return "Mila-jula asar, koi saaf pattern nahi"


def send(text):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text})
    print("Telegram:", r.status_code)


def report(it, final, mx=None, mn=None):
    t = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %H:%M")
    mc, vr = it.get("mcap"), it.get("vol_ratio")
    lines = [f"📊 {'FINAL ' if final else ''}IMPACT REPORT",
             f"{it['symbol']} | {it['exchange']} {it['type'].upper()}",
             it["headline"],
             f"Announcement: {t(it['t0'])} UTC",
             f"Trading start: {t(it['tl'])} UTC" + ("" if it["exact"] else " (nahi mila, announcement time liya)"),
             f"Market cap: {'$' + format(mc, ',.0f') if mc else 'n/a'} | Exchanges (abhi): {it.get('ex') or 'n/a'}",
             "", "Announcement ke hisaab se:",
             row(it, "3 din pehle → announcement", "A", "A-3d"),
             row(it, "1h pehle → announcement", "A", "A-1h"),
             row(it, "announcement ke 1h baad", "A+1h", "A"),
             f"Volume (pichhle 24h vs pehle 2 din): {vr:.1f}x" if vr else "Volume: n/a",
             "", "Trading start ke hisaab se:",
             row(it, "announcement → start", "L", "A"),
             row(it, "24h pehle → start", "L", "L-24h"),
             row(it, "1h pehle → start", "L", "L-1h")]
    for n in ("L+1h", "L+3h", "L+6h", "L+24h", "L+3d"):
        lines.append(row(it, n[1:], n, "L"))
    if final:
        lines += ["", f"Max pump: {fmt(mx)} | Max dump: {fmt(mn)}",
                  f"Hint: {hint(ret(it, 'L', 'A-3d')[0], ret(it, 'L+24h', 'L')[0])}"]
    lines.append(it["url"])
    return "\n".join(lines)


def extremes(it):
    base = it["p"].get("L")
    if not base:
        return None, None
    pts = [s[1] for s in coin_series(it["coin_id"], it["tl"], it["tl"] + 3 * D)]
    if not pts:
        return None, None
    return pct(max(pts), base), pct(min(pts), base)


def save_row(it, mx, mn):
    g = lambda n, b: ret(it, n, b)[0]
    new_file = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["trading_start_utc", "symbol", "exchange", "type", "time_exact",
                        "mcap", "exchanges_now", "vol_ratio_pre", "runup_3d_to_ann",
                        "1h_before_ann", "1h_after_ann", "ann_to_start", "24h_before_start",
                        "1h_before_start", "1h", "3h", "6h", "24h", "3d",
                        "btc_adj_1h", "btc_adj_24h", "btc_adj_3d", "max_pump", "max_dump", "url"])
        w.writerow([datetime.fromtimestamp(it["tl"], timezone.utc).isoformat(), it["symbol"],
                    it["exchange"], it["type"], it["exact"], it.get("mcap"), it.get("ex"),
                    it.get("vol_ratio"),
                    g("A", "A-3d"), g("A", "A-1h"), g("A+1h", "A"), g("L", "A"),
                    g("L", "L-24h"), g("L", "L-1h"),
                    g("L+1h", "L"), g("L+3h", "L"), g("L+6h", "L"), g("L+24h", "L"), g("L+3d", "L"),
                    ret(it, "L+1h", "L")[1], ret(it, "L+24h", "L")[1], ret(it, "L+3d", "L")[1],
                    mx, mn, it["url"]])


def update(it, now):
    """Series se checkpoints bharta hai. True = koi naya price mila."""
    cps = [(n, (it["tl"] if r == "l" else it["t0"]) + o) for n, r, o in CHECKPOINTS]
    pending = [(n, ts) for n, ts in cps if n not in it["p"] and ts <= now]
    if not pending:
        return False
    lo = it["t0"] - 3 * D - H
    hi = min(now, max(ts for _, ts in cps)) + H
    series = coin_series(it["coin_id"], lo, hi)
    new = False
    for n, ts in pending:
        s = nearest(series, ts)
        if s is None and now < ts + GRACE:
            continue
        it["p"][n] = s[1] if s else None
        b = nearest(BTC, ts) if BTC else None
        it["b"][n] = b[1] if b else None
        new = new or s is not None
    if "mcap" not in it and it["p"].get("A") is not None:
        ref = it["t0"]
        s = nearest(series, ref)
        it["mcap"] = s[2] if s else None
        last = [x[3] for x in series if ref - D <= x[0] <= ref and x[3]]
        prior = [x[3] for x in series if ref - 3 * D <= x[0] < ref - D and x[3]]
        if last and prior:
            it["vol_ratio"] = (sum(last) / len(last)) / (sum(prior) / len(prior))
    return new


def main():
    global BTC
    state = {"items": {}, "seen": []}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            loaded = json.load(f)
        if "items" in loaded:
            state = loaded
    items, seen = state["items"], set(state["seen"])
    now = int(time.time())
    BTC = btc_series()

    added = 0
    days = min(BACKFILL_DAYS, 30) if BACKFILL_DAYS > 0 else 1
    for item in Client().listings(days=days):
        key = item.permalink
        if key in seen or not item.symbols:
            continue
        if ONLY_EXCHANGES and not any(x in item.exchange_name.lower() for x in ONLY_EXCHANGES):
            continue
        if added >= MAX_NEW_PER_RUN:
            break
        t0 = to_ts(getattr(item, "published_at", None)) or now
        tl = listing_time(item)
        exact = tl is not None and t0 - H <= tl <= t0 + 14 * D
        if not exact:
            tl = t0
        items[key] = {"symbol": item.symbols[0], "exchange": item.exchange_name,
                      "type": item.type, "headline": item.headline("en"),
                      "url": item.source_url, "t0": t0, "tl": tl, "exact": exact,
                      "coin_id": None, "ex": None, "p": {}, "b": {},
                      "silent": BACKFILL_DAYS > 0}
        seen.add(key)
        added += 1
    print(f"Naye track: {added}")

    for key in list(items):
        it = items[key]
        if not it["coin_id"]:
            it["coin_id"] = find_coin(it["symbol"])
            if it["coin_id"]:
                it["ex"] = exchange_count(it["coin_id"])
            elif now > it["t0"] + GRACE:
                print("CoinGecko pe nahi mila, chhod diya:", it["symbol"])
                del items[key]
                continue
        new = update(it, now) if it["coin_id"] else False
        if "A" in it["p"] and it["p"]["A"] is None:
            print("Fresh coin (announcement pe price nahi), chhod diya:", it["symbol"])
            del items[key]
            continue
        if now > it["tl"] + 3 * D + GRACE:
            for n, _, _ in CHECKPOINTS:
                it["p"].setdefault(n, None)
                it["b"].setdefault(n, None)
        if all(n in it["p"] for n, _, _ in CHECKPOINTS):
            if any(v is not None for v in it["p"].values()):
                mx, mn = extremes(it)
                if not it["silent"]:
                    send(report(it, True, mx, mn))
                save_row(it, mx, mn)
            del items[key]
        elif new and not it["silent"]:
            send(report(it, False))

    with open(STATE_FILE, "w") as f:
        json.dump({"items": items, "seen": sorted(seen)}, f)


if __name__ == "__main__":
    main()
