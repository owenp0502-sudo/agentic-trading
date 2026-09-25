"""
discord_notify.py — Outbound Discord Webhook alerts.

Notification only — never the control plane (per the risk register: if
Discord is down, execution still happened and we log locally).
"""

import logging
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

COLOR_SUCCESS = 0x2ECC71   # green
COLOR_FAILURE = 0xE74C3C   # red


def _mention() -> str:
    """
    Native push pings require the NUMERIC user ID (<@123456789012345678>).
    A username like 'owenpadillaa' cannot be mentioned — warn and skip so
    the embed still delivers (just without the ping).
    """
    uid = config.DISCORD_USER_ID
    if not uid:
        return ""
    if not uid.isdigit():
        logger.warning(
            "DISCORD_USER_ID='%s' is not numeric — mention skipped. "
            "Use Discord: Settings → Advanced → Developer Mode, then "
            "right-click your name → Copy User ID.", uid,
        )
        return ""
    return f"<@{uid}> "


def send_discord_alert(
    action: str,
    ticker: str,
    quantity: Optional[float],
    dollar_val: Optional[float],
    reason: str,
    success: bool = True,
) -> bool:
    """
    POST a rich embed to DISCORD_WEBHOOK_URL mentioning the user.

    Returns True on 2xx, False otherwise. Never raises — a notification
    outage must not take the executor down with it.
    """
    if not config.DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL not set — alert skipped: %s %s",
                       action, ticker)
        return False

    qty_txt = f"{quantity:g}" if quantity is not None else "—"
    val_txt = f"${dollar_val:,.2f}" if dollar_val is not None else "—"
    payload = {
        # 'content' is what actually fires the push notification ping.
        "content": (
            f"{_mention()}{'✅ ORDER EXECUTED' if success else '⚠️ EXECUTION ISSUE'}"
            f" — `{ticker}`"
        ).strip(),
        "embeds": [{
            "title": "TJR SMC Bot — Trade Signal",
            "color": COLOR_SUCCESS if success else COLOR_FAILURE,
            "fields": [
                {"name": "Action", "value": str(action), "inline": True},
                {"name": "Ticker", "value": str(ticker), "inline": True},
                {"name": "Quantity", "value": qty_txt, "inline": True},
                {"name": "Dollar Value", "value": val_txt, "inline": True},
                {"name": "Strategy Reason", "value": reason or "—", "inline": False},
            ],
            "footer": {"text": "Scanner → Render executor · 10% allocation cap"},
        }],
    }

    try:
        resp = requests.post(
            config.DISCORD_WEBHOOK_URL, json=payload,
            timeout=config.HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.error("Discord webhook failed: %s", exc)
        return False
