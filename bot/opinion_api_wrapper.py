"""
Обертка для работы с Opinion API.
Функции для получения данных из API с правильной обработкой ответов.

СТРУКТУРА ОБЪЕКТА ОРДЕРА:
==========================
Объект ордера, возвращаемый API, содержит следующие поля:

Основные поля:
- order_id (str): Уникальный идентификатор ордера
- market_id (int): ID рынка
- market_title (str): Название рынка
- root_market_id (int): ID корневого рынка
- root_market_title (str): Название корневого рынка

Статус и состояние:
- status (int): Числовой код статуса (1=Pending, 2=Finished, 3=Canceled)
- status_enum (str): Строковое представление статуса ('Pending', 'Finished', 'Canceled')
- created_at (int): Время создания ордера (Unix timestamp)
- expires_at (int): Время истечения ордера (Unix timestamp, 0 если не истекает)

Цена и количество:
- price (str/float): Цена ордера (десятичное число в виде строки)
- order_amount (str/float): Общая сумма ордера в USDT (десятичное число в виде строки)
- order_shares (str/float): Количество акций в ордере (десятичное число в виде строки)
- filled_amount (str/float): Исполненная сумма в USDT (десятичное число в виде строки)
- filled_shares (str/float): Исполненное количество акций (десятичное число в виде строки)

Направление и токен:
- side (int): Направление ордера (1=Buy, 2=Sell)
- side_enum (str): Строковое представление направления ('Buy', 'Sell')
- outcome (str): Исход ('YES' или 'NO')
- outcome_side (int): Числовой код исхода (1=YES, 2=NO)
- outcome_side_enum (str): Строковое представление исхода ('Yes', 'No')

Торговля:
- trading_method (int): Метод торговли (2=Limit)
- trading_method_enum (str): Строковое представление метода ('Limit')
- trades (list): Список сделок по ордеру (обычно пустой список)

Дополнительные поля:
- quote_token (str): Адрес токена котировки (USDT контракт)
- profit (str): Прибыль (может быть пустой строкой)

Пример объекта ордера:
{
    'order_id': 'def73c87-e120-11f0-8edd-0a58a9feac02',
    'market_id': 2119,
    'market_title': '50+ bps increase',
    'status': 3,
    'status_enum': 'Canceled',
    'side': 1,
    'side_enum': 'Buy',
    'price': '0.983000000000000000',
    'order_amount': '1.999999999999999992',
    'filled_amount': '0.000000000000000000',
    'created_at': 1766619174,
    'expires_at': 0,
    'outcome': 'NO',
    'outcome_side': 2,
    'outcome_side_enum': 'No',
    'trading_method': 2,
    'trading_method_enum': 'Limit',
    'trades': []
}
"""

import asyncio
import traceback
from typing import Any, List, Optional

from logger_config import setup_logger

# Настройка логирования: используем тот же логгер, что и sync_orders,
# так как opinion_api_wrapper используется в основном из sync_orders
logger = setup_logger("opinion_api_wrapper", "sync_orders.log")

# Константы для статусов ордеров (числовые коды из API)
ORDER_STATUS_PENDING = (
    "1"  # Открытый/активный ордер (status_enum='Pending', соответствует 'pending')
)
ORDER_STATUS_FINISHED = (
    "2"  # Исполненный ордер (status_enum='Finished', соответствует 'finished')
)
ORDER_STATUS_CANCELED = (
    "3"  # Отмененный ордер (status_enum='Canceled', соответствует 'canceled')
)


