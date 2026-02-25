# email_reporter.py
#
# Shared hourly email digest for all 4 Kalshi bots (BTC, ETH, SOL, XRP)
#
# Drop this file in the same directory as your bot files.
# Uses Gmail SMTP — set EMAIL_FROM, EMAIL_TO, EMAIL_PASS as environment variables on Render.
#
# HOW TO USE IN EACH BOT FILE:
# 1. Add at top:          import email_reporter
# 2. After logging setup: email_reporter.start()
# 3. After balance fetch: email_reporter.update_balance(bal)
# 4. After each settle:   email_reporter.register_trade(
#        asset='BTC',  # BTC / ETH / SOL / XRP
#        side=st.side,
#        price_cents=st.entry_price_cents,
#        qty=st.qty,
#        pnl_cents=pnl,
#        won=(result == st.side)
#    )

import smtplib
import threading
import time
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from collections import defaultdict
import logging

log = logging.getLogger('email_reporter')

# ── CONFIG ─────────────────────────────────────────────────────────────────────

EMAIL_FROM         = os.getenv('EMAIL_FROM', '')        # your Gmail address
EMAIL_TO           = os.getenv('EMAIL_TO', '')          # where to send reports
EMAIL_PASS         = os.getenv('EMAIL_PASS', '')        # Gmail App Password (16 chars)
EMAIL_ENABLED      = os.getenv('EMAIL_ENABLED', 'true').lower() == 'true'

SEND_HOUR_INTERVAL = 3600       # seconds between hourly emails
SCRAPE_FLOOR       = 200.00     # withdraw 100% of balance above this
SCRAPE_HOURS_EST   = {9, 21}    # 9am and 9pm EST  (21 = 9pm in 24hr)
ALERT_HOURLY_LOSS  = -30.00     # send immediate alert if combined hour P&L <= this

# ── SHARED STATE ───────────────────────────────────────────────────────────────

_lock = threading.Lock()

_hourly = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl_cents': 0, 'trades': []})
_daily  = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl_cents': 0})

_portfolio_balance      = 0.0
_start_of_day_balance   = 0.0

# ── PUBLIC API ─────────────────────────────────────────────────────────────────

def register_trade(asset: str, side: str, price_cents: int,
                   qty: int, pnl_cents: int, won: bool):
    """Call this after every settlement. asset = 'BTC' / 'ETH' / 'SOL' / 'XRP'"""
    with _lock:
        h = _hourly[asset]
        d = _daily[asset]
        if won:
            h['wins'] += 1
            d['wins'] += 1
        else:
            h['losses'] += 1
            d['losses'] += 1
        h['pnl_cents'] += pnl_cents
        d['pnl_cents'] += pnl_cents
        h['trades'].append({
            'side': side, 'price': price_cents,
            'qty': qty, 'pnl': pnl_cents, 'won': won,
            'ts': datetime.now(timezone.utc).strftime('%H:%M')
        })


def update_balance(balance_usd: float):
    """Call whenever you fetch balance from Kalshi API."""
    global _portfolio_balance, _start_of_day_balance
    with _lock:
        _portfolio_balance = balance_usd
        if _start_of_day_balance == 0.0:
            _start_of_day_balance = balance_usd


def start():
    """Call once at bot startup to launch the background reporter thread."""
    t = threading.Thread(target=_reporter_loop, daemon=True, name='email-reporter')
    t.start()
    log.info('[EMAIL] Hourly reporter started — scrapes at 9am/9pm EST, alert threshold -$30/hr')

# ── EMAIL BUILDERS ─────────────────────────────────────────────────────────────

