"""
╔═══════════════════════════════════════════════════════════════════╗
║         XAUUSD GOLD SNIPER — Version ELITE v3                     ║
║  Liquidité | SMC | Order Flow | News Offensif | Canal vivant      ║
╚═══════════════════════════════════════════════════════════════════╝
"""

import os, asyncio, logging, random, sys
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
import requests
import pandas as pd
import numpy as np
from telegram import Bot
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Fix encodage Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# ─── CONFIG ───────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8314000586:AAHDswO3HfnNmhqC8IyzTOEeYYNYWnx_Puw")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "@sniperzossie")
TWELVE_DATA_KEY    = os.getenv("TWELVE_DATA_KEY",     "951d12906f144383b7f98f8a7938dba3")

SYMBOL         = "XAU/USD"
TF_MAIN        = "15min"
TF_HIGH        = "1h"
TF_TREND       = "4h"
SCAN_SEC       = 60
COOLDOWN_MIN   = 45
MIN_CONFLUENCE = 65

# ─── NEWS : fenêtres en minutes ───────────────────────────────────
NEWS_WARN_BEFORE  = 20   # alerte canal X min avant
NEWS_SPIKE_WAIT   = 2    # attendre X min après annonce (spike)
NEWS_SNIPE_AFTER  = 5    # sniper le setup X min après (direction confirmée)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("GOLD-SNIPER")

# ─── CALENDRIER NEWS HEBDO (heure UTC fixe) ───────────────────────
# Format : { jour_semaine(0=Lun): [(heure, minute, "Nom", impact), ...] }
# Impact : "🔴" = majeur sur GOLD | "🟠" = modéré
NEWS_CALENDAR = {
    0: [  # Lundi
        (14, 0,  "ISM Manufacturing PMI", "🔴"),
    ],
    1: [  # Mardi
        (13, 30, "CPI USA (Inflation)",    "🔴"),
        (14, 0,  "CB Consumer Confidence", "🟠"),
    ],
    2: [  # Mercredi
        (13, 30, "PPI USA",                "🟠"),
        (18, 0,  "FOMC Minutes / Fed Rate","🔴"),
        (18, 30, "Discours Fed Chair",     "🔴"),
    ],
    3: [  # Jeudi
        (13, 30, "Jobless Claims USA",     "🟠"),
        (13, 30, "GDP USA",                "🔴"),
    ],
    4: [  # Vendredi
        (13, 30, "NFP (Non-Farm Payroll)", "🔴"),
        (13, 30, "Unemployment Rate",      "🔴"),
        (15, 0,  "Michigan Sentiment",     "🟠"),
    ],
    5: [],  # Samedi
    6: [],  # Dimanche
}

def get_upcoming_news(window_minutes=60):
    """Retourne les annonces dans la prochaine fenêtre de temps."""
    now = datetime.utcnow()
    dow = now.weekday()
    upcoming = []
    for (h, m, name, impact) in NEWS_CALENDAR.get(dow, []):
        news_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
        diff_min  = (news_time - now).total_seconds() / 60
        if -NEWS_SNIPE_AFTER <= diff_min <= window_minutes:
            upcoming.append({
                "name":     name,
                "impact":   impact,
                "time":     news_time,
                "diff_min": diff_min,
            })
    return upcoming

def news_status():
    """
    Retourne le statut news actuel :
    - "warn"  : annonce dans moins de NEWS_WARN_BEFORE min → alerter
    - "spike" : annonce il y a moins de NEWS_SPIKE_WAIT min → attendre spike
    - "snipe" : annonce il y a entre spike_wait et snipe_after → SNIPER
    - None    : aucune news proche
    """
    now = datetime.utcnow()
    dow = now.weekday()
    for (h, m, name, impact) in NEWS_CALENDAR.get(dow, []):
        news_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
        diff_min  = (news_time - now).total_seconds() / 60
        info = {"name": name, "impact": impact, "time": news_time, "diff_min": diff_min}
        if 0 < diff_min <= NEWS_WARN_BEFORE:
            return "warn",  info
        if -NEWS_SPIKE_WAIT <= diff_min <= 0:
            return "spike", info
        if -NEWS_SNIPE_AFTER <= diff_min < -NEWS_SPIKE_WAIT:
            return "snipe", info
    return None, None

# ─── STRUCTURES ───────────────────────────────────────────────────
@dataclass
class Signal:
    direction: str
    entry: float
    sl: float
    tps: list
    nb_tp: int
    atr: float
    score: int
    score_max: int
    confluence: dict
    timestamp: datetime
    is_news_trade: bool = False
    news_name: str = ""
    message_id: Optional[int] = None
    tp_hit: list = field(default_factory=list)
    active: bool = True
    closed_in_profit: bool = False

@dataclass
class Performance:
    total_signals: int = 0
    wins: int = 0
    losses: int = 0
    partial_wins: int = 0
    total_tps_hit: int = 0
    news_trades: int = 0

# ─── DONNÉES ──────────────────────────────────────────────────────
def fetch_ohlcv(interval: str, n: int = 150) -> pd.DataFrame:
    r = requests.get("https://api.twelvedata.com/time_series", params={
        "symbol": SYMBOL, "interval": interval,
        "outputsize": n, "apikey": TWELVE_DATA_KEY, "format": "JSON"
    }, timeout=10)
    d = r.json()
    if "values" not in d:
        raise ValueError(f"API error: {d}")
    df = pd.DataFrame(d["values"])
    df.rename(columns={"datetime": "time"}, inplace=True)
    for c in ["open","high","low","close"]:
        df[c] = df[c].astype(float)
    df["time"] = pd.to_datetime(df["time"])
    return df.sort_values("time").reset_index(drop=True)

