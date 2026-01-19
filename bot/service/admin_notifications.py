"""
Модуль для отправки уведомлений администратору с прикреплением лог-файлов.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.types import FSInputFile
from service.config import settings

logger = logging.getLogger(__name__)

# Директория для логов (по умолчанию bot/logs)
LOGS_DIR = Path(__file__).parent.parent / "logs"

# Коoldown для уведомлений об ошибках (в секундах)
ERROR_ALERT_COOLDOWN = 3  # 3 минут


def get_latest_log_file() -> Optional[Path]:
    """
    Находит самый свежий лог-файл в директории logs.

    Проверяет основной файл bot.log и все ротированные файлы (bot.log.1, bot.log.2, etc.),
    возвращает файл с самой поздней датой модификации.

    Returns:
        Path к самому свежему лог-файлу или None, если файлы не найдены
    """
    if not LOGS_DIR.exists():
        logger.warning(f"Директория логов не найдена: {LOGS_DIR}")
        return None

    # Ищем все файлы логов (bot.log, bot.log.1, bot.log.2, etc.)
    log_files = []
    base_log = LOGS_DIR / "bot.log"
    if base_log.exists():
        log_files.append(base_log)

    # Ищем ротированные файлы
    for i in range(1, 11):  # Максимум 10 ротированных файлов
        rotated_log = LOGS_DIR / f"bot.log.{i}"
        if rotated_log.exists():
            log_files.append(rotated_log)
        else:
            # Если файла нет, дальше искать не нужно (ротация последовательная)
            break

    if not log_files:
        logger.warning("Лог-файлы не найдены")
        return None

    # Возвращаем файл с самой поздней датой модификации
    latest_file = max(log_files, key=lambda p: p.stat().st_mtime)
    return latest_file


async def send_admin_notification_with_log(
    bot: Bot, message: str, log_file: Optional[Path] = None
) -> bool:
    """
    Отправляет уведомление администратору с прикреплением лог-файла.

    Args:
        bot: Экземпляр aiogram Bot
        message: Текст сообщения для администратора
        log_file: Путь к лог-файлу (если None, будет найден автоматически)

    Returns:
        True если уведомление отправлено успешно, False в случае ошибки
    """
    if not settings.admin_telegram_id or settings.admin_telegram_id == 0:
        logger.debug("Admin telegram ID не настроен, уведомление не отправлено")
        return False

    try:
        # Если файл не указан, находим самый свежий
        if log_file is None:
            log_file = get_latest_log_file()

        # Отправляем сообщение
        await bot.send_message(
            chat_id=settings.admin_telegram_id,
            text=message,
            parse_mode="HTML",
        )

        # Если есть лог-файл, отправляем его как документ
        if log_file and log_file.exists():
            try:
                document = FSInputFile(log_file)
                await bot.send_document(
                    chat_id=settings.admin_telegram_id,
                    document=document,
                    caption="📄 Latest log file",
                )
                logger.info(f"Лог-файл отправлен администратору: {log_file.name}")
            except Exception as e:
                logger.error(f"Ошибка при отправке лог-файла администратору: {e}")
                # Отправляем текстовое сообщение об ошибке
                await bot.send_message(
                    chat_id=settings.admin_telegram_id,
                    text=f"⚠️ Не удалось прикрепить лог-файл: {e}",
                )
        else:
            logger.warning("Лог-файл не найден для отправки администратору")
            await bot.send_message(
                chat_id=settings.admin_telegram_id,
                text="⚠️ Лог-файл не найден",
            )

        return True

    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления администратору: {e}")
        return False


class AdminErrorAlertHandler(logging.Handler):
    """
    Обработчик логов для отправки уведомлений администратору при ошибках.

    Отправляет уведомления только для записей уровня ERROR и выше,
    с применением cooldown для предотвращения спама.
    """

    def __init__(self, bot: Bot, cooldown_seconds: int = ERROR_ALERT_COOLDOWN):
        """
        Инициализирует обработчик.

        Args:
            bot: Экземпляр aiogram Bot для отправки уведомлений
            cooldown_seconds: Интервал cooldown в секундах между уведомлениями
        """
        super().__init__()
        self.bot = bot
        self.cooldown_seconds = cooldown_seconds
        self.last_alert_time: float = 0.0
        self.setLevel(logging.ERROR)  # Только ERROR и выше

    def emit(self, record: logging.LogRecord) -> None:
        """
        Обрабатывает запись лога и отправляет уведомление, если необходимо.

        Args:
            record: Запись лога для обработки
        """
        # Проверяем уровень логирования
        if record.levelno < logging.ERROR:
            return

        # Проверяем cooldown
        current_time = time.time()
        if current_time - self.last_alert_time < self.cooldown_seconds:
            return

        # Обновляем время последнего уведомления
        self.last_alert_time = current_time

        # Формируем сообщение
        message = (
            f"🚨 <b>Error Alert</b>\n\n"
            f"<b>Level:</b> {record.levelname}\n"
            f"<b>Module:</b> {record.name}\n"
            f"<b>Message:</b> {record.getMessage()}\n"
        )

        # Добавляем информацию о файле и строке, если доступна
        if record.pathname:
            message += f"<b>File:</b> {record.pathname}"
            if record.lineno:
                message += f":{record.lineno}"

        # Создаем асинхронную задачу для отправки уведомления
        # Используем try-except, чтобы не блокировать логирование
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(send_admin_notification_with_log(self.bot, message))
        except RuntimeError:
            # Если event loop не запущен, логируем предупреждение
            logger.warning(
                "Event loop не запущен, уведомление администратору не отправлено"
            )
        except Exception as e:
            logger.error(f"Ошибка при создании задачи отправки уведомления: {e}")