def _build_hourly_email() -> str:
    now_str   = datetime.now().strftime('%b %d, %Y  %I:%M %p')
    daily_pnl = _portfolio_balance - _start_of_day_balance
    pnl_sign  = '+' if daily_pnl >= 0 else ''
    status_color = '#2ecc71' if daily_pnl >= 0 else '#e74c3c'

    rows = ''
    for asset in ['BTC', 'ETH', 'SOL', 'XRP']:
        h = _hourly[asset]
        d = _daily[asset]
        h_total = h['wins'] + h['losses']
        d_total = d['wins'] + d['losses']
        h_wr    = f"{h['wins']}/{h_total} ({h['wins']/h_total:.0%})" if h_total else 'N/A'
        d_wr    = f"{d['wins']}/{d_total} ({d['wins']/d_total:.0%})" if d_total else 'N/A'
        h_pnl   = h['pnl_cents'] / 100
        d_pnl   = d['pnl_cents'] / 100
        h_col   = '#27ae60' if h_pnl >= 0 else '#c0392b'
        d_col   = '#27ae60' if d_pnl >= 0 else '#c0392b'
        h_sign  = '+' if h_pnl >= 0 else ''
        d_sign  = '+' if d_pnl >= 0 else ''

        if h_pnl >= 0:
            status = '✅ OK'
        elif h_pnl > -15:
            status = '⚠️ WARN'
        else:
            status = '🛑 CHECK'

        rows += f"""
        <tr style="border-bottom:1px solid #eee">
          <td style="padding:8px 12px;font-weight:bold">{asset}</td>
          <td style="padding:8px 12px;color:{h_col}">{h_sign}${h_pnl:.2f}</td>
          <td style="padding:8px 12px">{h_wr}</td>
          <td style="padding:8px 12px;color:{d_col}">{d_sign}${d_pnl:.2f}</td>
          <td style="padding:8px 12px">{d_wr}</td>
          <td style="padding:8px 12px">{status}</td>
        </tr>"""

    return f"""
<html><body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:20px">
  <div style="max-width:640px;margin:auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 8px rgba(0,0,0,0.1);overflow:hidden">
    <div style="background:#1E3A5F;padding:20px">
      <h2 style="color:#fff;margin:0">Kalshi Bot — Hourly Report</h2>
      <p style="color:#aac4e8;margin:4px 0 0">{now_str}</p>
    </div>
    <div style="padding:20px">
      <div style="background:#f8f9fa;border-radius:6px;padding:16px;margin-bottom:16px;
                  border-left:4px solid {status_color}">
        <p style="margin:0;font-size:18px;font-weight:bold">
          Portfolio Balance: <span style="color:#1E3A5F">${_portfolio_balance:.2f}</span>
        </p>
        <p style="margin:4px 0 0;color:{status_color};font-weight:bold">
          Daily P&L: {pnl_sign}${daily_pnl:.2f}
        </p>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <thead>
          <tr style="background:#1E3A5F;color:#fff">
            <th style="padding:8px 12px;text-align:left">Bot</th>
            <th style="padding:8px 12px;text-align:left">Hour P&L</th>
            <th style="padding:8px 12px;text-align:left">Hour W/L</th>
            <th style="padding:8px 12px;text-align:left">Daily P&L</th>
            <th style="padding:8px 12px;text-align:left">Daily W/L</th>
            <th style="padding:8px 12px;text-align:left">Status</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    <div style="background:#f0f4f8;padding:12px 20px;font-size:12px;color:#888">
      Auto-generated by Kalshi Bot System | Next report in ~1 hour
    </div>
  </div>
</body></html>"""


def _build_scrape_email(balance: float) -> tuple:
    withdraw = max(0.0, balance - SCRAPE_FLOOR)
    ts       = datetime.now().strftime('%I:%M %p')
    color    = '#2ecc71' if withdraw > 0 else '#e74c3c'
    subj     = f'💰 Kalshi Scrape Time {ts} | Withdraw ${withdraw:.2f}'
    html     = f"""
<html><body style="font-family:Arial,sans-serif;padding:20px">
  <div style="max-width:480px;margin:auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 8px rgba(0,0,0,0.1);overflow:hidden">
    <div style="background:#1E3A5F;padding:20px">
      <h2 style="color:#fff;margin:0">💰 Scrape Reminder</h2>
      <p style="color:#aac4e8;margin:4px 0 0">{ts}</p>
    </div>
    <div style="padding:24px">
      <p style="font-size:16px">Current Balance: <strong>${balance:.2f}</strong></p>
      <p style="font-size:16px">Scrape Floor: <strong>${SCRAPE_FLOOR:.2f}</strong></p>
      <div style="background:#f8f9fa;border-radius:6px;padding:16px;
                  border-left:4px solid {color};margin-top:16px">
        <p style="margin:0;font-size:22px;font-weight:bold;color:{color}">
          {'Withdraw: $' + f'{withdraw:.2f}' if withdraw > 0 else '⏳ Below floor — nothing to withdraw'}
        </p>
        <p style="margin:8px 0 0;color:#888;font-size:13px">
          100% of balance above ${SCRAPE_FLOOR:.0f} should be withdrawn now.
        </p>
      </div>
    </div>
  </div>
</body></html>"""
    return subj, html


