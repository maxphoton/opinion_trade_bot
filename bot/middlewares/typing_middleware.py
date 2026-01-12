"""Middleware для отправки действия печатания перед каждым сообщением бота."""

import logging
from typing import Any, Callable, Dict

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.utils.chat_action import ChatActionSender

logger = logging.getLogger(__name__)


class TypingMiddleware(BaseMiddleware):
    """Middleware для отправки действия печатания перед обработкой сообщений."""

    def __init__(self, bot: Bot):
        super().__init__()
        self.bot = bot

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Any],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        """Отправляет действие печатания перед обработкой события."""
        chat_id = None
        message_thread_id = None

        # Получаем chat_id в зависимости от типа события
        if isinstance(event, Message):
            chat_id = event.chat.id
            if event.is_topic_message:
                message_thread_id = event.message_thread_id
        elif isinstance(event, CallbackQuery) and event.message:
            chat_id = event.message.chat.id
            if event.message.is_topic_message:
                message_thread_id = event.message.message_thread_id

        # Отправляем действие печатания, если удалось получить chat_id
        if chat_id:
            try:
                async with ChatActionSender.typing(
                    bot=self.bot,
                    chat_id=chat_id,
                    message_thread_id=message_thread_id,
                ):
                    return await handler(event, data)
            except Exception as e:
                logger.warning(f"Ошибка при отправке действия печатания: {e}")
                # В случае ошибки продолжаем обработку без действия печатания
                return await handler(event, data)

        # Если chat_id не получен, продолжаем обработку без действия печатания
        return await handler(event, data)
