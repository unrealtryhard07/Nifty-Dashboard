"""
╔══════════════════════════════════════════════════════╗
║     NIFTY OPTIONS DASHBOARD — Configuration          ║
║     Edit ONLY this file before running.              ║
╚══════════════════════════════════════════════════════╝

HOW TO RUN:
  pip install -r requirements.txt
  streamlit run dashboard.py
"""

# ─── TELEGRAM (optional but recommended) ─────────────────────
# 1. Message @BotFather → /newbot → copy token
# 2. Message your bot once, then open:
#    https://api.telegram.org/bot<TOKEN>/getUpdates  → find chat_id
TELEGRAM_BOT_TOKEN = ""   # e.g. "7123456789:AAFxxxxx"
TELEGRAM_CHAT_ID   = ""   # e.g. "987654321"

# ─── MARKET PARAMETERS ───────────────────────────────────────
SYMBOL          = "NIFTY"   # or "BANKNIFTY"
NIFTY_STEP      = 50        # strike step size
NUM_EXPIRIES    = 2         # how many expiries to show (1 = nearest only)
STRIKES_AROUND_ATM = 12    # how many strikes above & below ATM to scan

# ─── REFRESH ─────────────────────────────────────────────────
REFRESH_SECONDS = 60        # dashboard auto-refresh interval

# ─── SIGNAL THRESHOLDS ───────────────────────────────────────
# PCR — Put-Call Ratio
PCR_CALL_BUY  = 0.70    # PCR below this → BUY CALLS (market oversold)
PCR_PUT_BUY   = 1.30    # PCR above this → BUY PUTS  (market overbought)

# IV Rank (0–100%). Buy options only when IV is cheap.
IV_RANK_BUY_MAX  = 40   # only buy if IV Rank < 40%
IV_RANK_EXIT_MIN = 70   # exit when IV Rank > 70% (options expensive)

# Delta sweet-spot for strike selection
DELTA_MIN = 0.25         # don't buy if |delta| < this (too far OTM)
DELTA_MAX = 0.60         # don't buy if |delta| > this (too deep ITM)

# OI change % to flag buildup/unwinding
OI_BUILDUP_PCT  =  15   # ≥ +15% OI change = fresh buildup
OI_UNWIND_PCT   = -10   # ≤ -10% OI change = unwinding

# P&L targets (% of premium paid)
TARGET_PROFIT_PCT = 50   # exit when up 50%
STOP_LOSS_PCT     = 30   # exit when down 30%

# Minimum score (out of 10) to fire a BUY alert
MIN_SIGNAL_SCORE = 6

# Risk-free rate for Black-Scholes Greeks
RISK_FREE_RATE = 0.068   # ~6.8% (India 91-day T-bill)