def _build_alert_email(hour_pnl: float, balance: float) -> tuple:
    subj = f'🚨 KALSHI ALERT | -${abs(hour_pnl):.2f} this hour | Balance: ${balance:.2f}'
    html = f"""
<html><body style="font-family:Arial,sans-serif;padding:20px">
  <div style="max-width:480px;margin:auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 8px rgba(0,0,0,0.1);overflow:hidden">
    <div style="background:#e74c3c;padding:20px">
      <h2 style="color:#fff;margin:0">🚨 Loss Alert</h2>
    </div>
    <div style="padding:24px">
      <p style="font-size:16px">Combined hourly P&L across all 4 bots:</p>
      <p style="font-size:28px;font-weight:bold;color:#e74c3c">-${abs(hour_pnl):.2f}</p>
      <p style="font-size:16px">Current Balance: <strong>${balance:.2f}</strong></p>
      <p style="color:#888">Check Render logs immediately. Consider pausing bots if losses continue.</p>
    </div>
  </div>
</body></html>"""
    return subj, html

# ── INTERNALS ──────────────────────────────────────────────────────────────────

def _send_email(subject: str, html_body: str):
    if not EMAIL_ENABLED:
        log.info('[EMAIL] Disabled — skipping send')
        return
    if not EMAIL_FROM or not EMAIL_PASS or not EMAIL_TO:
        log.warning('[EMAIL] Missing credentials — set EMAIL_FROM / EMAIL_TO / EMAIL_PASS on Render')
        return
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = EMAIL_FROM
        msg['To']      = EMAIL_TO
        msg.attach(MIMEText(html_body, 'html'))
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(EMAIL_FROM, EMAIL_PASS)
            s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        log.info(f'[EMAIL] Sent: {subject}')
    except Exception as e:
        log.error(f'[EMAIL] Send failed: {e}')


def _reset_hourly():
    with _lock:
        for asset in list(_hourly.keys()):
            _hourly[asset] = {'wins': 0, 'losses': 0, 'pnl_cents': 0, 'trades': []}


def _reporter_loop():
    """Background thread — fires every hour on the hour."""
    while True:
        now = time.time()
        secs_to_next_hour = SEND_HOUR_INTERVAL - (now % SEND_HOUR_INTERVAL)
        time.sleep(secs_to_next_hour)

        try:
            with _lock:
                body     = _build_hourly_email()
                bal      = _portfolio_balance
                hour_pnl = sum(v['pnl_cents'] for v in _hourly.values()) / 100

            ts           = datetime.now().strftime('%I:%M %p')
            current_hour = datetime.now().hour   # NOTE: make sure Render timezone = EST
                                                 # or set TZ=America/New_York on Render

            # 1. Standard hourly digest
            _send_email(f'Kalshi Bot Report {ts} | Balance: ${bal:.2f}', body)

            # 2. Scrape reminder at 9am (hour=9) and 9pm (hour=21)
            if current_hour in SCRAPE_HOURS_EST:
                subj, html = _build_scrape_email(bal)
                _send_email(subj, html)

            # 3. Loss alert if combined hour P&L <= -$30
            if hour_pnl <= ALERT_HOURLY_LOSS:
                subj, html = _build_alert_email(hour_pnl, bal)
                _send_email(subj, html)
                log.warning(f'[EMAIL] ALERT SENT — hour_pnl=${hour_pnl:.2f}')

        except Exception as e:
            log.error(f'[EMAIL] Reporter loop error: {e}')

        _reset_hourly()
