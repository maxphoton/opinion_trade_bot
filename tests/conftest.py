"""
Конфигурация pytest для тестов.
Настраивает пути импорта для работы с относительными импортами из bot/
"""

import sys
from pathlib import Path

# Добавляем папку bot в sys.path для работы относительных импортов
# sync_orders.py использует импорты вида "from database import ..."
project_root = Path(__file__).parent.parent
bot_path = project_root / "bot"
if str(bot_path) not in sys.path:
    sys.path.insert(0, str(bot_path))

# Мокируем setup_proxy до импорта sync_orders, чтобы избежать проблем
# sync_orders.py вызывает setup_proxy() при импорте модуля
# Это нужно сделать до того, как тесты импортируют sync_orders
# Используем sys.modules для мокирования без импорта opinion_clob_sdk
from unittest.mock import MagicMock

# Создаем мок модуля client_factory без импорта opinion_clob_sdk
if "client_factory" not in sys.modules:
    mock_client_factory = MagicMock()
    mock_client_factory.setup_proxy = lambda: None
    sys.modules["client_factory"] = mock_client_factory