async def get_my_orders(
    client, market_id: int = 0, status: str = "", limit: int = 10, page: int = 1
) -> List[Any]:
    """
    Получает ордеры пользователя из API (асинхронная версия).

    Args:
        client: Клиент Opinion SDK
        market_id: ID рынка для фильтрации (по умолчанию 0 = все рынки)
        status: Фильтр по статусу ордера (строка с числовым кодом статуса):
            - ORDER_STATUS_PENDING ("1") → Pending (открытый/активный ордер, status_enum='Pending', соответствует 'pending')
            - ORDER_STATUS_FINISHED ("2") → Finished (исполненный ордер, status_enum='Finished', соответствует 'finished')
            - ORDER_STATUS_CANCELED ("3") → Canceled (отмененный ордер, status_enum='Canceled', соответствует 'canceled')
            - "" (пустая строка) → все статусы
        limit: Количество ордеров на странице (по умолчанию 10).
               ВАЖНО: Если передавать только limit без page (или page=1),
               API возвращает максимум 20 ордеров, независимо от значения limit
               (даже если limit > 20). Для получения больше 20 ордеров необходимо
               использовать пагинацию с параметром page.
        page: Номер страницы для пагинации (по умолчанию 1)

    Returns:
        Список объектов ордеров со всеми полями из API.
        Каждый объект содержит поля: order_id, status, status_enum, market_id,
        market_title, price, side, side_enum, outcome, order_amount, filled_amount,
        created_at, и другие.

    Note:
        Маппинг статусов из API:
        - status=ORDER_STATUS_PENDING (1), status_enum='Pending' → открытый/активный ордер (pending)
        - status=ORDER_STATUS_FINISHED (2), status_enum='Finished' → исполненный ордер (finished)
        - status=ORDER_STATUS_CANCELED (3), status_enum='Canceled' → отмененный ордер (canceled)
    """
    try:
        # Формируем параметры для запроса
        params = {
            "market_id": market_id,
            "status": status,
            "limit": limit,
            "page": page,
        }

        logger.info(
            f"Запрос ордеров из API: market_id={market_id}, status={status}, limit={limit}, page={page}"
        )

        # Вызываем API в отдельном потоке, так как SDK синхронный
        response = await asyncio.to_thread(client.get_my_orders, **params)

        logger.info(
            f"API Response: errno={response.errno}, errmsg={getattr(response, 'errmsg', 'N/A')}"
        )

        # Проверяем ошибки
        if response.errno != 0:
            logger.warning(
                f"Ошибка при получении ордеров: errno={response.errno}, errmsg={getattr(response, 'errmsg', 'N/A')}"
            )
            return []

        # response.result.list содержит список ордеров
        if not hasattr(response, "result") or not response.result:
            logger.warning("Ответ API не содержит result")
            return []

        if not hasattr(response.result, "list"):
            logger.warning("Ответ API не содержит result.list")
            return []

        # Возвращаем список объектов ордеров со всеми полями
        order_list = response.result.list
        order_count = len(order_list) if order_list else 0
        logger.info(f"Получено {order_count} ордеров из API")

        return order_list if order_list else []

    except Exception as e:
        logger.error(f"Исключение при получении ордеров из API: {e}")
        logger.error(traceback.format_exc())
        return []


async def get_order_by_id(client, order_id: str) -> Optional[Any]:
    """
    Получает ордер по его ID из API (асинхронная версия).

    Args:
        client: Клиент Opinion SDK
        order_id: ID ордера (строка)

    Returns:
        Объект ордера со всеми полями из API, или None в случае ошибки.
        Объект содержит поля: order_id, status, status_enum, market_id,
        market_title, price, side, side_enum, outcome, order_amount,
        filled_amount, maker_amount, created_at, и другие.
    """
    try:
        logger.info(f"Запрос ордера по ID из API: order_id={order_id}")

        # Вызываем API в отдельном потоке, так как SDK синхронный
        response = await asyncio.to_thread(client.get_order_by_id, order_id=order_id)

        logger.info(
            f"API Response: errno={response.errno}, errmsg={getattr(response, 'errmsg', 'N/A')}"
        )

        # Проверяем ошибки
        if response.errno != 0:
            logger.warning(
                f"Ошибка при получении ордера: errno={response.errno}, errmsg={getattr(response, 'errmsg', 'N/A')}"
            )
            return None

        if not hasattr(response, "result") or not response.result:
            logger.warning("Ответ API не содержит result")
            return None

        # Ордер находится в response.result.order_data
        if not hasattr(response.result, "order_data"):
            logger.warning("Ответ API не содержит result.order_data")
            return None

        # Возвращаем объект ордера со всеми полями
        order = response.result.order_data

        logger.info(
            f"Получен ордер из API: order_id={getattr(order, 'order_id', 'N/A')}"
        )

        return order

    except Exception as e:
        # Определяем тип ошибки для правильного уровня логирования
        error_str = str(e)
        is_timeout = (
            "504" in error_str
            or "Gateway Time-out" in error_str
            or "timeout" in error_str.lower()
        )

        if is_timeout:
            # Таймауты - это временные проблемы API, логируем как WARNING
            logger.warning(
                f"Таймаут при получении ордера из API (order_id={order_id}): {error_str}"
            )
            logger.debug(f"Traceback для таймаута:\n{traceback.format_exc()}")
        else:
            # Другие ошибки - логируем как ERROR
            logger.error(
                f"Исключение при получении ордера из API (order_id={order_id}): {e}"
            )
            logger.error(traceback.format_exc())

        return None
