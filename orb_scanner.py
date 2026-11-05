"""
Nifty 500 - 15-minute Opening Range Box breakout scanner -> Telegram

Rules
  Box      : high/low of the first 15m candle (09:15-09:30), wicks included
  Inside   : candles 2..n must stay inside the box (high <= box high, low >= box low)
             minimum 3 inside candles, maximum 7 inside candles
  Breakout : the first 15m candle that CLOSES above box high (BUY) or
             below box low (SELL). Wick-only moves are not a breakout.
  Cutoff   : last valid breakout candle is 11:15-11:30 (9th candle)

Run once per call (GitHub Actions schedules it). Each stock alerts once per day.
    python orb_scanner.py --ping     # send a test message to Telegram
    python orb_scanner.py --force    # test mode: scan the last session, ignore time window
"""
import os, sys, json, datetime as dt
from io import StringIO
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
IST = ZoneInfo("Asia/Kolkata")

MIN_INSIDE = 3            # minimum inside candles before breakout
MAX_INSIDE = 7            # maximum inside candles (breakout then on candle 9 = 11:15-11:30)
STRICT_INSIDE = True      # True  = a wick outside the box cancels the setup (same as your Chartink code)
                          # False = only CLOSES outside the box matter; wicks are ignored
CANDLE = pd.Timedelta(minutes=15)
CHUNK = 100
STATE_FILE = "sent.json"
NIFTY500_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"


# ------------------------------------------------------------ helpers
def get_symbols():
    try:
        r = requests.get(NIFTY500_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        df.to_csv("nifty500.csv", index=False)
    except Exception as e:
        print("Could not download list, using local nifty500.csv:", e)
        df = pd.read_csv("nifty500.csv")
    return [s.strip() + ".NS" for s in df["Symbol"]]


def download(symbols):
    frames = {}
    for k in range(0, len(symbols), CHUNK):
        part = symbols[k:k + CHUNK]
        try:
            raw = yf.download(part, period="1d", interval="15m", group_by="ticker",
                              progress=False, threads=True, auto_adjust=False)
        except Exception as e:
            print("download error:", e)
            continue
        for s in part:
            try:
                df = raw[s].dropna(how="all")
                if not df.empty:
                    frames[s] = df
            except KeyError:
                pass
    return frames


def to_ist(df):
    df = df.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(IST)
    return df


def send(msg):
    print(msg, "\n")
    if not TG_TOKEN or not TG_CHAT_ID:
        print("(Telegram keys missing, message not sent)")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT_ID, "text": msg}, timeout=15)
    except Exception as e:
        print("Telegram error:", e)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


# ------------------------------------------------------------ strategy
def find_breakout(d):
    """d = today's COMPLETED 15m candles, first row must be the 09:15 candle."""
    if len(d) < MIN_INSIDE + 2:
        return None
    if d.index[0].strftime("%H:%M") != "09:15":
        return None
    hi, lo = float(d["High"].iloc[0]), float(d["Low"].iloc[0])
    inside = 0

    for idx in range(1, len(d)):
        if d.index[idx] != d.index[0] + CANDLE * idx:      # missing candle in data
            return None
        h, l, c = (float(d[x].iloc[idx]) for x in ("High", "Low", "Close"))

        if c > hi or c < lo:                               # candle CLOSED outside the box
            if MIN_INSIDE <= inside <= MAX_INSIDE:
                side = "BUY" if c > hi else "SELL"
                return side, d.index[idx], c, hi, lo, inside
            return None                                    # broke out too early

        if STRICT_INSIDE and not (h <= hi and l >= lo):    # wick poked outside the box
            return None
        inside += 1
        if inside > MAX_INSIDE:                            # consolidation too long
            return None
    return None


# ------------------------------------------------------------ main
def main():
    if "--ping" in sys.argv:
        send("✅ Telegram connection works. ORB scanner is ready.")
        return

    force = "--force" in sys.argv
    now = dt.datetime.now(IST)
    if not force:
        start = now.replace(hour=10, minute=30, second=0, microsecond=0)
        end = now.replace(hour=12, minute=20, second=0, microsecond=0)
        if now.weekday() >= 5 or not (start <= now <= end):
            print("Outside scan window:", now)
            return

    data = download(get_symbols())
    state = load_state()
    day_key = now.strftime("%Y-%m-%d")
    sent = set(state.get(day_key, []))
    found = 0

    for s, df in data.items():
        df = to_ist(df)
        day = df.index.date.max() if force else now.date()
        d = df[df.index.date == day]
        if not force:
            d = d[d.index + CANDLE <= now]                 # completed candles only
        res = find_breakout(d)
        if not res:
            continue
        side, t, c, hi, lo, n_in = res
        key = f"{s}|{side}"
        if key in sent and not force:
            continue
        icon = "🟢" if side == "BUY" else "🔴"
        name = s.replace(".NS", "")
        send(f"{icon}{' [TEST]' if force else ''} {side}: {name}\n"
             f"15m candle closed {'above' if side == 'BUY' else 'below'} the opening box\n"
             f"Box: {lo:.2f} - {hi:.2f}\n"
             f"Close: {c:.2f}\n"
             f"Inside candles: {n_in}\n"
             f"Candle: {t.strftime('%H:%M')} - {(t + CANDLE).strftime('%H:%M')}")
        sent.add(key)
        found += 1

    if not force:
        with open(STATE_FILE, "w") as f:
            json.dump({day_key: sorted(sent)}, f)
    print(f"Done. New signals: {found}")


if __name__ == "__main__":
    main()