def fetch_price() -> float:
    r = requests.get("https://api.twelvedata.com/price",
        params={"symbol": SYMBOL, "apikey": TWELVE_DATA_KEY}, timeout=5)
    return float(r.json()["price"])

def fetch_dxy() -> dict:
    """
    Récupère le DXY (Dollar Index) — corrélation inverse avec l'or.
    DXY monte = or baisse. DXY baisse = or monte.
    """
    try:
        r = requests.get("https://api.twelvedata.com/time_series", params={
            "symbol": "DX/Y", "interval": "1h",
            "outputsize": 20, "apikey": TWELVE_DATA_KEY, "format": "JSON"
        }, timeout=10)
        d = r.json()
        if "values" not in d:
            return {"trend": "neutral", "rsi": 50, "change": 0}
        df = pd.DataFrame(d["values"])
        df["close"] = df["close"].astype(float)
        df = df.sort_values("datetime").reset_index(drop=True)
        close = df["close"]
        rsi_v = rsi(close).iloc[-1]
        change = (close.iloc[-1] - close.iloc[-5]) / close.iloc[-5] * 100
        # DXY en hausse = baissier pour l'or
        if change > 0.15 and rsi_v > 55:  trend = "up"    # dollar fort = or bearish
        elif change < -0.15 and rsi_v < 45: trend = "down"  # dollar faible = or bullish
        else:                               trend = "neutral"
        return {"trend": trend, "rsi": round(rsi_v,1), "change": round(change,3)}
    except:
        return {"trend": "neutral", "rsi": 50, "change": 0}

# ─── INDICATEURS ──────────────────────────────────────────────────
def ema(s, p): return s.ewm(span=p, adjust=False).mean()

def rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(com=p-1, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(com=p-1, adjust=False).mean()
    return 100 - 100/(1 + g/l)

def atr(df, p=14):
    tr = pd.concat([
        df["high"]-df["low"],
        (df["high"]-df["close"].shift()).abs(),
        (df["low"]-df["close"].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()

def macd(s):
    m = ema(s,12) - ema(s,26)
    sig = ema(m,9)
    return m, sig, m-sig

def stoch_rsi(s, p=14, k=3, d=3):
    r_ = rsi(s, p)
    lo = r_.rolling(p).min()
    hi = r_.rolling(p).max()
    k_ = 100 * (r_ - lo) / (hi - lo + 1e-9)
    d_ = k_.rolling(d).mean()
    return k_.rolling(k).mean(), d_

def bollinger(s, p=20, std=2):
    mid = s.rolling(p).mean()
    dev = s.rolling(p).std()
    return mid+std*dev, mid, mid-std*dev

def williams_r(df, p=14):
    hh = df["high"].rolling(p).max()
    ll = df["low"].rolling(p).min()
    return -100 * (hh - df["close"]) / (hh - ll + 1e-9)

# ─── LIQUIDITÉ & SMC ──────────────────────────────────────────────
def detect_swing_highs_lows(df, left=3, right=3):
    swings = {"highs": [], "lows": []}
    for i in range(left, len(df)-right):
        win_h = df["high"].iloc[i-left:i+right+1]
        win_l = df["low"].iloc[i-left:i+right+1]
        if df["high"].iloc[i] == win_h.max():
            swings["highs"].append({"price": df["high"].iloc[i], "idx": i})
        if df["low"].iloc[i] == win_l.min():
            swings["lows"].append({"price": df["low"].iloc[i], "idx": i})
    return swings

def detect_liquidity_grab(df, swings):
    last = df.iloc[-1]; prev = df.iloc[-2]
    grabs = []
    for sw in swings["lows"][-5:]:
        if prev["low"] < sw["price"] and last["close"] > sw["price"]:
            grabs.append({"type": "bullish_grab", "level": sw["price"]})
    for sw in swings["highs"][-5:]:
        if prev["high"] > sw["price"] and last["close"] < sw["price"]:
            grabs.append({"type": "bearish_grab", "level": sw["price"]})
    return grabs

def detect_equal_highs_lows(df, tolerance=0.05):
    levels = {"eq_highs": [], "eq_lows": []}
    highs = df["high"].values; lows = df["low"].values
    for i in range(len(highs)-10, len(highs)):
        for j in range(max(0,i-20), i):
            if abs(highs[i]-highs[j]) / highs[j] * 100 < tolerance:
                levels["eq_highs"].append(highs[i])
            if abs(lows[i]-lows[j]) / lows[j] * 100 < tolerance:
                levels["eq_lows"].append(lows[i])
    return levels

def detect_fvg(df):
    fvgs = []
    for i in range(2, len(df)):
        c1h, c1l = df["high"].iloc[i-2], df["low"].iloc[i-2]
        c3h, c3l = df["high"].iloc[i],   df["low"].iloc[i]
        if c3l > c1h: fvgs.append({"type":"bull","top":c3l,"bot":c1h,"idx":i})
        if c3h < c1l: fvgs.append({"type":"bear","top":c1l,"bot":c3h,"idx":i})
    return fvgs[-6:]

def detect_order_blocks(df):
    obs = []
    for i in range(1, len(df)-2):
        body    = abs(df["close"].iloc[i] - df["open"].iloc[i])
        move    = abs(df["close"].iloc[i+1] - df["open"].iloc[i+1])
        move2   = abs(df["close"].iloc[i+2] - df["open"].iloc[i+2]) if i+2 < len(df) else 0
        impulse = move + move2
        if impulse > body * 2.5:
            t = "bull" if df["close"].iloc[i+1] > df["open"].iloc[i+1] else "bear"
            obs.append({"type":t,"high":df["high"].iloc[i],"low":df["low"].iloc[i],"idx":i})
    return obs[-5:]

def detect_bos_choch(df):
    result = {"bos": None, "choch": None}
    highs = df["high"].values; lows = df["low"].values
    if len(df) < 10: return result
    if highs[-1] > highs[-5:-1].max(): result["bos"] = "bullish"
    elif lows[-1] < lows[-5:-1].min(): result["bos"] = "bearish"
    recent_trend = "up" if highs[-5] > highs[-10] else "down"
    if recent_trend == "up"   and lows[-1] < lows[-5:-1].min():   result["choch"] = "bearish"
    elif recent_trend == "down" and highs[-1] > highs[-5:-1].max(): result["choch"] = "bullish"
    return result

def detect_premium_discount(df):
    high  = df["high"].iloc[-50:].max()
    low   = df["low"].iloc[-50:].min()
    price = df["close"].iloc[-1]
    pct   = (price - low) / (high - low) * 100
    if pct > 60:   return "premium", pct
    elif pct < 40: return "discount", pct
    return "equilibrium", pct

# ─── ANALYSE NEWS POST-SPIKE ──────────────────────────────────────
def analyse_news_direction(df: pd.DataFrame) -> Optional[str]:
    """
    Après le spike news, on lit la direction en 3 bougies M1/M5.
    Retourne "BUY", "SELL" ou None si pas de direction claire.
    """
    if len(df) < 5: return None
    last3 = df.iloc[-3:]
    bulls = sum(1 for _, r in last3.iterrows() if r["close"] > r["open"])
    bears = sum(1 for _, r in last3.iterrows() if r["close"] < r["open"])
    body_total = sum(abs(r["close"]-r["open"]) for _, r in last3.iterrows())
    atr_val = atr(df).iloc[-1]
    # Direction claire si 2/3 bougies dans le même sens ET body > 1.5x ATR
    if bulls >= 2 and body_total > atr_val * 1.5: return "BUY"
    if bears >= 2 and body_total > atr_val * 1.5: return "SELL"
    return None

# ─── MOTEUR D'ANALYSE PRINCIPAL ───────────────────────────────────
def analyse_full(df_m15, df_h1, df_h4, news_mode=False, dxy=None):
    """
    Score sur 100 pts + bonus DXY (15 pts).
    Signal si score >= MIN_CONFLUENCE%.
    En mode news, le seuil est abaissé à 55% (momentum fort).
    """
    threshold = 55 if news_mode else MIN_CONFLUENCE
    close   = df_m15["close"]
    price   = close.iloc[-1]
    atr_val = atr(df_m15).iloc[-1]
    atr_pct = atr_val / price * 100
    high_vol = atr_pct > 0.35

    directions = {"BUY": 0, "SELL": 0}

    # 1. Tendance multi-TF (20 pts)
    e8, e21, e50, e200 = ema(close,8).iloc[-1], ema(close,21).iloc[-1], ema(close,50).iloc[-1], ema(close,200).iloc[-1]
    e21_h1  = ema(df_h1["close"],21).iloc[-1]
    e200_h1 = ema(df_h1["close"],200).iloc[-1]
    e21_h4  = ema(df_h4["close"],21).iloc[-1]
    tb = (e8>e21>e50)+(price>e200)+(df_h1["close"].iloc[-1]>e21_h1)+(df_h1["close"].iloc[-1]>e200_h1)+(df_h4["close"].iloc[-1]>e21_h4)
    ts = (e8<e21<e50)+(price<e200)+(df_h1["close"].iloc[-1]<e21_h1)+(df_h1["close"].iloc[-1]<e200_h1)+(df_h4["close"].iloc[-1]<e21_h4)
    directions["BUY"]+=tb*4; directions["SELL"]+=ts*4

    # 2. RSI (9 pts)
    rsi_v  = rsi(close).iloc[-1]
    rsi_h1 = rsi(df_h1["close"]).iloc[-1]
    rb = (45<rsi_v<65)+(rsi_v>rsi(close).iloc[-2])+(rsi_h1>50)
    rs = (35<rsi_v<55)+(rsi_v<rsi(close).iloc[-2])+(rsi_h1<50)
    directions["BUY"]+=rb*3; directions["SELL"]+=rs*3

    # 3. MACD (12 pts)
    m,ms,mh = macd(close)
    mcb = int(mh.iloc[-1]>0 and mh.iloc[-2]<=0)
    mcs = int(mh.iloc[-1]<0 and mh.iloc[-2]>=0)
    mab = int(m.iloc[-1]>0); mas = int(m.iloc[-1]<0)
    directions["BUY"]+=(mcb*4+mab*2); directions["SELL"]+=(mcs*4+mas*2)

    # 4. Stoch RSI (4 pts)
    sk,sd = stoch_rsi(close)
    srb = int(sk.iloc[-1]>sd.iloc[-1] and sk.iloc[-2]<=sd.iloc[-2] and sk.iloc[-1]<80)
    srs = int(sk.iloc[-1]<sd.iloc[-1] and sk.iloc[-2]>=sd.iloc[-2] and sk.iloc[-1]>20)
    directions["BUY"]+=srb*4; directions["SELL"]+=srs*4

    # 5. Bollinger (5 pts)
    bb_up,_,bb_low = bollinger(close)
    directions["BUY"] +=int(price<=bb_low.iloc[-1]*1.001)*5
    directions["SELL"]+=int(price>=bb_up.iloc[-1]*0.999)*5

    # 6. Williams %R (5 pts)
    wr = williams_r(df_m15).iloc[-1]
    directions["BUY"] +=int(wr<-80)*5; directions["SELL"]+=int(wr>-20)*5

    # 7. Liquidity Grab (20 pts)
    swings = detect_swing_highs_lows(df_m15)
    grabs  = detect_liquidity_grab(df_m15, swings)
    gb = sum(1 for g in grabs if g["type"]=="bullish_grab")
    gs = sum(1 for g in grabs if g["type"]=="bearish_grab")
    directions["BUY"]+=gb*10; directions["SELL"]+=gs*10

    # 8. Equal Highs/Lows (8 pts)
    eq = detect_equal_highs_lows(df_m15)
    eqh = any(abs(price-h)/price*100<0.1 for h in eq["eq_highs"])
    eql = any(abs(price-l)/price*100<0.1 for l in eq["eq_lows"])
    directions["SELL"]+=int(eqh)*8; directions["BUY"]+=int(eql)*8

    # 9. Order Blocks (12 pts)
    obs = detect_order_blocks(df_m15)
    ob = sum(1 for o in obs if o["type"]=="bull" and o["low"]*0.998<=price<=o["high"]*1.002)
    os_ = sum(1 for o in obs if o["type"]=="bear" and o["low"]*0.998<=price<=o["high"]*1.002)
    directions["BUY"]+=ob*6; directions["SELL"]+=os_*6

    # 10. FVG (10 pts)
    fvgs = detect_fvg(df_m15)
    fb = sum(1 for f in fvgs if f["type"]=="bull" and f["bot"]<=price<=f["top"]*1.002)
    fs = sum(1 for f in fvgs if f["type"]=="bear" and f["bot"]*0.998<=price<=f["top"])
    directions["BUY"]+=fb*5; directions["SELL"]+=fs*5

    # 11. BOS/CHoCH (15 pts)
    bc = detect_bos_choch(df_m15)
    if bc["choch"]=="bullish":  directions["BUY"] +=10
    if bc["choch"]=="bearish":  directions["SELL"]+=10
    if bc["bos"]  =="bullish":  directions["BUY"] +=5
    if bc["bos"]  =="bearish":  directions["SELL"]+=5

    # 12. Premium/Discount (7 pts)
    zone, zone_pct = detect_premium_discount(df_m15)
    if zone=="discount": directions["BUY"] +=7
    elif zone=="premium":directions["SELL"]+=7

    # 13. DXY — Dollar Index (15 pts — corrélation inverse avec l'or)
    dxy_trend = dxy["trend"] if dxy else "neutral"
    dxy_rsi   = dxy["rsi"]   if dxy else 50
    dxy_change = dxy["change"] if dxy else 0
    # DXY baisse = or monte (BUY)
    if dxy_trend == "down":
        directions["BUY"]  += 15
    # DXY monte = or baisse (SELL)
    elif dxy_trend == "up":
        directions["SELL"] += 15
    # Confirmation partielle par RSI DXY
    if dxy_rsi < 40: directions["BUY"]  += 5
    if dxy_rsi > 60: directions["SELL"] += 5

    buy_s  = min(directions["BUY"],  100)
    sell_s = min(directions["SELL"], 100)

    direction = None
    score = 0
    if buy_s >= threshold and buy_s > sell_s:
        direction = "BUY";  score = buy_s
    elif sell_s >= threshold and sell_s > buy_s:
        direction = "SELL"; score = sell_s
    else:
        return None

    entry = price

    # ── SL basé sur la vraie structure du marché ──────────────────
    # On regarde les 20 dernières bougies H1 pour trouver le dernier
    # swing significatif — pas juste les 5 dernières M15
    h1_close = df_h1["close"]
    h1_atr   = atr(df_h1).iloc[-1]

    if direction == "BUY":
        # SL = sous le dernier swing low H1 significatif
        swing_low  = df_h1["low"].iloc[-20:].min()
        sl_struct  = round(swing_low - h1_atr * 0.3, 2)
        # SL minimum = 3$ sous l'entrée (anti stop-hunt)
        sl_min     = round(entry - max(3.0, h1_atr * 1.5), 2)
        sl         = min(sl_struct, sl_min)  # le plus bas des deux
    else:
        # SL = au-dessus du dernier swing high H1 significatif
        swing_high = df_h1["high"].iloc[-20:].max()
        sl_struct  = round(swing_high + h1_atr * 0.3, 2)
        # SL minimum = 3$ au-dessus de l'entrée
        sl_min     = round(entry + max(3.0, h1_atr * 1.5), 2)
        sl         = max(sl_struct, sl_min)  # le plus haut des deux

    # En mode news : TP plus agressifs (volatilité élevée garantie)
    risk = abs(entry-sl)
    if news_mode:
        nb_tp  = 6
        ratios = [1.0,1.8,2.8,4.0,5.5,7.0]
    elif high_vol:
        nb_tp  = 6
        ratios = [0.8,1.2,1.8,2.5,3.5,5.0]
    else:
        nb_tp  = 4
        ratios = [1.0,1.5,2.5,4.0]

    tps = []
    for r_ in ratios:
        v = entry+risk*r_ if direction=="BUY" else entry-risk*r_
        tps.append(round(v,2))

    return {
        "direction": direction, "entry": round(entry,2),
        "sl": sl, "tps": tps, "nb_tp": nb_tp,
        "atr": round(atr_val,2), "atr_pct": round(atr_pct,3),
        "high_vol": high_vol, "score": score, "score_max": 100,
        "score_pct": round(score/100*100),
        "rsi": round(rsi_v,1), "williams_r": round(wr,1),
        "stoch_k": round(sk.iloc[-1],1),
        "zone": zone, "zone_pct": round(zone_pct,1),
        "grabs": grabs, "bos_choch": bc,
        "news_mode": news_mode,
        "dxy_trend": dxy_trend, "dxy_rsi": dxy_rsi, "dxy_change": dxy_change,
    }

# ─── MESSAGES ─────────────────────────────────────────────────────
def build_score_bar(pct):
    filled = int(pct/10)
    return "🟩"*filled + "⬜"*(10-filled)

def msg_signal(sig: dict, news_name: str = "") -> str:
    d      = sig["direction"]
    em     = "🟢" if d=="BUY" else "🔴"
    arrow  = "📈" if d=="BUY" else "📉"
    vol    = "🔥 HAUTE — 6 TP" if sig["high_vol"] or sig["news_mode"] else "📊 NORMALE — 4 TP"
    bar    = build_score_bar(sig["score_pct"])
    news_tag = f"\n⚡ *TRADE NEWS : {news_name}*\n_Setup post-spike — volatilité maximale_\n" if sig["news_mode"] else ""
    grabs_txt = ""
    if sig["grabs"]:
        t = "haussier" if sig["grabs"][0]["type"]=="bullish_grab" else "baissier"
        grabs_txt = f"\n💧 *Liquidity Grab {t}* détecté à `{sig['grabs'][0]['level']}`"
    bc = sig["bos_choch"]
    bos_txt = ""
    if bc["choch"]: bos_txt = f"\n🔄 *CHoCH {bc['choch'].upper()}* — retournement confirmé"
    elif bc["bos"]: bos_txt = f"\n🔁 *BOS {bc['bos'].upper()}* — structure cassée"
    zone_em = "🔻" if sig["zone"]=="premium" else ("🔺" if sig["zone"]=="discount" else "⚖️")
    dxy_em  = "📉" if sig["dxy_trend"]=="down" else ("📈" if sig["dxy_trend"]=="up" else "➡️")
    dxy_txt = f"{dxy_em} DXY : `{sig['dxy_trend'].upper()}` | RSI `{sig['dxy_rsi']}` | Δ `{sig['dxy_change']}%`"

    tps_lines = ""
    for i, tp in enumerate(sig["tps"], 1):
        rr_ = round(abs(tp-sig["entry"])/abs(sig["entry"]-sig["sl"]),1)
        bar2 = "━"*min(i*2,12)
        tps_lines += f"  {'└' if i==len(sig['tps']) else '├'}─TP{i} `{tp}` — R:R {rr_}x {bar2}\n"

    return (
        f"{'═'*26}\n"
        f"{em} *GOLD SNIPER — {d}* {arrow}\n"
        f"{'═'*26}\n"
        f"{news_tag}"
        f"\n🎯 *ENTRÉE :* `{sig['entry']}`\n"
        f"🛑 *STOP LOSS :* `{sig['sl']}`\n"
        f"\n🏹 *TAKE PROFITS ({sig['nb_tp']} TP)*\n"
        f"{tps_lines}"
        f"\n{'─'*26}\n"
        f"🧠 *CONFLUENCE* {bar} `{sig['score_pct']}%`\n"
        f"  📊 RSI `{sig['rsi']}` | ⚡ StochRSI `{sig['stoch_k']}` | 📉 W%R `{sig['williams_r']}`\n"
        f"  🌡️ Volatilité : {vol}\n"
        f"  {zone_em} Zone : `{sig['zone'].upper()}` ({sig['zone_pct']}%)\n"
        f"  {dxy_txt}\n"
        f"{grabs_txt}{bos_txt}\n"
        f"\n{'─'*26}\n"
        f"⏰ `{datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC`\n"
        f"{'═'*26}\n"
        f"⚠️ _Gérez votre risque._"
    )

def msg_tp_hit(signal: Signal, tp_idx: int) -> str:
    medals = ["🥇","🥈","🥉","💎","👑","🚀"]
    medal  = medals[tp_idx] if tp_idx < len(medals) else "✅"
    tp_val = signal.tps[tp_idx]
    remaining = signal.nb_tp-(tp_idx+1)
    rr_ = round(abs(tp_val-signal.entry)/abs(signal.entry-signal.sl),1)
    status = ""
    for i, tp in enumerate(signal.tps):
        status += f"  {'✅' if i<=tp_idx else '⏳'} TP{i+1} : `{tp}`{'  — *TOUCHÉ*' if i<=tp_idx else ''}\n"
    advice = (
        f"\n🎯 *Prochain :* `{signal.tps[tp_idx+1]}` | R:R {round(abs(signal.tps[tp_idx+1]-signal.entry)/abs(signal.entry-signal.sl),1)}x\n"
        f"💡 _Déplacez le SL en breakeven !_"
        if remaining > 0 else
        f"\n🏆 *TOUS LES TP ATTEINTS — Trade parfait !*\n🙏 _C'est du pur snipe. Bien joué._"
    )
    news_tag = f"⚡ _(Trade news : {signal.news_name})_\n" if signal.is_news_trade else ""
    return (
        f"{'═'*26}\n"
        f"{medal} *TP{tp_idx+1} TOUCHÉ — GOLD* {medal}\n"
        f"{'═'*26}\n"
        f"{news_tag}"
        f"\n💰 *Prix atteint :* `{tp_val}`\n"
        f"📌 `{signal.direction}` | Entrée `{signal.entry}` | R:R `{rr_}x`\n"
        f"\n*Statut :*\n{status}"
        f"{advice}\n"
        f"{'═'*26}"
    )

def msg_sl_hit(signal: Signal) -> str:
    partial = len(signal.tp_hit) > 0
    txt = f"⚡ {len(signal.tp_hit)} TP capturés avant la clôture." if partial else "❌ Aucun TP. Ça arrive — c'est le jeu."
    return (
        f"{'═'*26}\n🛑 *STOP LOSS TOUCHÉ — GOLD*\n{'═'*26}\n\n"
        f"📌 `{signal.direction}` | Entrée `{signal.entry}` | SL `{signal.sl}`\n\n"
        f"{txt}\n\n"
        f"💪 _Un SL c'est une protection, pas une défaite.\nOn reste disciplinés. Prochain setup en approche._\n"
        f"{'═'*26}"
    )

def msg_news_warning(news: dict) -> str:
    mins = int(news["diff_min"])
    return (
        f"{'═'*26}\n"
        f"⚠️ *ANNONCE MACRO DANS {mins} MIN*\n"
        f"{'═'*26}\n"
        f"\n"
        f"{news['impact']} *{news['name']}*\n"
        f"🕐 `{news['time'].strftime('%H:%M')} UTC`\n"
        f"\n"
        f"💡 *Stratégie :*\n"
        f"  • Ne pas entrer de trade maintenant\n"
        f"  • Attendre le spike initial (1-2 min)\n"
        f"  • Le bot snipe la direction confirmée\n"
        f"  • Gros moves attendus sur XAUUSD 💰\n"
        f"\n"
        f"_Préparez-vous. Les meilleures opportunités arrivent après les news._\n"
        f"{'═'*26}"
    )

def msg_news_spike_wait(news: dict) -> str:
    return (
        f"⚡ *SPIKE EN COURS — {news['name']}*\n"
        f"⏳ _On laisse le marché digérer...\nSignal snipe dans {NEWS_SNIPE_AFTER - int(-news['diff_min'])} min._"
    )

def msg_news_snipe_ready(news: dict) -> str:
    return (
        f"🎯 *SNIPE MODE ACTIVÉ — Post {news['name']}*\n"
        f"📡 _Analyse de la direction en cours...\nSi confluence ≥ 55%, le signal part maintenant._"
    )

MOTIVATIONAL = [
    "💎 *L'or ne ment jamais.* Seuls les impatients perdent.",
    "🧘 Pas de signal = pas de trade. *Le cash est une position.*",
    "🦁 Les grands traders attendent. *La patience EST l'edge.*",
    "📖 Ne jamais risquer plus de 1-2% par trade. *La survie avant tout.*",
    "🌊 Apprenez à surfer les vagues. Pas à nager contre.",
    "🎯 *Qualité > Quantité.* 3 bons trades > 10 mauvais.",
    "🔒 *Protégez vos gains.* SL en breakeven dès TP1 touché.",
    "🧠 *L'émotion est l'ennemi n°1.* Suivez le plan, toujours.",
    "⚡ XAUUSD est une bête. *Respectez-la et elle vous respectera.*",
    "🏆 *Consistance > performance.* Les pros gagnent régulièrement.",
]
MARKET_VIBES = [
    "🌙 Session Asie active — l'or en consolidation. On surveille.",
    "☀️ Session Londres ouverte — volatilité en hausse. Soyez prêts.",
    "🇺🇸 Session New York — les gros moves se font ici. Focus.",
    "🔍 Les institutionnels laissent des traces. On les suit.",
    "📊 Marché calme = accumulation en cours. Le move arrive.",
]

def msg_bilan_jour(perf: Performance, date: str) -> str:
    wr = round(perf.wins/perf.total_signals*100) if perf.total_signals>0 else 0
    em = "🔥" if wr>=70 else ("💪" if wr>=50 else "📊")
    return (
        f"{'═'*26}\n📊 *BILAN DU JOUR — {date}*\n{'═'*26}\n\n"
        f"  📡 Signaux : `{perf.total_signals}`\n"
        f"  ✅ Wins : `{perf.wins}`  ❌ Losses : `{perf.losses}`\n"
        f"  ⚡ Partiels : `{perf.partial_wins}`\n"
        f"  🏹 TP touchés : `{perf.total_tps_hit}`\n"
        f"  📰 Trades news : `{perf.news_trades}`\n\n"
        f"  {em} *Win Rate : {wr}%*\n\n{'─'*26}\n"
        f"{'🔥 Excellente journée !' if wr>=70 else ('💪 Bonne journée !' if wr>=50 else '📈 On continue.')}\n"
        f"{'═'*26}"
    )

def msg_bilan_semaine(perf: Performance, week: str) -> str:
    wr    = round(perf.wins/perf.total_signals*100) if perf.total_signals>0 else 0
    grade = "S" if wr>=80 else ("A" if wr>=70 else ("B" if wr>=60 else ("C" if wr>=50 else "D")))
    stars = "⭐"*{"S":5,"A":4,"B":3,"C":2,"D":1}[grade]
    return (
        f"{'═'*26}\n🏆 *BILAN HEBDOMADAIRE*\nSemaine {week}\n{'═'*26}\n\n"
        f"  📡 Signaux : `{perf.total_signals}`\n"
        f"  ✅ Wins : `{perf.wins}`  ❌ Losses : `{perf.losses}`\n"
        f"  ⚡ Partiels : `{perf.partial_wins}`\n"
        f"  🏹 TP touchés : `{perf.total_tps_hit}`\n"
        f"  📰 Trades news : `{perf.news_trades}`\n\n"
        f"  📈 *Win Rate : {wr}%*\n"
        f"  🎖️ *Grade : {grade}* {stars}\n\n{'─'*26}\n"
        f"🔜 _Prêts pour la semaine prochaine. L'or récompense la patience._\n"
        f"{'═'*26}"
    )

def msg_market_open() -> str:
    h = datetime.utcnow().hour
    s = "Asie" if h<8 else ("Londres" if h<12 else "New York")
    # Affiche les prochaines news de la journée
    upcoming = get_upcoming_news(window_minutes=480)
    news_txt = ""
    if upcoming:
        news_txt = "\n\n📅 *News du jour :*\n"
        for n in upcoming:
            news_txt += f"  {n['impact']} `{n['time'].strftime('%H:%M')}` — {n['name']}\n"
    return (
        f"🔔 *SESSION {s.upper()} OUVERTE*\n{'─'*26}\n"
        f"🏅 XAUUSD sous surveillance active.\n"
        f"📡 Le bot analyse en temps réel.{news_txt}\n"
        f"⚡ _Soyez prêts — les opportunités arrivent vite._"
    )

def msg_waiting() -> str:
    return random.choice([
        "🔍 *Scan actif...* Pas de setup qualité pour l'instant.",
        "⏳ *Market Watch.* Pas de signal — la patience paie.",
        "🧘 *Surveillance continue.* L'or consolide — setup imminent possible.",
        "👁️ *On guette la liquidité...* Les institutionnels préparent leur move.",
    ])

# ─── BOT PRINCIPAL ────────────────────────────────────────────────
class GoldSniperBot:
    def __init__(self):
        self.bot = Bot(
            token=TELEGRAM_BOT_TOKEN,
            request=HTTPXRequest(
                connect_timeout=30,
                read_timeout=30,
                write_timeout=30,
                pool_timeout=30,
            )
        )
        self.active_signal: Optional[Signal] = None
        self.last_signal_time: Optional[datetime] = None
        self.daily_perf   = Performance()
        self.weekly_perf  = Performance()
        self.scheduler    = AsyncIOScheduler()
        self.scan_count   = 0
        self.news_warned  = set()   # évite d'envoyer 2x le même warning
        self.news_sniped  = set()   # évite de sniper 2x la même news

    async def send(self, text, reply_to=None) -> int:
        msg = await self.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, text=text,
            parse_mode=ParseMode.MARKDOWN, reply_to_message_id=reply_to
        )
        return msg.message_id

    async def run(self):
        log.info("🚀 GOLD SNIPER ELITE v3 démarré")
        self.scheduler.add_job(self.bilan_jour,      "cron", hour=21, minute=0)
        self.scheduler.add_job(self.bilan_semaine,   "cron", day_of_week="fri", hour=21, minute=30)
        self.scheduler.add_job(self.market_open_msg, "cron", hour=8,  minute=0)
        self.scheduler.add_job(self.market_open_msg, "cron", hour=13, minute=30)
        self.scheduler.add_job(self.send_vibe,       "cron", hour="9,13,17,20", minute=0)
        # Messages weekend
        self.scheduler.add_job(self.msg_weekend,     "cron", day_of_week="fri", hour=22, minute=0)
        self.scheduler.add_job(self.msg_lundi,       "cron", day_of_week="mon", hour=7,  minute=0)
        self.scheduler.start()
        await self.send(
            "🤖 *GOLD SNIPER ELITE v3 — EN LIGNE* 🏅\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Analyse multi-confluence 13 systèmes\n"
            "✅ Liquidité | SMC | Order Flow | DXY\n"
            "✅ TP dynamiques 4 ou 6\n"
            "✅ Module news offensif activé 📰\n\n"
            "_L'or récompense la patience. Let's go._"
        )
        while True:
            try:
                await self.tick()
            except Exception as e:
                log.error(f"Erreur : {e}")
            await asyncio.sleep(SCAN_SEC)

    async def tick(self):
        now = datetime.utcnow()
        dow = now.weekday()

        # ── Marché fermé le weekend (or ferme vendredi 22h UTC) ──
        if dow == 5:  # Samedi
            log.info("Weekend — marché fermé 💤")
            return
        if dow == 6:  # Dimanche
            log.info("Weekend — marché fermé 💤")
            return
        if dow == 4 and now.hour >= 22:  # Vendredi après 22h UTC
            log.info("Marché fermé — weekend commencé 💤")
            return
        if dow == 0 and now.hour < 1:  # Lundi avant 1h UTC
            log.info("Marché pas encore ouvert 💤")
            return

        price = fetch_price()
        self.scan_count += 1
        log.info(f"[#{self.scan_count}] {price}")
        if self.active_signal and self.active_signal.active:
            await self.check_signal(price)
            return

        # ── Vérification news ──
        status, news_info = news_status()

        if status == "warn":
            key = news_info["name"] + news_info["time"].strftime("%H%M")
            if key not in self.news_warned:
                self.news_warned.add(key)
                await self.send(msg_news_warning(news_info))
            return  # on n'analyse pas avant l'annonce

        if status == "spike":
            await self.send(msg_news_spike_wait(news_info))
            return  # on attend que le spike passe

        if status == "snipe":
            key = news_info["name"] + news_info["time"].strftime("%H%M")
            if key not in self.news_sniped:
                self.news_sniped.add(key)
                await self.send(msg_news_snipe_ready(news_info))
                await self._try_signal(price, news_mode=True, news_name=news_info["name"])
            return

        # ── Cooldown normal ──
        if self.last_signal_time:
            elapsed = (datetime.utcnow()-self.last_signal_time).total_seconds()/60
            if elapsed < COOLDOWN_MIN:
                if self.scan_count % 5 == 0:
                    await self.send(msg_waiting())
                return

        # ── Analyse normale ──
        await self._try_signal(price, news_mode=False)

    async def _try_signal(self, price, news_mode=False, news_name=""):
        df_m15 = fetch_ohlcv(TF_MAIN,  150)
        df_h1  = fetch_ohlcv(TF_HIGH,  100)
        df_h4  = fetch_ohlcv(TF_TREND, 80)
        dxy    = fetch_dxy()
        log.info(f"DXY → trend:{dxy['trend']} rsi:{dxy['rsi']} Δ:{dxy['change']}%")
        result = analyse_full(df_m15, df_h1, df_h4, news_mode=news_mode, dxy=dxy)

        if result:
            log.info(f"🎯 {result['direction']} | {result['score_pct']}% {'[NEWS]' if news_mode else ''}")
            signal = Signal(
                direction=result["direction"], entry=result["entry"],
                sl=result["sl"], tps=result["tps"], nb_tp=result["nb_tp"],
                atr=result["atr"], score=result["score"], score_max=100,
                confluence=result, timestamp=datetime.utcnow(),
                is_news_trade=news_mode, news_name=news_name,
            )
            mid = await self.send(msg_signal(result, news_name))
            signal.message_id = mid
            self.active_signal = signal
            self.last_signal_time = datetime.utcnow()
            self.daily_perf.total_signals  += 1
            self.weekly_perf.total_signals += 1
            if news_mode:
                self.daily_perf.news_trades  += 1
                self.weekly_perf.news_trades += 1
        else:
            if not news_mode and self.scan_count % 10 == 0:
                await self.send(msg_waiting())

    async def check_signal(self, price):
        sig = self.active_signal
        sl_hit = (sig.direction=="BUY" and price<=sig.sl) or \
                 (sig.direction=="SELL" and price>=sig.sl)
        if sl_hit:
            log.info(f"🛑 SL {price}")
            await self.send(msg_sl_hit(sig), reply_to=sig.message_id)
            sig.active = False
            if len(sig.tp_hit)==0:
                self.daily_perf.losses  += 1; self.weekly_perf.losses += 1
            else:
                self.daily_perf.partial_wins  += 1; self.weekly_perf.partial_wins += 1
            return
        for i, tp in enumerate(sig.tps):
            if i in sig.tp_hit: continue
            hit = (sig.direction=="BUY" and price>=tp) or (sig.direction=="SELL" and price<=tp)
            if hit:
                log.info(f"✅ TP{i+1} {price}")
                sig.tp_hit.append(i)
                self.daily_perf.total_tps_hit  += 1; self.weekly_perf.total_tps_hit += 1
                await self.send(msg_tp_hit(sig, i), reply_to=sig.message_id)
                if len(sig.tp_hit)==sig.nb_tp:
                    sig.active=False; sig.closed_in_profit=True
                    self.daily_perf.wins  += 1; self.weekly_perf.wins += 1
                elif len(sig.tp_hit)==1:
                    self.daily_perf.wins  += 1; self.weekly_perf.wins += 1
                break

    async def bilan_jour(self):
        await self.send(msg_bilan_jour(self.daily_perf, datetime.utcnow().strftime("%d/%m/%Y")))
        self.daily_perf = Performance()

    async def bilan_semaine(self):
        w = (datetime.utcnow()-timedelta(days=4)).strftime("%d/%m") + " au " + datetime.utcnow().strftime("%d/%m/%Y")
        await self.send(msg_bilan_semaine(self.weekly_perf, w))
        self.weekly_perf = Performance()

    async def market_open_msg(self): await self.send(msg_market_open())
    async def send_vibe(self):       await self.send(random.choice(MOTIVATIONAL+MARKET_VIBES))

    async def msg_weekend(self):
        await self.send(
            "🌙 *MARCHÉ FERMÉ — BON WEEKEND* 🏖️\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Le marché de l'or ferme ses portes.\n"
            "Le bot se met en veille jusqu'à lundi.\n\n"
            "📊 *Profitez du weekend pour :*\n"
            "  • Analyser vos trades de la semaine\n"
            "  • Revoir votre gestion du risque\n"
            "  • Vous reposer — le mindset c'est capital 🧠\n\n"
            "_L'or sera là lundi. Soyez prêts._ 💎"
        )

    async def msg_lundi(self):
        await self.send(
            "⚡ *MARCHÉ OUVERT — C'EST LUNDI !* 🔥\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Le bot reprend la surveillance XAUUSD.\n"
            "Analyse multi-confluence active. 🎯\n\n"
            "💡 _Nouvelle semaine = nouvelles opportunités.\nRestez disciplinés. L'or récompense la patience._"
        )

# ─── LANCEMENT ────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(GoldSniperBot().run())
