# email_reporter.py
#
# Scrape-only email alerts for the BTC Kalshi bot.
#
# Sends a "scrape" email whenever portfolio balance exceeds the floor
# by $20+. No hourly digests, no loss alerts.
#
# HOW TO USE (BTC bot only):
# 1. Add at top:          import email_reporter
# 2. After logging setup: email_reporter.start()
# 3. After balance fetch: email_reporter.update_balance(bal)

import smtplib
import threading
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from zoneinfo import ZoneInfo
import logging

EST = ZoneInfo('America/New_York')

log = logging.getLogger('email_reporter')

# ── CONFIG ─────────────────────────────────────────────────────────────────────

EMAIL_FROM         = os.getenv('EMAIL_FROM', '')        # your Gmail address
EMAIL_TO           = os.getenv('EMAIL_TO', '')          # where to send reports
EMAIL_PASS         = os.getenv('EMAIL_PASS', '')        # Gmail App Password (16 chars)
EMAIL_ENABLED      = os.getenv('EMAIL_ENABLED', 'true').lower() == 'true'

SCRAPE_FLOOR       = 110.00     # withdraw 100% of balance above this
SCRAPE_MIN_AMOUNT  = 20.00      # only email when withdrawable amount >= this

# ── SHARED STATE ───────────────────────────────────────────────────────────────

_lock = threading.Lock()

_portfolio_balance = 0.0
_scrape_sent       = False      # True once we've emailed for current spike

# ── PUBLIC API ─────────────────────────────────────────────────────────────────

def update_balance(balance_usd: float):
    """Call whenever you fetch balance from Kalshi API.
    Triggers a scrape email if balance exceeds floor by $20+."""
    global _portfolio_balance, _scrape_sent
    with _lock:
        _portfolio_balance = balance_usd
        withdraw = balance_usd - SCRAPE_FLOOR
        if withdraw >= SCRAPE_MIN_AMOUNT:
            if not _scrape_sent:
                _scrape_sent = True
                subj, html = _build_scrape_email(balance_usd)
                threading.Thread(target=_send_email, args=(subj, html),
                                 daemon=True).start()
        else:
            # Reset flag so next time balance climbs back up we email again
            _scrape_sent = False


def register_trade(**kwargs):
    """No-op kept for backwards compatibility so existing calls don't crash."""
    pass


def start():
    """Call once at bot startup. Logs config — no background thread needed."""
    log.info(f'[EMAIL] Scrape alerts active — floor=${SCRAPE_FLOOR:.0f}, '
             f'min=${SCRAPE_MIN_AMOUNT:.0f}')

# ── EMAIL BUILDER ─────────────────────────────────────────────────────────────

def _build_scrape_email(balance: float) -> tuple:
    withdraw = max(0.0, balance - SCRAPE_FLOOR)
    ts       = datetime.now(EST).strftime('%I:%M %p %Z')
    subj     = f'\U0001f4b0 Kalshi Scrape {ts} | Withdraw ${withdraw:.2f}'
    html     = f"""
<html><body style="font-family:Arial,sans-serif;padding:20px">
  <div style="max-width:480px;margin:auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 8px rgba(0,0,0,0.1);overflow:hidden">
    <div style="background:#1E3A5F;padding:20px">
      <h2 style="color:#fff;margin:0">\U0001f4b0 Scrape Reminder</h2>
      <p style="color:#aac4e8;margin:4px 0 0">{ts}</p>
    </div>
    <div style="padding:24px">
      <p style="font-size:16px">Current Balance: <strong>${balance:.2f}</strong></p>
      <p style="font-size:16px">Scrape Floor: <strong>${SCRAPE_FLOOR:.2f}</strong></p>
      <div style="background:#f8f9fa;border-radius:6px;padding:16px;
                  border-left:4px solid #2ecc71;margin-top:16px">
        <p style="margin:0;font-size:22px;font-weight:bold;color:#2ecc71">
          Withdraw: ${withdraw:.2f}
        </p>
        <p style="margin:8px 0 0;color:#888;font-size:13px">
          100% of balance above ${SCRAPE_FLOOR:.0f} should be withdrawn now.
        </p>
      </div>
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
