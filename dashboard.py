"""
╔══════════════════════════════════════════════════════════════╗
║   NIFTY OPTIONS SIGNAL DASHBOARD                             ║
║   Free data from NSE India — No broker API needed            ║
║                                                              ║
║   Run:  streamlit run dashboard.py                           ║
╚══════════════════════════════════════════════════════════════╝
"""

# ══════════════════════════════════════════════════════════════
#  IMPORTS
# ══════════════════════════════════════════════════════════════
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import requests
import time
import datetime
import math
from dataclasses import dataclass, field
from typing import Optional

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    SYMBOL, NIFTY_STEP, NUM_EXPIRIES, STRIKES_AROUND_ATM,
    REFRESH_SECONDS,
    PCR_CALL_BUY, PCR_PUT_BUY,
    IV_RANK_BUY_MAX, IV_RANK_EXIT_MIN,
    DELTA_MIN, DELTA_MAX,
    OI_BUILDUP_PCT, OI_UNWIND_PCT,
    TARGET_PROFIT_PCT, STOP_LOSS_PCT,
    MIN_SIGNAL_SCORE, RISK_FREE_RATE,
)


# ══════════════════════════════════════════════════════════════
#  PAGE CONFIG  (must be first Streamlit call)
# ══════════════════════════════════════════════════════════════
st.set_page_config(
    page_title=f"{SYMBOL} Options Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Dark theme CSS ─────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stAppViewContainer"] { background:#0e1117; }
[data-testid="stSidebar"]          { background:#14161f; }
.kpi-card {
    background:#1a1d2e; border-radius:12px; padding:18px 22px;
    border:1px solid #2a2d42; text-align:center; margin-bottom:8px;
}
.kpi-val  { font-size:1.6rem; font-weight:700; margin:4px 0; }
.kpi-lbl  { font-size:0.78rem; color:#888; text-transform:uppercase; letter-spacing:1px; }
.kpi-sub  { font-size:0.85rem; margin-top:2px; }
.sig-call { background:#071a0f; border:2px solid #00c853; border-radius:10px; padding:14px 16px; margin-bottom:10px; }
.sig-put  { background:#1a0707; border:2px solid #ff1744; border-radius:10px; padding:14px 16px; margin-bottom:10px; }
.sig-exit { background:#1a1500; border:2px solid #ffd600; border-radius:10px; padding:14px 16px; margin-bottom:10px; }
.sig-watch{ background:#07101a; border:2px solid #2196f3; border-radius:10px; padding:14px 16px; margin-bottom:10px; }
.badge    { display:inline-block; padding:2px 10px; border-radius:12px; font-size:.82rem; font-weight:700; }
.badge-call  { background:#00c853; color:#000; }
.badge-put   { background:#ff1744; color:#fff; }
.badge-exit  { background:#ffd600; color:#000; }
.badge-watch { background:#2196f3; color:#fff; }
.score-bar { height:8px; border-radius:4px; background:#333; margin:6px 0; }
.atm-row   { background:#1a1a40 !important; }
hr { border-color:#2a2d42; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════
#  SESSION STATE INIT
# ══════════════════════════════════════════════════════════════
def _ss(key, default):
    if key not in st.session_state:
        st.session_state[key] = default

_ss("snapshot",       None)
_ss("last_refresh",   None)
_ss("signals",        [])
_ss("exit_signals",   [])
_ss("watch_signals",  [])
_ss("open_positions", [])
_ss("alert_log",      [])
_ss("iv_history",     [])       # list of daily ATM IVs for IV Rank
_ss("last_heartbeat", None)
_ss("nse_session",    None)
_ss("nse_session_ts", 0)
_ss("error_msg",      "")


# ══════════════════════════════════════════════════════════════
#  NSE DATA ENGINE  (free — no API key needed)
# ══════════════════════════════════════════════════════════════

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Referer":         "https://www.nseindia.com/",
}


def _get_nse_session() -> requests.Session:
    """
    NSE requires cookies set by the homepage before the API responds.
    We cache the session for 20 minutes to avoid hammering NSE.
    """
    age = time.time() - st.session_state.nse_session_ts
    if st.session_state.nse_session and age < 1200:
        return st.session_state.nse_session

    sess = requests.Session()
    sess.headers.update(NSE_HEADERS)
    # Warm up cookies
    for url in [
        "https://www.nseindia.com",
        "https://www.nseindia.com/market-data/live-equity-market?symbol=NIFTY",
    ]:
        try:
            sess.get(url, timeout=10)
            time.sleep(0.5)
        except Exception:
            pass

    st.session_state.nse_session    = sess
    st.session_state.nse_session_ts = time.time()
    return sess


def fetch_nse_option_chain(symbol: str = "NIFTY") -> dict:
    """
    Fetch raw option chain JSON from NSE.
    Returns the parsed JSON dict or raises on failure.
    """
    sess = _get_nse_session()
    url  = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
    r    = sess.get(url, timeout=12)
    r.raise_for_status()
    return r.json()


def parse_option_chain(raw: dict, num_expiries: int = NUM_EXPIRIES) -> pd.DataFrame:
    """
    Parse NSE option chain JSON into a tidy DataFrame.

    NSE data already includes: strikePrice, expiryDate, OI, changeInOI,
    volume, IV, LTP, change%, underlyingValue.
    Greeks are calculated here via Black-Scholes.
    """
    records    = raw.get("records", {})
    data       = records.get("data", [])
    spot       = float(records.get("underlyingValue", 0))
    all_expiries = records.get("expiryDates", [])

    # Take only the first N expiries
    target_expiries = set(all_expiries[:num_expiries])

    rows = []
    for item in data:
        exp_str = item.get("expiryDate", "")
        if exp_str not in target_expiries:
            continue

        try:
            exp_date = datetime.datetime.strptime(exp_str, "%d-%b-%Y").date()
        except ValueError:
            continue

        dte = max((exp_date - datetime.date.today()).days, 1)

        for opt_type in ("CE", "PE"):
            opt = item.get(opt_type)
            if not opt:
                continue

            strike   = float(item.get("strikePrice", opt.get("strikePrice", 0)))
            ltp      = float(opt.get("lastPrice", 0))
            oi       = int(opt.get("openInterest", 0))
            oi_chg   = int(opt.get("changeinOpenInterest", 0))
            volume   = int(opt.get("totalTradedVolume", 0))
            iv_nse   = float(opt.get("impliedVolatility", 0))   # NSE provides IV %

            # OI change %
            oi_chg_pct = (oi_chg / max(oi - oi_chg, 1)) * 100 if oi != 0 else 0

            # OI flag
            if oi_chg_pct >= OI_BUILDUP_PCT:
                oi_flag = "buildup"
            elif oi_chg_pct <= OI_UNWIND_PCT:
                oi_flag = "unwinding"
            else:
                oi_flag = "neutral"

            # Greeks via Black-Scholes (using NSE IV)
            greeks = _black_scholes_greeks(spot, strike, dte, iv_nse, opt_type)

            rows.append({
                "strike":       strike,
                "type":         opt_type,
                "ltp":          ltp,
                "oi":           oi,
                "oi_change":    oi_chg,
                "oi_change_pct":round(oi_chg_pct, 1),
                "volume":       volume,
                "iv":           iv_nse,
                "delta":        greeks["delta"],
                "gamma":        greeks["gamma"],
                "theta":        greeks["theta"],
                "vega":         greeks["vega"],
                "oi_flag":      oi_flag,
                "expiry":       exp_date,
                "expiry_str":   exp_str,
                "dte":          dte,
                "spot":         spot,
                "tradingsymbol": _make_symbol(symbol, exp_date, int(strike), opt_type),
            })

    df = pd.DataFrame(rows)
    return df, spot, all_expiries[:num_expiries]


def _make_symbol(sym: str, expiry: datetime.date, strike: int, opt_type: str) -> str:
    mon = expiry.strftime("%b").upper()
    yr  = expiry.strftime("%y")
    return f"{sym}{yr}{mon}{strike}{opt_type}"


# ══════════════════════════════════════════════════════════════
#  BLACK-SCHOLES GREEKS
# ══════════════════════════════════════════════════════════════

def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def _black_scholes_greeks(
    S: float, K: float, dte: int, iv_pct: float, opt_type: str
) -> dict:
    """
    Pure-Python Black-Scholes Greeks.
    S = spot, K = strike, dte = days to expiry,
    iv_pct = implied volatility in percent (e.g. 15.0 means 15%)
    """
    empty = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    if iv_pct <= 0 or S <= 0 or K <= 0 or dte <= 0:
        return empty

    T   = dte / 365.0
    r   = RISK_FREE_RATE
    sig = iv_pct / 100.0

    try:
        d1 = (math.log(S / K) + (r + 0.5 * sig ** 2) * T) / (sig * math.sqrt(T))
        d2 = d1 - sig * math.sqrt(T)

        gamma = _norm_pdf(d1) / (S * sig * math.sqrt(T))
        vega  = S * _norm_pdf(d1) * math.sqrt(T) / 100  # per 1% IV change

        if opt_type == "CE":
            delta = _norm_cdf(d1)
            theta = (-(S * _norm_pdf(d1) * sig) / (2 * math.sqrt(T))
                     - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365
        else:
            delta = _norm_cdf(d1) - 1
            theta = (-(S * _norm_pdf(d1) * sig) / (2 * math.sqrt(T))
                     + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365

        return {
            "delta": round(delta, 4),
            "gamma": round(gamma, 6),
            "theta": round(theta, 2),
            "vega":  round(vega, 4),
        }
    except (ValueError, ZeroDivisionError):
        return empty


# ══════════════════════════════════════════════════════════════
#  DERIVED METRICS
# ══════════════════════════════════════════════════════════════

def compute_pcr(df: pd.DataFrame) -> float:
    """Put-Call Ratio by Open Interest."""
    if df.empty:
        return 1.0
    p = df[df["type"] == "PE"]["oi"].sum()
    c = df[df["type"] == "CE"]["oi"].sum()
    return round(p / c, 3) if c > 0 else 1.0


def compute_max_pain(df: pd.DataFrame) -> int:
    """Strike price where total option buyer loss is maximum."""
    if df.empty:
        return 0
    strikes = sorted(df["strike"].unique())
    calls   = df[df["type"] == "CE"].set_index("strike")["oi"].to_dict()
    puts    = df[df["type"] == "PE"].set_index("strike")["oi"].to_dict()

    best_s, best_pain = strikes[0], float("inf")
    for s in strikes:
        pain = (
            sum(max(0, s - k) * calls.get(k, 0) for k in strikes) +
            sum(max(0, k - s) * puts.get(k, 0)  for k in strikes)
        )
        if pain < best_pain:
            best_pain, best_s = pain, s
    return int(best_s)


def compute_iv_rank(current_iv: float, history: list) -> float:
    """IV Rank: where current IV sits in its 52-week range (0–100)."""
    if len(history) < 5:
        return -1.0
    lo, hi = min(history), max(history)
    if hi == lo:
        return 50.0
    return round((current_iv - lo) / (hi - lo) * 100, 1)


def get_atm_iv(df: pd.DataFrame, spot: float) -> float:
    """Average IV of ATM call + put."""
    atm = round(spot / NIFTY_STEP) * NIFTY_STEP
    sub = df[df["strike"] == atm]["iv"]
    vals = [v for v in sub if v > 0]
    return round(np.mean(vals), 2) if vals else 0.0


# ══════════════════════════════════════════════════════════════
#  SIGNAL ENGINE
# ══════════════════════════════════════════════════════════════

@dataclass
class Signal:
    kind:          str            # BUY_CALL | BUY_PUT | EXIT | WATCH
    tradingsymbol: str
    strike:        int
    expiry:        datetime.date
    ltp:           float
    score:         int
    reasons:       list = field(default_factory=list)
    delta:         float = 0.0
    gamma:         float = 0.0
    theta:         float = 0.0
    vega:          float = 0.0
    iv:            float = 0.0
    oi:            int   = 0
    oi_flag:       str   = "neutral"
    oi_change_pct: float = 0.0
    pcr:           float = 1.0
    iv_rank:       float = -1.0
    max_pain:      int   = 0
    spot:          float = 0.0
    ts:            str   = field(default_factory=lambda: datetime.datetime.now().strftime("%H:%M:%S"))


def _score_direction(row: pd.Series, side: str, pcr: float, iv_rank: float, max_pain: int) -> tuple:
    """Score a single option row for BUY_CALL or BUY_PUT. Returns (score, reasons)."""
    score, reasons = 0, []
    spot   = row["spot"]
    delta  = abs(row["delta"])
    gamma  = row["gamma"]
    volume = row["volume"]
    oi_flag = row["oi_flag"]
    oi_pct  = row["oi_change_pct"]
    iv      = row["iv"]

    is_call = (side == "BUY_CALL")

    # ── Factor 1: PCR sentiment (2 pts) ──────────────────────
    if is_call and pcr <= PCR_CALL_BUY:
        score += 2; reasons.append(f"PCR {pcr:.2f} → bullish (buy calls)")
    elif is_call and pcr <= 0.88:
        score += 1; reasons.append(f"PCR {pcr:.2f} → mildly bullish")
    elif not is_call and pcr >= PCR_PUT_BUY:
        score += 2; reasons.append(f"PCR {pcr:.2f} → bearish (buy puts)")
    elif not is_call and pcr >= 1.12:
        score += 1; reasons.append(f"PCR {pcr:.2f} → mildly bearish")

    # ── Factor 2: IV Rank (2 pts) ────────────────────────────
    if 0 <= iv_rank <= 20:
        score += 2; reasons.append(f"IV Rank {iv_rank:.0f}% — very cheap options ✅")
    elif 20 < iv_rank <= IV_RANK_BUY_MAX:
        score += 1; reasons.append(f"IV Rank {iv_rank:.0f}% — cheap options")
    elif iv_rank < 0:
        pass   # history not built up yet, don't penalise

    # ── Factor 3: OI buildup (2 pts) ─────────────────────────
    if oi_flag == "buildup":
        score += 2; reasons.append(f"OI buildup +{oi_pct:.0f}% — fresh positions")
    elif oi_pct >= 7:
        score += 1; reasons.append(f"OI growing +{oi_pct:.0f}%")

    # ── Factor 4: Delta sweet spot (1 pt) ────────────────────
    if DELTA_MIN <= delta <= DELTA_MAX:
        score += 1; reasons.append(f"Δ {delta:.2f} in sweet zone (0.25–0.60)")
    elif delta < DELTA_MIN:
        score -= 1   # penalise strikes too far OTM

    # ── Factor 5: Volume confirmation (1 pt) ─────────────────
    if volume >= 1000:
        score += 1; reasons.append(f"Volume {volume:,} — active interest")

    # ── Factor 6: Max Pain alignment (1 pt) ──────────────────
    if max_pain > 0:
        if is_call and spot > max_pain:
            score += 1; reasons.append(f"Spot {spot:.0f} > MaxPain {max_pain} — call friendly")
        elif not is_call and spot < max_pain:
            score += 1; reasons.append(f"Spot {spot:.0f} < MaxPain {max_pain} — put friendly")

    # ── Factor 7: Gamma momentum (1 pt) ─────────────────────
    if gamma >= 0.0005:
        score += 1; reasons.append(f"Γ {gamma:.5f} — responsive strike (near ATM)")

    return max(score, 0), reasons


def generate_signals(df: pd.DataFrame, pcr: float, iv_rank: float, max_pain: int) -> tuple:
    """Returns (entry_signals, watch_signals) sorted by score desc."""
    entry, watch = [], []

    call_eligible = (pcr <= 1.0)    # scan calls when market not extremely bearish
    put_eligible  = (pcr >= 1.0)    # scan puts when market not extremely bullish

    for _, row in df.iterrows():
        otype = row["type"]
        side  = "BUY_CALL" if otype == "CE" else "BUY_PUT"

        if side == "BUY_CALL" and not call_eligible:
            continue
        if side == "BUY_PUT" and not put_eligible:
            continue

        sc, reasons = _score_direction(row, side, pcr, iv_rank, max_pain)
        sig = Signal(
            kind=side, tradingsymbol=row["tradingsymbol"],
            strike=int(row["strike"]), expiry=row["expiry"],
            ltp=row["ltp"], score=sc, reasons=reasons,
            delta=row["delta"], gamma=row["gamma"],
            theta=row["theta"], vega=row["vega"],
            iv=row["iv"], oi=row["oi"],
            oi_flag=row["oi_flag"], oi_change_pct=row["oi_change_pct"],
            pcr=pcr, iv_rank=iv_rank, max_pain=max_pain, spot=row["spot"],
        )
        if sc >= MIN_SIGNAL_SCORE:
            entry.append(sig)
        elif sc >= 4:
            watch.append(sig)

    entry.sort(key=lambda s: (-s.score, s.expiry))
    watch.sort(key=lambda s: (-s.score, s.expiry))
    return entry, watch[:8]


def generate_exit_signals(positions: list, df: pd.DataFrame, pcr: float, iv_rank: float) -> list:
    """Check each open position for exit conditions."""
    exits = []
    for pos in positions:
        sym     = pos.get("tradingsymbol", "")
        bp      = float(pos.get("buy_price", 0))
        ptype   = pos.get("type", "CE")
        expiry  = pos.get("expiry")

        match = df[(df["tradingsymbol"] == sym) | (df["strike"] == pos.get("strike", 0))]
        if ptype:
            match = match[match["type"] == ptype]
        if match.empty:
            continue

        row   = match.iloc[0]
        ltp   = row["ltp"]
        iv    = row["iv"]
        delta = abs(row["delta"])

        exit_reasons, sc = [], 0

        # P&L checks
        if bp > 0:
            pnl = (ltp - bp) / bp * 100
            if pnl >= TARGET_PROFIT_PCT:
                exit_reasons.append(f"🎯 Profit target hit (+{pnl:.0f}%)")
                sc += 5
            elif pnl <= -STOP_LOSS_PCT:
                exit_reasons.append(f"🛑 Stop-loss hit ({pnl:.0f}%)")
                sc += 5

        # IV became expensive
        if iv_rank >= IV_RANK_EXIT_MIN:
            exit_reasons.append(f"IV Rank {iv_rank:.0f}% — options expensive, book now")
            sc += 3

        # OI unwinding
        if row["oi_flag"] == "unwinding":
            exit_reasons.append(f"OI unwinding ({row['oi_change_pct']:.0f}%) — smart money exiting")
            sc += 3

        # Deep ITM — capture profits
        if delta > 0.75:
            exit_reasons.append(f"|Δ| {delta:.2f} — deep ITM, lock in gains")
            sc += 2

        # PCR reversal vs your position
        if ptype == "CE" and pcr >= PCR_PUT_BUY:
            exit_reasons.append(f"PCR {pcr:.2f} flipped bearish — exit call")
            sc += 2
        elif ptype == "PE" and pcr <= PCR_CALL_BUY:
            exit_reasons.append(f"PCR {pcr:.2f} flipped bullish — exit put")
            sc += 2

        # Expiry tomorrow
        if isinstance(expiry, datetime.date):
            if (expiry - datetime.date.today()).days <= 1:
                exit_reasons.append("⏰ Expiry tomorrow — exit to avoid theta crush")
                sc += 4

        if sc >= 4:
            exits.append(Signal(
                kind="EXIT", tradingsymbol=sym,
                strike=int(row["strike"]), expiry=expiry or datetime.date.today(),
                ltp=ltp, score=sc, reasons=exit_reasons,
                delta=row["delta"], gamma=row["gamma"],
                theta=row["theta"], vega=row["vega"],
                iv=iv, oi=row["oi"], oi_flag=row["oi_flag"],
                oi_change_pct=row["oi_change_pct"],
                pcr=pcr, iv_rank=iv_rank, spot=row["spot"],
            ))

    exits.sort(key=lambda s: -s.score)
    return exits


# ══════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════

_SENT: set = set()

def _tg(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"
        }, timeout=6)
        return r.ok
    except Exception:
        return False


def tg_buy_alert(sig: Signal) -> bool:
    key = (sig.tradingsymbol, datetime.datetime.now().strftime("%Y%m%d%H"))
    if key in _SENT: return False
    _SENT.add(key)

    emoji = "🟢🚀" if sig.kind == "BUY_CALL" else "🔴📉"
    side  = "CALL" if sig.kind == "BUY_CALL" else "PUT"
    ivr   = f"{sig.iv_rank:.0f}%" if sig.iv_rank >= 0 else "building..."
    reasons = "\n".join(f"  ✅ {r}" for r in sig.reasons)

    msg = (
        f"{emoji} <b>NIFTY {side} SIGNAL  [Score {sig.score}/10]</b>\n\n"
        f"📌 Symbol  : <code>{sig.tradingsymbol}</code>\n"
        f"💰 LTP     : ₹{sig.ltp:.2f}\n"
        f"🎯 Strike  : {sig.strike}  |  Expiry: {sig.expiry}\n"
        f"📊 Spot    : {sig.spot:.0f}\n\n"
        f"<b>Greeks</b>\n"
        f"  Δ {sig.delta:.4f}  Γ {sig.gamma:.5f}\n"
        f"  Θ {sig.theta:.2f}/day  ν {sig.vega:.4f}\n"
        f"  IV {sig.iv:.1f}%  |  IV Rank {ivr}\n\n"
        f"<b>Market</b>\n"
        f"  PCR {sig.pcr:.3f}  |  Max Pain {sig.max_pain}\n"
        f"  OI {sig.oi:,}  ({sig.oi_flag}  {sig.oi_change_pct:+.0f}%)\n\n"
        f"<b>Why?</b>\n{reasons}\n\n"
        f"⏰ {datetime.datetime.now().strftime('%d %b %Y  %H:%M')}"
    )
    return _tg(msg)


def tg_exit_alert(sig: Signal, buy_price: float = 0) -> bool:
    key = ("EXIT", sig.tradingsymbol, datetime.datetime.now().strftime("%Y%m%d%H"))
    if key in _SENT: return False
    _SENT.add(key)

    pnl_pct = ((sig.ltp - buy_price) / buy_price * 100) if buy_price > 0 else 0
    reasons = "\n".join(f"  ⚠️ {r}" for r in sig.reasons)

    msg = (
        f"🟡 <b>EXIT SIGNAL — {sig.tradingsymbol}</b>\n\n"
        f"💰 Current ₹{sig.ltp:.2f}  |  Bought ₹{buy_price:.2f}\n"
        f"{'📈' if pnl_pct >= 0 else '📉'} P&L  : {pnl_pct:+.1f}%\n\n"
        f"<b>Reasons</b>\n{reasons}\n\n"
        f"⏰ {datetime.datetime.now().strftime('%d %b %Y  %H:%M')}"
    )
    return _tg(msg)


def tg_heartbeat(spot: float, pcr: float, iv_rank: float, max_pain: int):
    bias = "🐂 BULLISH" if pcr < 0.85 else ("🐻 BEARISH" if pcr > 1.20 else "⚖️ NEUTRAL")
    ivs  = f"{iv_rank:.0f}% — CHEAP ✅" if 0 <= iv_rank <= 40 else (
           f"{iv_rank:.0f}% — EXPENSIVE ⚠️" if iv_rank > 60 else
           f"{iv_rank:.0f}% — Moderate")
    _tg(
        f"📊 <b>NIFTY Dashboard — Hourly</b>\n\n"
        f"  Spot     : {spot:,.0f}\n"
        f"  PCR      : {pcr:.3f}  →  {bias}\n"
        f"  IV Rank  : {ivs}\n"
        f"  Max Pain : {max_pain:,}\n"
        f"⏰ {datetime.datetime.now().strftime('%H:%M')}"
    )


# ══════════════════════════════════════════════════════════════
#  MASTER REFRESH
# ══════════════════════════════════════════════════════════════

def refresh(force: bool = False):
    now = datetime.datetime.now()

    if not force and st.session_state.last_refresh:
        if (now - st.session_state.last_refresh).seconds < REFRESH_SECONDS:
            return

    ph = st.empty()
    ph.info("⟳  Fetching live data from NSE India...")

    try:
        raw           = fetch_nse_option_chain(SYMBOL)
        chain_df, spot, expiries = parse_option_chain(raw, NUM_EXPIRIES)

        if chain_df.empty:
            st.session_state.error_msg = "NSE returned empty data. Market may be closed."
            ph.empty(); return

        pcr      = compute_pcr(chain_df)
        max_pain = compute_max_pain(chain_df)
        atm_iv   = get_atm_iv(chain_df, spot)

        # IV history for IV Rank
        if atm_iv > 0:
            st.session_state.iv_history.append(atm_iv)
            st.session_state.iv_history = st.session_state.iv_history[-252:]

        iv_rank = compute_iv_rank(atm_iv, st.session_state.iv_history)

        # Signals
        entry, watch = generate_signals(chain_df, pcr, iv_rank, max_pain)
        exits        = generate_exit_signals(
            st.session_state.open_positions, chain_df, pcr, iv_rank
        )

        st.session_state.snapshot = {
            "chain":    chain_df, "spot": spot,
            "expiries": expiries, "pcr": pcr,
            "max_pain": max_pain, "atm_iv": atm_iv,
            "iv_rank":  iv_rank,  "ts": now,
        }
        st.session_state.signals       = entry
        st.session_state.exit_signals  = exits
        st.session_state.watch_signals = watch
        st.session_state.last_refresh  = now
        st.session_state.error_msg     = ""

        # Telegram alerts
        for sig in entry[:3]:
            if tg_buy_alert(sig):
                st.session_state.alert_log.append({
                    "Time": now.strftime("%H:%M:%S"),
                    "Type": sig.kind, "Symbol": sig.tradingsymbol,
                    "LTP": sig.ltp, "Score": f"{sig.score}/10",
                })
        for sig in exits:
            pos = next((p for p in st.session_state.open_positions
                        if p.get("tradingsymbol") == sig.tradingsymbol), {})
            if tg_exit_alert(sig, pos.get("buy_price", 0)):
                st.session_state.alert_log.append({
                    "Time": now.strftime("%H:%M:%S"),
                    "Type": "EXIT", "Symbol": sig.tradingsymbol,
                    "LTP": sig.ltp, "Score": f"{sig.score}/10",
                })

        # Hourly heartbeat
        lhb = st.session_state.last_heartbeat
        if lhb is None or (now - lhb).seconds >= 3600:
            tg_heartbeat(spot, pcr, iv_rank, max_pain)
            st.session_state.last_heartbeat = now

    except Exception as e:
        st.session_state.error_msg = f"Data error: {e}. Retrying next refresh."

    ph.empty()


# ══════════════════════════════════════════════════════════════
#  SIDEBAR
# ══════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("## ⚙️ Dashboard")
    st.caption("Nifty Options — NSE Free Data")

    if st.button("🔄  Refresh Now", use_container_width=True, type="primary"):
        refresh(force=True)
        st.rerun()

    lr = st.session_state.last_refresh
    st.caption(f"Last update: {lr.strftime('%H:%M:%S') if lr else 'Never'} "
               f"| auto ⟳ {REFRESH_SECONDS}s")

    st.divider()
    st.markdown("### 📂 My Positions")
    st.caption("Add to get exit alerts")

    with st.form("pos_form", clear_on_submit=True):
        ps  = st.text_input("Symbol (e.g. NIFTY25APR22350CE)")
        c1, c2 = st.columns(2)
        pp  = c1.number_input("Buy Price ₹", min_value=1.0, value=100.0, step=0.5)
        pt  = c2.selectbox("Type", ["CE", "PE"])
        pst = st.number_input("Strike", value=22350, step=50)
        if st.form_submit_button("➕ Add"):
            snap = st.session_state.snapshot
            exp  = (datetime.date.fromisoformat(snap["expiries"][0].split(" ")[0])
                    if snap and snap.get("expiries") else datetime.date.today())
            st.session_state.open_positions.append({
                "tradingsymbol": ps, "buy_price": pp,
                "type": pt, "strike": pst, "expiry": exp,
            })
            st.success(f"Added {ps}")

    for i, pos in enumerate(st.session_state.open_positions):
        cc1, cc2 = st.columns([3, 1])
        cc1.markdown(f"`{pos['tradingsymbol']}` @ ₹{pos['buy_price']:.0f}")
        if cc2.button("✕", key=f"del_{i}"):
            st.session_state.open_positions.pop(i)
            st.rerun()

    st.divider()
    st.markdown("### 🎯 Signal Settings")
    st.caption(f"Min score to alert: **{MIN_SIGNAL_SCORE}/10**")
    st.caption(f"PCR Call threshold: **< {PCR_CALL_BUY}**")
    st.caption(f"PCR Put threshold:  **> {PCR_PUT_BUY}**")
    st.caption(f"IV Rank buy zone:   **< {IV_RANK_BUY_MAX}%**")
    st.caption("Edit these in config.py")

    tg_ok = bool(TELEGRAM_BOT_TOKEN and not TELEGRAM_BOT_TOKEN.startswith("your"))
    st.divider()
    st.markdown(f"📡 Telegram: {'✅ Connected' if tg_ok else '⚠️ Not configured'}")
    if not tg_ok:
        st.caption("Add bot token + chat_id in config.py")


# ══════════════════════════════════════════════════════════════
#  INITIAL DATA LOAD
# ══════════════════════════════════════════════════════════════
if st.session_state.snapshot is None:
    refresh(force=True)

snap = st.session_state.snapshot
if snap is None:
    st.error(st.session_state.error_msg or "No data. Click Refresh.")
    st.stop()


# ══════════════════════════════════════════════════════════════
#  MAIN HEADER
# ══════════════════════════════════════════════════════════════
spot      = snap["spot"]
pcr       = snap["pcr"]
max_pain  = snap["max_pain"]
atm_iv    = snap["atm_iv"]
iv_rank   = snap["iv_rank"]
chain_df  = snap["chain"]
atm       = int(round(spot / NIFTY_STEP) * NIFTY_STEP)
mp_gap    = spot - max_pain

st.markdown(f"# 📊 {SYMBOL} Options Signal Dashboard")
st.caption(f"Source: NSE India (free)  •  Updated: {snap['ts'].strftime('%d %b %Y  %H:%M:%S')}")

if st.session_state.error_msg:
    st.warning(st.session_state.error_msg)

# ── KPI Row ────────────────────────────────────────────────────
k1, k2, k3, k4, k5, k6 = st.columns(6)

if pcr < PCR_CALL_BUY:        pcr_col, pcr_txt = "#00c853", "BULLISH 🐂"
elif pcr > PCR_PUT_BUY:       pcr_col, pcr_txt = "#ff1744", "BEARISH 🐻"
else:                          pcr_col, pcr_txt = "#ffd600", "NEUTRAL ⚖️"

if iv_rank < 0:                ivr_col, ivr_txt = "#888",    "UNKNOWN"
elif iv_rank <= 30:            ivr_col, ivr_txt = "#00c853", "CHEAP ✅"
elif iv_rank <= 60:            ivr_col, ivr_txt = "#ffd600", "MODERATE"
else:                          ivr_col, ivr_txt = "#ff1744", "EXPENSIVE ⚠️"

mp_arrow = "↑" if mp_gap > 0 else "↓"
mp_bias  = "call zone" if mp_gap > 0 else "put zone"

k1.markdown(f'<div class="kpi-card"><div class="kpi-lbl">Nifty Spot</div><div class="kpi-val">{spot:,.2f}</div><div class="kpi-sub">ATM: {atm}</div></div>', unsafe_allow_html=True)
k2.markdown(f'<div class="kpi-card"><div class="kpi-lbl">PCR</div><div class="kpi-val" style="color:{pcr_col}">{pcr:.3f}</div><div class="kpi-sub" style="color:{pcr_col}">{pcr_txt}</div></div>', unsafe_allow_html=True)
k3.markdown(f'<div class="kpi-card"><div class="kpi-lbl">IV Rank</div><div class="kpi-val" style="color:{ivr_col}">{iv_rank:.0f}%' if iv_rank >= 0 else f'<div class="kpi-card"><div class="kpi-lbl">IV Rank</div><div class="kpi-val" style="color:{ivr_col}">—', unsafe_allow_html=True)
k3.markdown(f'<div class="kpi-card"><div class="kpi-lbl">IV Rank</div><div class="kpi-val" style="color:{ivr_col}">{"—" if iv_rank < 0 else f"{iv_rank:.0f}%"}</div><div class="kpi-sub" style="color:{ivr_col}">{ivr_txt}</div></div>', unsafe_allow_html=True)
k4.markdown(f'<div class="kpi-card"><div class="kpi-lbl">ATM IV</div><div class="kpi-val">{atm_iv:.2f}%</div><div class="kpi-sub">Implied Volatility</div></div>', unsafe_allow_html=True)
k5.markdown(f'<div class="kpi-card"><div class="kpi-lbl">Max Pain</div><div class="kpi-val">{max_pain:,}</div><div class="kpi-sub">{mp_arrow} {abs(mp_gap):.0f} pts from spot ({mp_bias})</div></div>', unsafe_allow_html=True)
k6.markdown(f'<div class="kpi-card"><div class="kpi-lbl">Active Signals</div><div class="kpi-val" style="color:#00c853">{len(st.session_state.signals)}</div><div class="kpi-sub">{len(st.session_state.exit_signals)} exit alerts</div></div>', unsafe_allow_html=True)

st.markdown("<hr>", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════
#  TABS
# ══════════════════════════════════════════════════════════════
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "🚨 Live Signals", "📋 Option Chain", "📈 OI & IV Charts", "👁️ Watchlist", "📜 Guide & Log"
])


# ─────────────────────────────────────────────────────────────
# TAB 1 — LIVE SIGNALS
# ─────────────────────────────────────────────────────────────
with tab1:
    exits  = st.session_state.exit_signals
    calls  = [s for s in st.session_state.signals if s.kind == "BUY_CALL"]
    puts_s = [s for s in st.session_state.signals if s.kind == "BUY_PUT"]

    # EXIT alerts first (urgent)
    if exits:
        st.markdown("### 🟡 EXIT ALERTS — Take Action")
        for sig in exits:
            r = " &nbsp;|&nbsp; ".join(sig.reasons)
            st.markdown(f"""
<div class="sig-exit">
  <span class="badge badge-exit">EXIT</span>
  <b> {sig.tradingsymbol}</b> &nbsp; Score {sig.score}/10 <br>
  <b>Current ₹{sig.ltp:.2f}</b> &nbsp;|&nbsp; Δ {sig.delta:.3f} &nbsp;|&nbsp; IV {sig.iv:.1f}% &nbsp;|&nbsp; OI {sig.oi:,}<br>
  <small style="color:#ccc">{r}</small>
</div>""", unsafe_allow_html=True)
        st.markdown("<hr>", unsafe_allow_html=True)

    if not calls and not puts_s:
        st.info(
            "📭  No signals at this moment — market conditions haven't crossed the thresholds yet.\n\n"
            "The dashboard is watching for: PCR extreme, low IV Rank, OI buildup, good delta, "
            "volume, and max pain alignment. Check the **Watchlist** tab for near-signal strikes."
        )
    else:
        c1, c2 = st.columns(2)

        with c1:
            st.markdown(f"### 🟢 BUY CALL &nbsp;<small>({len(calls)} signals)</small>", unsafe_allow_html=True)
            if not calls:
                st.info("No call signals now")
            for sig in calls[:6]:
                sc_bar = int(sig.score / 10 * 100)
                reasons_html = "<br>".join(f"✅ {r}" for r in sig.reasons)
                st.markdown(f"""
<div class="sig-call">
  <span class="badge badge-call">BUY CALL</span> &nbsp;
  <b>{sig.tradingsymbol}</b> &nbsp; Score {sig.score}/10<br>
  <div class="score-bar"><div style="width:{sc_bar}%;height:8px;background:#00c853;border-radius:4px;"></div></div>
  <b>₹{sig.ltp:.2f}</b> &nbsp; Strike {sig.strike} &nbsp; Expiry {sig.expiry} &nbsp; DTE {sig.score}<br>
  Δ <code>{sig.delta:.4f}</code> &nbsp; Γ <code>{sig.gamma:.5f}</code> &nbsp;
  Θ <code>{sig.theta:.2f}</code>/d &nbsp; ν <code>{sig.vega:.4f}</code><br>
  IV <b>{sig.iv:.1f}%</b> &nbsp; OI {sig.oi:,} &nbsp; <code>{sig.oi_flag} {sig.oi_change_pct:+.0f}%</code><br>
  <small style="color:#9f9">{reasons_html}</small>
</div>""", unsafe_allow_html=True)

        with c2:
            st.markdown(f"### 🔴 BUY PUT &nbsp;<small>({len(puts_s)} signals)</small>", unsafe_allow_html=True)
            if not puts_s:
                st.info("No put signals now")
            for sig in puts_s[:6]:
                sc_bar = int(sig.score / 10 * 100)
                reasons_html = "<br>".join(f"✅ {r}" for r in sig.reasons)
                st.markdown(f"""
<div class="sig-put">
  <span class="badge badge-put">BUY PUT</span> &nbsp;
  <b>{sig.tradingsymbol}</b> &nbsp; Score {sig.score}/10<br>
  <div class="score-bar"><div style="width:{sc_bar}%;height:8px;background:#ff1744;border-radius:4px;"></div></div>
  <b>₹{sig.ltp:.2f}</b> &nbsp; Strike {sig.strike} &nbsp; Expiry {sig.expiry}<br>
  Δ <code>{sig.delta:.4f}</code> &nbsp; Γ <code>{sig.gamma:.5f}</code> &nbsp;
  Θ <code>{sig.theta:.2f}</code>/d &nbsp; ν <code>{sig.vega:.4f}</code><br>
  IV <b>{sig.iv:.1f}%</b> &nbsp; OI {sig.oi:,} &nbsp; <code>{sig.oi_flag} {sig.oi_change_pct:+.0f}%</code><br>
  <small style="color:#f99">{reasons_html}</small>
</div>""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
# TAB 2 — OPTION CHAIN
# ─────────────────────────────────────────────────────────────
with tab2:
    expiry_list = snap.get("expiries", [])
    sel_exp = st.selectbox("Expiry", expiry_list, key="exp_sel")

    if sel_exp:
        try:
            exp_date = datetime.datetime.strptime(sel_exp, "%d-%b-%Y").date()
        except Exception:
            exp_date = None

        sub = chain_df[chain_df["expiry_str"] == sel_exp] if "expiry_str" in chain_df.columns else chain_df

        if sub.empty:
            st.warning("No data for selected expiry")
        else:
            calls_ch = sub[sub["type"] == "CE"].set_index("strike")
            puts_ch  = sub[sub["type"] == "PE"].set_index("strike")
            strikes  = sorted(set(calls_ch.index) | set(puts_ch.index))

            def gv(d, k):
                try:
                    v = d.loc[k, k] if isinstance(d.loc[k], pd.DataFrame) else d.loc[k]
                    return v[k] if isinstance(v, pd.Series) else v
                except Exception:
                    return None

            rows = []
            for s in strikes:
                c = calls_ch.loc[s] if s in calls_ch.index else None
                p = puts_ch.loc[s]  if s in puts_ch.index  else None
                is_atm = (s == atm)

                def fv(obj, key, fmt=None):
                    if obj is None: return "—"
                    try:
                        val = obj[key]
                        if fmt: return fmt.format(val)
                        return val
                    except Exception: return "—"

                rows.append({
                    "Call OI":      fv(c, "oi",     "{:,}"),
                    "C OI Chg":     fv(c, "oi_change_pct", "{:+.0f}%"),
                    "Call Vol":     fv(c, "volume",  "{:,}"),
                    "Call IV%":     fv(c, "iv",      "{:.1f}"),
                    "Call Δ":       fv(c, "delta",   "{:.3f}"),
                    "Call LTP":     fv(c, "ltp",     "₹{:.2f}"),
                    "⭐ STRIKE":     f"{'★ ' if is_atm else ''}{int(s)}",
                    "Put LTP":      fv(p, "ltp",     "₹{:.2f}"),
                    "Put Δ":        fv(p, "delta",   "{:.3f}"),
                    "Put IV%":      fv(p, "iv",      "{:.1f}"),
                    "Put Vol":      fv(p, "volume",  "{:,}"),
                    "P OI Chg":     fv(p, "oi_change_pct", "{:+.0f}%"),
                    "Put OI":       fv(p, "oi",      "{:,}"),
                    "OI Flag":      fv(c, "oi_flag") or fv(p, "oi_flag"),
                })

            table_df = pd.DataFrame(rows)

            def hl(row):
                try:
                    s = int(str(row["⭐ STRIKE"]).replace("★ ", ""))
                    if s == atm:
                        return ["background-color:#1a1a50; font-weight:bold"] * len(row)
                except Exception:
                    pass
                return [""] * len(row)

            st.dataframe(
                table_df.style.apply(hl, axis=1),
                use_container_width=True, height=580,
            )
            st.caption(f"★ = ATM ({atm})   |   OI Flag: buildup / unwinding / neutral")


# ─────────────────────────────────────────────────────────────
# TAB 3 — OI & IV CHARTS
# ─────────────────────────────────────────────────────────────
with tab3:
    expiry_list = snap.get("expiries", [])
    sel_exp2 = st.selectbox("Expiry for charts", expiry_list, key="exp_chart")
    sub2 = chain_df[chain_df["expiry_str"] == sel_exp2] if "expiry_str" in chain_df.columns else chain_df

    if not sub2.empty:
        calls2 = sub2[sub2["type"] == "CE"].sort_values("strike")
        puts2  = sub2[sub2["type"] == "PE"].sort_values("strike")

        # ── OI Bar Chart ──────────────────────────────────────
        fig_oi = go.Figure()
        fig_oi.add_trace(go.Bar(x=calls2["strike"], y=calls2["oi"],
            name="Call OI", marker_color="rgba(0,200,83,0.75)"))
        fig_oi.add_trace(go.Bar(x=puts2["strike"], y=puts2["oi"],
            name="Put OI",  marker_color="rgba(255,23,68,0.75)"))
        fig_oi.add_vline(x=spot, line_color="white", line_dash="dash",
                         annotation_text=f"Spot {spot:.0f}", annotation_font_color="white")
        fig_oi.add_vline(x=max_pain, line_color="gold", line_dash="dot",
                         annotation_text=f"MaxPain {max_pain}", annotation_font_color="gold")
        fig_oi.update_layout(title="Open Interest by Strike", template="plotly_dark",
                              barmode="group", height=380,
                              xaxis_title="Strike", yaxis_title="Open Interest",
                              legend=dict(orientation="h", y=1.1))

        # ── OI Change Chart ───────────────────────────────────
        fig_chg = go.Figure()
        fig_chg.add_trace(go.Bar(
            x=calls2["strike"], y=calls2["oi_change"],
            name="Call OI Δ",
            marker_color=["#00c853" if v >= 0 else "#ff1744" for v in calls2["oi_change"]],
        ))
        fig_chg.add_trace(go.Bar(
            x=puts2["strike"], y=puts2["oi_change"],
            name="Put OI Δ",
            marker_color=["#ff6090" if v >= 0 else "#00e676" for v in puts2["oi_change"]],
        ))
        fig_chg.add_vline(x=spot, line_color="white", line_dash="dash")
        fig_chg.update_layout(title="OI Change (Buildup = Fresh Positions  |  Unwinding = Exiting)",
                               template="plotly_dark", barmode="group", height=340,
                               xaxis_title="Strike", yaxis_title="OI Change")

        col_a, col_b = st.columns([1, 1])
        col_a.plotly_chart(fig_oi,  use_container_width=True)
        col_b.plotly_chart(fig_chg, use_container_width=True)

        # ── IV Skew ───────────────────────────────────────────
        fig_iv = go.Figure()
        fig_iv.add_trace(go.Scatter(x=calls2["strike"], y=calls2["iv"],
            mode="lines+markers", name="Call IV",
            line=dict(color="#00c853", width=2), marker=dict(size=5)))
        fig_iv.add_trace(go.Scatter(x=puts2["strike"], y=puts2["iv"],
            mode="lines+markers", name="Put IV",
            line=dict(color="#ff1744", width=2), marker=dict(size=5)))
        fig_iv.add_vline(x=spot, line_color="white", line_dash="dash",
                         annotation_text=f"Spot {spot:.0f}")
        fig_iv.add_hrect(y0=0, y1=min(calls2["iv"].min(), puts2["iv"].min()),
                         fillcolor="rgba(0,200,83,0.05)", line_width=0)
        fig_iv.update_layout(title="IV Skew — Buy when IV is at the LOW end of the smile",
                              template="plotly_dark", height=320,
                              xaxis_title="Strike", yaxis_title="Implied Volatility %")

        # ── PCR Gauge ─────────────────────────────────────────
        fig_pcr = go.Figure(go.Indicator(
            mode="gauge+number",
            value=pcr,
            number={"suffix": "", "font": {"size": 40, "color": "white"}},
            title={"text": "Put-Call Ratio (PCR)", "font": {"color": "white"}},
            gauge={
                "axis": {"range": [0, 2.5], "tickcolor": "white"},
                "steps": [
                    {"range": [0.0, 0.70], "color": "#004d1a"},
                    {"range": [0.70, 1.30], "color": "#333"},
                    {"range": [1.30, 2.50], "color": "#4d0000"},
                ],
                "bar": {"color": pcr_col, "thickness": 0.25},
                "threshold": {"line": {"color": "white", "width": 3},
                              "thickness": 0.75, "value": pcr},
            },
        ))
        fig_pcr.update_layout(template="plotly_dark", height=280,
                               paper_bgcolor="#0e1117", font_color="white")

        col_c, col_d = st.columns([1, 1])
        col_c.plotly_chart(fig_iv,  use_container_width=True)
        col_d.plotly_chart(fig_pcr, use_container_width=True)

        # ── Delta Profile ────────────────────────────────────
        fig_delta = go.Figure()
        fig_delta.add_trace(go.Scatter(x=calls2["strike"], y=calls2["delta"],
            mode="lines+markers", name="Call Δ",
            line=dict(color="#00c853", width=2)))
        fig_delta.add_trace(go.Scatter(x=puts2["strike"], y=puts2["delta"],
            mode="lines+markers", name="Put Δ",
            line=dict(color="#ff1744", width=2)))
        fig_delta.add_vline(x=spot, line_color="white", line_dash="dash")
        fig_delta.add_hrect(y0=DELTA_MIN, y1=DELTA_MAX,
            fillcolor="rgba(0,200,83,0.08)", line_width=0,
            annotation_text="Buy Zone (Δ 0.25–0.60)", annotation_font_color="#00c853")
        fig_delta.add_hrect(y0=-DELTA_MAX, y1=-DELTA_MIN,
            fillcolor="rgba(255,23,68,0.08)", line_width=0)
        fig_delta.update_layout(title="Delta Profile — Shaded = Sweet Spot for Buying",
                                 template="plotly_dark", height=300,
                                 xaxis_title="Strike", yaxis_title="Delta")
        st.plotly_chart(fig_delta, use_container_width=True)


# ─────────────────────────────────────────────────────────────
# TAB 4 — WATCHLIST
# ─────────────────────────────────────────────────────────────
with tab4:
    watches = st.session_state.watch_signals
    st.markdown("### 👁️ Near-Signal Strikes")
    st.caption(f"These haven't crossed the threshold ({MIN_SIGNAL_SCORE}/10) yet. Monitor them closely.")

    if not watches:
        st.info("Nothing notable approaching right now.")
    else:
        for sig in watches:
            is_call = "CE" in sig.tradingsymbol
            cls     = "sig-call" if is_call else "sig-put"
            badge   = '<span class="badge badge-call">CALL</span>' if is_call else '<span class="badge badge-put">PUT</span>'
            r       = " | ".join(sig.reasons)
            sc_bar  = int(sig.score / 10 * 100)
            st.markdown(f"""
<div class="{cls}">
  {badge} <b>{sig.tradingsymbol}</b> &nbsp; Score <b>{sig.score}</b>/10
  &nbsp; (need {MIN_SIGNAL_SCORE})<br>
  <div class="score-bar"><div style="width:{sc_bar}%;height:8px;background:{'#00c853' if is_call else '#ff1744'};border-radius:4px;"></div></div>
  LTP ₹{sig.ltp:.2f} &nbsp; Δ {sig.delta:.3f} &nbsp; IV {sig.iv:.1f}% &nbsp; OI {sig.oi:,}
  &nbsp; {sig.oi_flag} {sig.oi_change_pct:+.0f}%<br>
  <small style="color:#aaa">{r}</small>
</div>""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
# TAB 5 — GUIDE & LOG
# ─────────────────────────────────────────────────────────────
with tab5:
    col_g1, col_g2 = st.columns(2)

    with col_g1:
        st.markdown("### 🧠 Signal Scoring System")
        st.markdown(f"""
| Factor | Max Pts | Condition |
|--------|---------|-----------|
| **PCR Sentiment** | +2 | PCR < {PCR_CALL_BUY} (calls) / > {PCR_PUT_BUY} (puts) |
| **IV Rank Low** | +2 | IV Rank < 20% = very cheap options |
| **OI Buildup** | +2 | OI grew > {OI_BUILDUP_PCT}% on this strike |
| **Delta Zone** | +1 | {DELTA_MIN} ≤ |Δ| ≤ {DELTA_MAX} (sweet spot) |
| **Volume** | +1 | > 1000 contracts |
| **Max Pain Align** | +1 | Spot on right side of max pain |
| **Gamma** | +1 | Γ ≥ 0.0005 (near ATM) |
| **Total** | **10** | **Alert fires at ≥ {MIN_SIGNAL_SCORE}** |

*Edit thresholds in `config.py`*
""")

        st.markdown("### 📤 Exit Signal Logic")
        st.markdown(f"""
The dashboard monitors your open positions and fires exit alerts when:

- **+{TARGET_PROFIT_PCT}% profit** reached — take profits
- **-{STOP_LOSS_PCT}% loss** reached — cut losses
- **IV Rank > {IV_RANK_EXIT_MIN}%** — options got expensive, book now
- **OI Unwinding** on your strike — smart money exiting
- **|Δ| > 0.75** — deep ITM, lock in gains
- **PCR reversal** — market bias flipped against you
- **Expiry tomorrow** — exit before theta crush
""")

    with col_g2:
        st.markdown("### 📖 How Top Traders Read This Data")
        st.markdown("""
**PCR as Contrarian Indicator**
When PCR < 0.70, more calls are open than puts — the market is crowded bullish but
*protected* with puts. Institutional hedging makes sharp falls less likely. A rising
Nifty in a low-PCR environment = buy calls cheaply before the crowd does.

**IV Rank: Time Your Entry Like a Pro**
Never buy options when IV is high — you're overpaying for every rupee of premium.
IV Rank < 30% means options are cheap *relative to history*. When volatility mean-reverts
(which it always does), you profit from both the directional move AND vega expansion.

**OI Buildup = Follow the Big Money**
Volume tells you who traded *today*. OI tells you who's *still committed*. A 15–30%
spike in OI at a strike means institutional money just opened a large position there.
Strikes with fresh OI buildup are more likely to be defended or become targets.

**Max Pain: The Weekly Magnet**
Every week, market makers are short options across all strikes. They're incentivized to
pin price near the level where most options expire worthless — Max Pain. In the last
session of expiry, price gravitates toward Max Pain ~70% of the time. Trade accordingly.

**Delta: Not Too Hot, Not Too Cold**
Δ 0.25–0.50 options give the best risk-reward ratio:
• Δ < 0.20 = lottery ticket (usually expires worthless)
• Δ > 0.65 = behaves like the underlying, expensive
• Δ 0.30–0.50 = sweet spot: affordable, responds well to moves
""")

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown("### 📜 Alert History (this session)")
    if st.session_state.alert_log:
        st.dataframe(pd.DataFrame(st.session_state.alert_log), use_container_width=True)
    else:
        st.info("No Telegram alerts sent yet this session")

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown("### ⚙️ Setup Instructions")
    st.code("""
# 1. Install dependencies
pip install -r requirements.txt

# 2. (Optional) Configure Telegram in config.py
TELEGRAM_BOT_TOKEN = "your_token"
TELEGRAM_CHAT_ID   = "your_chat_id"

# 3. Run the dashboard
streamlit run dashboard.py

# No API key, no login, no subscription needed.
# Data comes directly from NSE India (free).
""", language="bash")


# ══════════════════════════════════════════════════════════════
#  AUTO-REFRESH
# ══════════════════════════════════════════════════════════════
st.markdown(f"<hr><small>⟳ Auto-refreshes every {REFRESH_SECONDS}s &nbsp;|&nbsp; Source: NSE India API (free) &nbsp;|&nbsp; Greeks: Black-Scholes</small>", unsafe_allow_html=True)

time.sleep(REFRESH_SECONDS)
refresh()
st.rerun()