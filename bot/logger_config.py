"""
Модуль для настройки логирования в приложении.

Предоставляет функцию setup_logger для создания и настройки логгеров
с записью в файл и выводом в консоль.
"""
import logging
from pathlib import Path
from typing import Optional


def setup_logger(
    logger_name: str,
    log_filename: str,
    level: int = logging.INFO,
    logs_dir: Optional[Path] = None
) -> logging.Logger:
    """
    Настраивает логгер с записью в файл и выводом в консоль.
    
    Args:
        logger_name: Имя логгера (например, "bot", "sync_orders")
        log_filename: Имя файла для логов (например, "bot.log", "sync_orders.log")
        level: Уровень логирования (по умолчанию logging.INFO)
        logs_dir: Директория для логов. Если не указана, используется logs/ в корне проекта
    
    Returns:
        Настроенный логгер
    
    Example:
        >>> logger = setup_logger("bot", "bot.log")
        >>> logger.info("Bot started")
    """
    # Определяем директорию для логов
    if logs_dir is None:
        # Используем директорию logs в корне проекта (на уровень выше bot/)
        # Path(__file__) -> bot/logger_config.py
        # .parent -> bot/
        # .parent -> корень проекта
        # / "logs" -> logs/
        logs_dir = Path(__file__).parent.parent / "logs"
    
    # Создаем папку logs, если её нет
    logs_dir.mkdir(exist_ok=True)
    
    # Получаем или создаем логгер
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    
    # Удаляем существующие обработчики, чтобы не дублировать логи
    logger.handlers.clear()
    
    # Формат логов
    log_format = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    
    # Обработчик для записи в файл (режим append - всегда добавляет в конец)
    log_file = logs_dir / log_filename
    file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
    file_handler.setLevel(level)
    file_handler.setFormatter(log_format)
    logger.addHandler(file_handler)
    
    # Обработчик для консоли (чтобы видеть логи в терминале)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(log_format)
    logger.addHandler(console_handler)
    
    # Предотвращаем распространение логов на корневой логгер
    logger.propagate = False
    
    return logger

