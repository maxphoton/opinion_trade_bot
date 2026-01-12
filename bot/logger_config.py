"""
Модуль для настройки логирования в приложении.

Предоставляет функцию setup_root_logger для настройки корневого логгера
и setup_logger для создания отдельных логгеров (например, для sync_orders).
"""

import logging
from pathlib import Path
from typing import Optional


def _create_handlers(
    log_file: Path, file_level: int = logging.INFO, console_level: int = logging.WARNING
) -> tuple[logging.FileHandler, logging.StreamHandler]:
    """
    Создает обработчики для файла и консоли с разными уровнями логирования.

    Args:
        log_file: Путь к файлу логов
        file_level: Уровень логирования для файла (по умолчанию INFO - все логи)
        console_level: Уровень логирования для консоли (по умолчанию WARNING - только важные)

    Returns:
        Кортеж (file_handler, console_handler)
    """
    # Детальный формат для файла (с filename:lineno для отладки)
    file_format = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s"
    )

    # Упрощенный формат для консоли (без filename:lineno, чтобы не засорять)
    console_format = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # Обработчик для записи в файл (режим append - добавляет в конец, не затирает)
    # В файл пишем все логи (INFO и выше)
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(file_level)
    file_handler.setFormatter(file_format)

    # Обработчик для консоли
    # В консоль выводим только важные сообщения (WARNING и выше)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(console_format)

    return file_handler, console_handler


def setup_root_logger(
    log_filename: str = "bot.log",
    file_level: int = logging.INFO,
    console_level: int = logging.WARNING,
    logs_dir: Optional[Path] = None,
) -> None:
    """
    Настраивает корневой логгер для всех модулей.

    Все модули, использующие logging.getLogger(__name__), автоматически
    будут логировать через корневой логгер в указанный файл.

    Args:
        log_filename: Имя файла для логов (по умолчанию "bot.log")
        file_level: Уровень логирования для файла (по умолчанию INFO - все логи)
        console_level: Уровень логирования для консоли (по умолчанию WARNING - только важные)
        logs_dir: Директория для логов. Если не указана, используется logs/ в корне проекта

    Example:
        >>> setup_root_logger()
        >>> logger = logging.getLogger(__name__)  # Теперь будет логировать в bot.log
        >>> logger.info("Message")  # Попадет только в файл
        >>> logger.warning("Warning")  # Попадет и в файл, и в консоль
    """
    # Проверяем, не настроен ли уже корневой логгер
    root_logger = logging.getLogger()
    if root_logger.handlers:
        # Логгер уже настроен, ничего не делаем
        return

    # Определяем директорию для логов
    if logs_dir is None:
        logs_dir = Path(__file__).parent.parent / "logs"

    # Создаем папку logs, если её нет
    logs_dir.mkdir(exist_ok=True)

    # Настраиваем корневой логгер
    # Устанавливаем минимальный уровень (INFO), чтобы все логи проходили
    root_logger.setLevel(min(file_level, console_level))

    # Создаем обработчики для корневого логгера с разными уровнями
    log_file = logs_dir / log_filename
    file_handler, console_handler = _create_handlers(
        log_file, file_level, console_level
    )
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)


def setup_logger(
    logger_name: str,
    log_filename: str,
    file_level: int = logging.INFO,
    console_level: int = logging.WARNING,
    logs_dir: Optional[Path] = None,
) -> logging.Logger:
    """
    Настраивает отдельный логгер с записью в свой файл (например, для sync_orders).

    Используется для модулей, которым нужен отдельный файл логов.
    Для обычных модулей используйте setup_root_logger() и logging.getLogger(__name__).

    Args:
        logger_name: Имя логгера (например, "sync_orders")
        log_filename: Имя файла для логов (например, "sync_orders.log")
        file_level: Уровень логирования для файла (по умолчанию INFO - все логи)
        console_level: Уровень логирования для консоли (по умолчанию WARNING - только важные)
        logs_dir: Директория для логов. Если не указана, используется logs/ в корне проекта

    Returns:
        Настроенный логгер

    Example:
        >>> logger = setup_logger("sync_orders", "sync_orders.log")
        >>> logger.info("Sync started")  # Попадет только в файл
        >>> logger.warning("Warning")  # Попадет и в файл, и в консоль
    """
    # Определяем директорию для логов
    if logs_dir is None:
        logs_dir = Path(__file__).parent.parent / "logs"

    # Создаем папку logs, если её нет
    logs_dir.mkdir(exist_ok=True)

    # Получаем или создаем конкретный логгер
    logger = logging.getLogger(logger_name)
    # Устанавливаем минимальный уровень, чтобы все логи проходили
    logger.setLevel(min(file_level, console_level))

    # Удаляем существующие обработчики, чтобы не дублировать логи
    logger.handlers.clear()

    # Создаем обработчики для конкретного логгера с разными уровнями
    log_file = logs_dir / log_filename
    file_handler, console_handler = _create_handlers(
        log_file, file_level, console_level
    )
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # Предотвращаем распространение логов на корневой логгер
    # Это нужно, чтобы логи из конкретного логгера не дублировались в корневом
    logger.propagate = False

    return logger
