"""Middleware для защиты от спама."""

import logging
import time
from collections import defaultdict, deque
from typing import Any, Callable, Dict

from aiogram import BaseMiddleware, Bot
from aiogram.types import TelegramObject

logger = logging.getLogger(__name__)


class AntiSpamMiddleware(BaseMiddleware):
    def __init__(self, bot: Bot, limit=5, interval=2, block_duration=30):
        super().__init__()
        self.bot = bot
        self.limit = limit
        self.interval = interval
        self.block_duration = block_duration
        self.user_spam_tracker: Dict[int, deque] = defaultdict(deque)
        self.user_blocked_until: Dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Any],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = event.from_user
        if not user:
            return await handler(event, data)

        uid = user.id
        now = time.time()

        if uid in self.user_blocked_until and now < self.user_blocked_until[uid]:
            logger.warning(f"Пользователь {user.full_name} заблокирован за спам")
            await self.bot.send_message(
                uid, "🚫 Please don't spam. Wait 30 seconds."
            )
            return

        timestamps = self.user_spam_tracker[uid]
        timestamps.append(now)

        while timestamps and now - timestamps[0] > self.interval:
            timestamps.popleft()

        if len(timestamps) > self.limit:
            self.user_blocked_until[uid] = now + self.block_duration
            self.user_spam_tracker[uid].clear()
            logger.warning(
                f"🔒 Пользователь {user.full_name} временно заблокирован за спам"
            )
            await self.bot.send_message(
                uid, "🚫 Please don't spam. Wait 30 seconds."
            )
            return

        return await handler(event, data)
