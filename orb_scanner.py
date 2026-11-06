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
    python orb_scanner.py --backtest 7d   # list every signal of the last 7 days (max 60d)
"""
import os, re, sys, json, datetime as dt
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
RR = 2.0                  # backtest target = RR x risk
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


def download(symbols, period="1d"):
    frames = {}
    for k in range(0, len(symbols), CHUNK):
        part = symbols[k:k + CHUNK]
        try:
            raw = yf.download(part, period=period, interval="15m", group_by="ticker",
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
        print("ERROR: TG_TOKEN or TG_CHAT_ID is empty. Secrets are missing or misnamed.")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT_ID, "text": msg}, timeout=15)
        if r.status_code != 200:
            print(f"ERROR: Telegram rejected the message. HTTP {r.status_code}: {r.text}")
            return False
        print("Telegram: message sent OK")
        return True
    except Exception as e:
        print("ERROR: could not reach Telegram:", e)
        return False


def send_file(path, caption=""):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        with open(path, "rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                              data={"chat_id": TG_CHAT_ID, "caption": caption},
                              files={"document": f}, timeout=30)
            if r.status_code != 200:
                print(f"ERROR: Telegram rejected the file. HTTP {r.status_code}: {r.text}")
    except Exception as e:
        print("Telegram file error:", e)


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
def simulate(d, t, side, entry, stop):
    """Walk candles after the breakout. Stop is checked before target in the same
    candle (conservative). Returns (R multiple, exit reason)."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None, "NA"
    target = entry + RR * risk if side == "BUY" else entry - RR * risk
    idx = d.index.get_loc(t)
    for j in range(idx + 1, len(d)):
        h, l = float(d["High"].iloc[j]), float(d["Low"].iloc[j])
        if side == "BUY":
            if l <= stop:
                return -1.0, "STOP"
            if h >= target:
                return RR, "TARGET"
        else:
            if h >= stop:
                return -1.0, "STOP"
            if l <= target:
                return RR, "TARGET"
    last = float(d["Close"].iloc[-1])
    r = (last - entry) / risk if side == "BUY" else (entry - last) / risk
    return round(r, 2), "EOD"


def backtest(period):
    now = dt.datetime.now(IST)
    data = download(get_symbols(), period)
    print(f"Loaded 15m data for {len(data)} stocks")
    rows = []
    for s, df in data.items():
        df = to_ist(df)
        for day in sorted(set(df.index.date)):
            d = df[df.index.date == day]
            live_day = day == now.date()
            if live_day:
                d = d[d.index + CANDLE <= now]             # ignore the still-forming candle
            res = find_breakout(d)
            if not res:
                continue
            side, t, c, hi, lo, n_in = res
            mid = (hi + lo) / 2
            r_box, why = simulate(d, t, side, c, lo if side == "BUY" else hi)
            r_mid, _ = simulate(d, t, side, c, mid)
            if live_day and now.hour * 60 + now.minute < 15 * 60 + 30:
                why = "OPEN"                               # day not finished yet
            rows.append({"date": str(day), "time": t.strftime("%H:%M"),
                         "stock": s.replace(".NS", ""), "side": side,
                         "box_low": round(lo, 2), "box_high": round(hi, 2),
                         "box_width_pct": round((hi - lo) / c * 100, 2),
                         "entry_close": round(c, 2), "inside_candles": n_in,
                         "R_stop_box_edge": r_box, "exit_box_edge": why,
                         "R_stop_box_mid": r_mid})
    if not rows:
        send(f"Backtest ({period}): no signals found.")
        return
    out = pd.DataFrame(rows).sort_values(["date", "time", "stock"])
    out.to_csv("backtest_results.csv", index=False)
    done = out[out["exit_box_edge"] != "OPEN"].dropna(subset=["R_stop_box_edge"])

    def stats(g, col):
        if g.empty:
            return "no trades"
        return (f"{len(g)} trades | win {(g[col] > 0).mean() * 100:.0f}% | "
                f"avg {g[col].mean():+.2f}R | total {g[col].sum():+.1f}R")

    head = [f"📊 Backtest {period}: {len(out)} signals on {out['date'].nunique()} days",
            f"Entry = breakout candle close, target = {RR:g}R, exit at day end if neither hit.",
            "",
            "Stop at far edge of box:",
            "  ALL  " + stats(done, "R_stop_box_edge"),
            "  BUY  " + stats(done[done.side == "BUY"], "R_stop_box_edge"),
            "  SELL " + stats(done[done.side == "SELL"], "R_stop_box_edge"),
            "Stop at middle of box:",
            "  ALL  " + stats(done, "R_stop_box_mid"),
            "",
            "By box width (far-edge stop):"]
    done = done.assign(bucket=pd.cut(done["box_width_pct"], [0, 0.5, 1, 2, 100],
                                     labels=["<0.5%", "0.5-1%", "1-2%", ">2%"]))
    for name, g in done.groupby("bucket", observed=True):
        head.append(f"  {name}: " + stats(g, "R_stop_box_edge"))
    send("\n".join(head))

    lines = []
    for day, g in out.groupby("date"):
        lines.append(f"\n📅 {day} - {len(g)} signals")
        for _, r in g.iterrows():
            icon = "🟢" if r["side"] == "BUY" else "🔴"
            res_txt = "open" if r["exit_box_edge"] == "OPEN" else f"{r['R_stop_box_edge']:+.2f}R {r['exit_box_edge']}"
            lines.append(f"{icon} {r['stock']} {r['side']} {r['time']} | box {r['box_low']}-{r['box_high']} "
                         f"({r['box_width_pct']}%) | {res_txt}")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 3500:
            send(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        send(chunk)
    send_file("backtest_results.csv", f"Backtest {period} (open in Excel / Sheets)")


def main():
    if "--ping" in sys.argv:
        if not send("✅ Telegram connection works. ORB scanner is ready."):
            sys.exit(1)                                    # make the workflow show a red cross
        return

    if "--backtest" in sys.argv:
        i = sys.argv.index("--backtest")
        arg = sys.argv[i + 1] if len(sys.argv) > i + 1 else "7d"
        if not re.fullmatch(r"\d{1,2}d", arg) or not 1 <= int(arg[:-1]) <= 60:
            arg = "7d"
        backtest(arg)
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
    print(f"Loaded 15m data for {len(data)} stocks")
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
    if force and found == 0:
        send(f"[TEST] Scan finished. Stocks scanned: {len(data)}. Signals in last session: 0.")
    print(f"Done. New signals: {found}")


if __name__ == "__main__":
    main()
