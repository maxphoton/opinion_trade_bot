"""
Admin notification helpers for alerts with log attachments.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Iterable, Optional

from aiogram import Bot
from aiogram.types import FSInputFile
from config import settings

logger = logging.getLogger(__name__)

# Directory where log files are stored (root/logs)
LOGS_DIR = Path(__file__).parent.parent / "logs"

# Cooldown for error alerts (seconds)
ERROR_ALERT_COOLDOWN = 180


def _iter_log_files() -> Iterable[Path]:
    """
    Yields candidate log files from logs directory.
    """
    if not LOGS_DIR.exists():
        return []

    candidates: list[Path] = []
    for item in LOGS_DIR.iterdir():
        if not item.is_file():
            continue
        if item.name.startswith("bot.log") or item.name.startswith("sync_orders.log"):
            candidates.append(item)
    return candidates


def get_latest_log_file() -> Optional[Path]:
    """
    Returns the newest log file from the logs directory.
    """
    log_files = list(_iter_log_files())
    if not log_files:
        logger.warning("Log files not found")
        return None
    return max(log_files, key=lambda p: p.stat().st_mtime)


async def send_admin_notification_with_log(
    bot: Bot, message: str, log_file: Optional[Path] = None
) -> bool:
    """
    Sends a notification to the admin with an optional log file attachment.
    """
    if not settings.admin_telegram_id or settings.admin_telegram_id == 0:
        logger.debug("Admin telegram ID not configured, notification skipped")
        return False

    try:
        if log_file is None:
            log_file = get_latest_log_file()

        await bot.send_message(
            chat_id=settings.admin_telegram_id,
            text=message,
            parse_mode="HTML",
        )

        if log_file and log_file.exists():
            document = FSInputFile(log_file)
            await bot.send_document(
                chat_id=settings.admin_telegram_id,
                document=document,
                caption="Latest log file",
            )
        else:
            await bot.send_message(
                chat_id=settings.admin_telegram_id,
                text="Log file not found",
            )
        return True
    except Exception as exc:
        logger.error(f"Failed to send admin notification: {exc}")
        return False


class AdminErrorAlertHandler(logging.Handler):
    """
    Sends admin notifications for ERROR+ log records with cooldown.
    """

    def __init__(self, bot: Bot, cooldown_seconds: int = ERROR_ALERT_COOLDOWN):
        super().__init__(level=logging.ERROR)
        self.bot = bot
        self.cooldown_seconds = cooldown_seconds
        self.last_alert_time: float = 0.0

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR:
            return

        now = time.time()
        if now - self.last_alert_time < self.cooldown_seconds:
            return
        self.last_alert_time = now

        message = (
            "🚨 <b>Error Alert</b>\n\n"
            f"<b>Level:</b> {record.levelname}\n"
            f"<b>Module:</b> {record.name}\n"
            f"<b>Message:</b> {record.getMessage()}\n"
        )
        if record.pathname:
            message += f"<b>File:</b> {record.pathname}:{record.lineno}"

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(send_admin_notification_with_log(self.bot, message))
        except RuntimeError:
            logger.warning("Event loop not running, admin alert skipped")
        except Exception as exc:
            logger.error(f"Failed to schedule admin alert: {exc}")
