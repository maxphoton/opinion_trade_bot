"""
Конфигурация pytest для тестов.
Настраивает пути импорта для работы с относительными импортами из bot/
"""
import sys
from pathlib import Path
from unittest.mock import patch

# Добавляем папку bot в sys.path для работы относительных импортов
# sync_orders.py использует импорты вида "from database import ..."
project_root = Path(__file__).parent.parent
bot_path = project_root / "bot"
if str(bot_path) not in sys.path:
    sys.path.insert(0, str(bot_path))

# Мокируем setup_proxy до импорта sync_orders, чтобы избежать проблем
# sync_orders.py вызывает setup_proxy() при импорте модуля
# Это нужно сделать до того, как тесты импортируют sync_orders
import client_factory
client_factory.setup_proxy = lambda: None

