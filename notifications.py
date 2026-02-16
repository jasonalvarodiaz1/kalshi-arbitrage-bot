import smtplib
import json
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, Optional, List
from urllib.request import urlopen, Request
from urllib.error import URLError
import os
import threading
import time
from datetime import datetime

logger = logging.getLogger('kalshi_bot')


class NotificationManager:
    """Manages notifications via email, webhook, console, and digest summaries."""

    def __init__(self):
        # Email config from env
        self.smtp_host = os.getenv('SMTP_HOST')
        self.smtp_port = int(os.getenv('SMTP_PORT', 587))
        self.smtp_user = os.getenv('SMTP_USER')
        self.smtp_password = os.getenv('SMTP_PASSWORD')
        self.email_to = os.getenv('NOTIFICATION_EMAIL')
        self.email_enabled = all([self.smtp_host, self.smtp_user, self.smtp_password, self.email_to])

        # Webhook config from env
        self.webhook_url = os.getenv('WEBHOOK_URL')  # Slack/Discord/generic webhook
        self.webhook_enabled = bool(self.webhook_url)

        # Notification mode: immediate | digest | critical | mute
        self.mode = os.getenv('NOTIFICATION_MODE', 'digest').strip().lower()
        try:
            self.summary_interval_min = int(os.getenv('NOTIFICATION_SUMMARY_INTERVAL_MIN', '120'))
        except ValueError:
            self.summary_interval_min = 120

        if self.email_enabled:
            logger.info("📧 Email notifications enabled")
        if self.webhook_enabled:
            logger.info("🔔 Webhook notifications enabled")
        if not self.email_enabled and not self.webhook_enabled:
            logger.info("📢 Notifications: console only (set SMTP_* or WEBHOOK_URL in .env to enable)")

        # Buffers and synchronization for digest mode
        self._lock = threading.Lock()
        self._opportunities: List[Dict] = []
        self._trades: List[Dict] = []

        self._stop_event = threading.Event()
        self._digest_thread: Optional[threading.Thread] = None

        if self.mode == 'digest':
            # Start background thread to flush summaries periodically
            self._digest_thread = threading.Thread(target=self._digest_worker, daemon=True)
            self._digest_thread.start()
            logger.info(f"🗒️ Notifications: running in DIGEST mode ({self.summary_interval_min} min intervals)")
        else:
            logger.info(f"🗒️ Notifications: mode={self.mode}")

    def _digest_worker(self):
        interval = max(1, self.summary_interval_min) * 60
        while not self._stop_event.wait(interval):
            try:
                self._flush_buffers()
            except Exception as e:
                logger.error(f"Digest worker error: {e}")

    def _flush_buffers(self):
        with self._lock:
            opps = self._opportunities
            trades = self._trades
            self._opportunities = []
            self._trades = []

        if not opps and not trades:
            return

        parts = []
        if opps:
            parts.append(f"Opportunities: {len(opps)}")
            for o in opps:
                parts.append(f"- {o.get('ticker')} | {o.get('profit_cents', 0)}¢ ({o.get('profit_percent', 0):.2f}%) | {o.get('title','')}")

        if trades:
            parts.append(f"Trades executed: {len(trades)}")
            for t in trades:
                parts.append(f"- {t.get('ticker')} | ${t.get('cost',0):.2f} | qty={t.get('quantity')}")

        subject = f"Kalshi Digest: {len(opps)} opps, {len(trades)} trades"
        body = "\n".join(parts)

        self._send_all(subject, body)

    def notify_opportunity(self, opportunity: Dict):
        """Send or buffer notification for an arbitrage opportunity found."""
        if self.mode == 'mute':
            # Console only
            print(f"[NOTIFY] Opportunity: {opportunity.get('ticker')} — {opportunity.get('profit_percent',0):.2f}%")
            return

        if self.mode == 'digest':
            with self._lock:
                self._opportunities.append(opportunity)
            # Also print a concise console line for visibility
            print(f"[DIGEST] Buffered opportunity: {opportunity.get('ticker')} — {opportunity.get('profit_percent',0):.2f}%")
            return

        # immediate or other modes -> send now
        ticker = opportunity.get('ticker', opportunity.get('event_ticker', 'unknown'))
        profit_pct = opportunity.get('profit_percent', 0)
        profit_cents = opportunity.get('profit_cents', 0)
        title = opportunity.get('title', '')

        subject = f"🎯 Kalshi Arb: {ticker} — {profit_pct:.2f}% profit"
        body = (
            f"Arbitrage opportunity detected!\n\n"
            f"Market: {title}\n"
            f"Ticker: {ticker}\n"
            f"YES price: {opportunity.get('yes_price')}¢\n"
            f"NO price: {opportunity.get('no_price')}¢\n"
            f"Total cost: {opportunity.get('total_cost')}¢\n"
            f"Profit: {profit_cents}¢ ({profit_pct:.2f}%)\n"
            f"Strategy: {opportunity.get('strategy')}\n"
            f"Time: {opportunity.get('timestamp')}"
        )
        self._send_all(subject, body)

    def notify_trade_executed(self, trade: Dict):
        """Send or buffer notification when a trade is executed."""
        if self.mode == 'mute':
            print(f"[NOTIFY] Trade executed: {trade.get('ticker')} ${trade.get('cost',0):.2f}")
            return

        if self.mode == 'digest':
            with self._lock:
                self._trades.append(trade)
            print(f"[DIGEST] Buffered trade: {trade.get('ticker')} ${trade.get('cost',0):.2f}")
            return

        subject = f"✅ Kalshi Trade: {trade.get('ticker')} — ${trade.get('cost', 0):.2f}"
        body = (
            f"Trade executed!\n\n"
            f"Ticker: {trade.get('ticker')}\n"
            f"Type: {trade.get('type')}\n"
            f"Quantity: {trade.get('quantity')}\n"
            f"YES price: {trade.get('yes_price')}¢\n"
            f"NO price: {trade.get('no_price')}¢\n"
            f"Total cost: ${trade.get('cost', 0):.2f}\n"
            f"Expected profit: ${trade.get('expected_profit', 0):.2f}\n"
            f"Time: {trade.get('timestamp')}"
        )
        self._send_all(subject, body)

    def notify_error(self, context: str, error: str):
        """Send notification for critical errors (always immediate)."""
        subject = f"❌ Kalshi Bot Error: {context}"
        body = f"Error in {context}:\n\n{error}"
        # Always send immediately regardless of mode
        self._send_all(subject, body)

    def notify_leg_risk(self, ticker: str, order_id: str, cancelled: bool):
        """Send notification when leg risk is detected (always immediate)."""
        status = "cancelled successfully" if cancelled else "FAILED TO CANCEL — MANUAL INTERVENTION REQUIRED"
        subject = f"⚠️ Kalshi Leg Risk: {ticker}"
        body = (
            f"Leg risk detected!\n\n"
            f"Ticker: {ticker}\n"
            f"YES order ID: {order_id}\n"
            f"Status: {status}\n\n"
            f"One side of an arbitrage trade failed. The other side was {'auto-cancelled' if cancelled else 'NOT cancelled'}."
        )
        self._send_all(subject, body)

    def _send_all(self, subject: str, body: str):
        """Send notification via all enabled channels."""
        if self.email_enabled:
            self._send_email(subject, body)
        if self.webhook_enabled:
            self._send_webhook(subject, body)

    def _send_email(self, subject: str, body: str):
        """Send email notification."""
        try:
            msg = MIMEMultipart()
            msg['From'] = self.smtp_user
            msg['To'] = self.email_to
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain'))

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg)
            logger.debug(f"Email sent: {subject}")
        except Exception as e:
            logger.error(f"Failed to send email: {e}")

    def _send_webhook(self, subject: str, body: str):
        """Send webhook notification (compatible with Slack/Discord)."""
        try:
            payload = json.dumps({
                "text": f"*{subject}*\n```{body}```",
                "content": f"**{subject}**\n```{body}```"
            }).encode('utf-8')

            req = Request(self.webhook_url, data=payload, headers={'Content-Type': 'application/json'})
            urlopen(req, timeout=10)
            logger.debug(f"Webhook sent: {subject}")
        except URLError as e:
            logger.error(f"Failed to send webhook: {e}")
        except Exception as e:
            logger.error(f"Webhook error: {e}")

    def stop(self):
        """Stop any background threads and flush pending digests."""
        if self._digest_thread and self._digest_thread.is_alive():
            self._stop_event.set()
            self._digest_thread.join(timeout=5)
        # flush remaining
        try:
            self._flush_buffers()
        except Exception:
            pass
