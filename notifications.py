import smtplib
import json
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, Optional, List
from urllib.request import urlopen, Request
from urllib.error import URLError
import os

logger = logging.getLogger('kalshi_bot')


class NotificationManager:
    """Manages notifications via email, webhook, and console."""

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

        if self.email_enabled:
            logger.info("📧 Email notifications enabled")
        if self.webhook_enabled:
            logger.info("🔔 Webhook notifications enabled")
        if not self.email_enabled and not self.webhook_enabled:
            logger.info("📢 Notifications: console only (set SMTP_* or WEBHOOK_URL in .env to enable)")

    def notify_opportunity(self, opportunity: Dict):
        """Send notification for an arbitrage opportunity found."""
        ticker = opportunity.get('ticker', opportunity.get('event_ticker', 'unknown'))
        profit_pct = opportunity.get('profit_percent', 0)
        profit_cents = opportunity.get('profit_cents', 0)
        title = opportunity.get('title', '')
        opp_type = opportunity.get('type', 'single')

        subject = f"🎯 Kalshi Arb: {ticker} — {profit_pct:.2f}% profit"

        if opp_type == 'multi_leg':
            body = (
                f"Multi-leg arbitrage opportunity detected!\n\n"
                f"Event: {opportunity.get('event_ticker')}\n"
                f"Legs: {opportunity.get('num_legs')}\n"
                f"Total cost: {opportunity.get('total_cost')}¢\n"
                f"Profit: {profit_cents}¢ ({profit_pct:.2f}%)\n"
                f"Max qty: {opportunity.get('max_executable_qty')}\n"
                f"Strategy: {opportunity.get('strategy')}\n"
                f"Time: {opportunity.get('timestamp')}"
            )
        else:
            body = (
                f"Arbitrage opportunity detected!\n\n"
                f"Market: {title}\n"
                f"Ticker: {ticker}\n"
                f"YES price: {opportunity.get('yes_price')}¢\n"
                f"NO price: {opportunity.get('no_price')}¢\n"
                f"Total cost: {opportunity.get('total_cost')}¢\n"
                f"Profit: {profit_cents}¢ ({profit_pct:.2f}%)\n"
                f"Max qty: {opportunity.get('max_executable_qty', 'N/A')}\n"
                f"Strategy: {opportunity.get('strategy')}\n"
                f"Time: {opportunity.get('timestamp')}"
            )

        self._send_all(subject, body)

    def notify_trade_executed(self, trade: Dict):
        """Send notification when a trade is executed."""
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
        """Send notification for critical errors."""
        subject = f"❌ Kalshi Bot Error: {context}"
        body = f"Error in {context}:\n\n{error}"
        self._send_all(subject, body)

    def notify_leg_risk(self, ticker: str, order_id: str, cancelled: bool):
        """Send notification when leg risk is detected."""
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
                "text": f"*{subject}*\n```{body}```",  # Slack format
                "content": f"**{subject}**\n```{body}```"  # Discord format
            }).encode('utf-8')

            req = Request(self.webhook_url, data=payload, headers={'Content-Type': 'application/json'})
            urlopen(req, timeout=10)
            logger.debug(f"Webhook sent: {subject}")
        except URLError as e:
            logger.error(f"Failed to send webhook: {e}")
        except Exception as e:
            logger.error(f"Webhook error: {e}")
