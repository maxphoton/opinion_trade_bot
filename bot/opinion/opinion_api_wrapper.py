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
import logging
import traceback
from typing import Any, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from opinion_clob_sdk.chain.py_order_utils.model.order import PlaceOrderDataInput
from opinion_clob_sdk.chain.py_order_utils.model.order_type import (
    LIMIT_ORDER,
    MARKET_ORDER,
)
from service.config import USDT_CONTRACT_ADDRESS

logger = logging.getLogger(__name__)

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


def parse_market_url(url: str) -> Tuple[Optional[int], Optional[str]]:
    """Parses Opinion.trade URL and extracts marketId and market type."""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        market_id = None
        market_type = None

        if "topicId" in params:
            market_id = int(params["topicId"][0])

        if "type" in params:
            market_type = params["type"][0]

        return market_id, market_type
    except (ValueError, AttributeError):
        return None, None


async def get_market_info(client, market_id: int, is_categorical: bool = False):
    """Gets market information."""
    try:
        if is_categorical:
            response = client.get_categorical_market(market_id=market_id)
        else:
            response = client.get_market(market_id=market_id, use_cache=True)

        if response.errno == 0:
            return response.result.data

        logger.error(
            f"Error getting market: {response.errmsg} (code: {response.errno})"
        )
        return None
    except Exception as e:
        logger.error(f"Error getting market: {e}")
        return None


def get_categorical_market_submarkets(market) -> list:
    """Extracts list of submarkets from categorical market."""
    if hasattr(market, "child_markets") and market.child_markets:
        return market.child_markets
    return []


async def get_orderbooks(client, yes_token_id: str, no_token_id: str):
    """Gets order books for YES and NO tokens."""
    yes_orderbook = None
    no_orderbook = None

    try:
        response = client.get_orderbook(token_id=yes_token_id)
        if response.errno == 0:
            yes_orderbook = (
                response.result
                if hasattr(response.result, "bids")
                else getattr(response.result, "data", response.result)
            )
    except Exception as e:
        logger.error(f"Error getting orderbook for YES: {e}")

    try:
        response = client.get_orderbook(token_id=no_token_id)
        if response.errno == 0:
            no_orderbook = (
                response.result
                if hasattr(response.result, "bids")
                else getattr(response.result, "data", response.result)
            )
    except Exception as e:
        logger.error(f"Error getting orderbook for NO: {e}")

    return yes_orderbook, no_orderbook


def calculate_spread_and_liquidity(orderbook, token_name: str) -> dict:
    """Calculates spread and liquidity for a token."""
    if not orderbook:
        return {
            "best_bid": None,
            "best_ask": None,
            "spread": None,
            "spread_pct": None,
            "mid_price": None,
            "bid_liquidity": 0,
            "ask_liquidity": 0,
            "total_liquidity": 0,
        }

    bids = orderbook.bids if hasattr(orderbook, "bids") else []
    asks = orderbook.asks if hasattr(orderbook, "asks") else []

    best_bid = None
    if bids and len(bids) > 0:
        bid_prices = [float(bid.price) for bid in bids if hasattr(bid, "price")]
        if bid_prices:
            best_bid = max(bid_prices)

    best_ask = None
    if asks and len(asks) > 0:
        ask_prices = [float(ask.price) for ask in asks if hasattr(ask, "price")]
        if ask_prices:
            best_ask = min(ask_prices)

    spread = None
    spread_pct = None
    mid_price = None

    if best_bid and best_ask:
        spread = best_ask - best_bid
        mid_price = (best_bid + best_ask) / 2
        spread_pct = (spread / mid_price * 100) if mid_price > 0 else 0

    bid_liquidity = sum(float(bid.size) for bid in bids[:5]) if bids else 0
    ask_liquidity = sum(float(ask.size) for ask in asks[:5]) if asks else 0
    total_liquidity = bid_liquidity + ask_liquidity

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "spread_pct": spread_pct,
        "mid_price": mid_price,
        "bid_liquidity": bid_liquidity,
        "ask_liquidity": ask_liquidity,
        "total_liquidity": total_liquidity,
    }


async def check_usdt_balance(client, required_amount: float) -> Tuple[bool, float]:
    """
    Checks if USDT balance is sufficient.

    Returns:
        (has_balance, available_balance)
    """
    try:
        available = await get_usdt_balance(client)
        return available >= required_amount, available
    except Exception as e:
        logger.error(f"Error checking balance: {e}")
        logger.error(traceback.format_exc())
        return False, 0.0


async def place_market_order(
    client, order_params: dict
) -> tuple[bool, str | None, str | None]:
    """
    Places a market order on the exchange.

    Returns:
        (success, order_id, error_message)
    """
    try:
        order_data = PlaceOrderDataInput(
            marketId=order_params["market_id"],
            tokenId=order_params["token_id"],
            side=order_params["side"],
            orderType=MARKET_ORDER,
            price="0",
            makerAmountInQuoteToken=order_params["amount"],
        )

        def _place_order_sync():
            return client.place_limit_order(order_data, check_approval=True)

        result = await asyncio.to_thread(_place_order_sync)

        if result.errno == 0:
            order_id = "N/A"
            if hasattr(result, "result") and hasattr(result.result, "order_data"):
                order_data_obj = result.result.order_data
                if hasattr(order_data_obj, "order_id"):
                    order_id = order_data_obj.order_id
                elif hasattr(order_data_obj, "id"):
                    order_id = order_data_obj.id
            return True, str(order_id), None

        error_msg = (
            result.errmsg
            if hasattr(result, "errmsg") and result.errmsg
            else f"Error code: {result.errno}"
        )
        logger.error(
            f"Error placing market order: {error_msg}\n"
            f"  - errno: {result.errno}\n"
            f"  - errmsg: {getattr(result, 'errmsg', None)}\n"
            f"  - Full result: {result}"
        )
        return False, None, error_msg
    except Exception as exc:
        error_msg = str(exc)
        logger.error(
            f"Exception while placing market order: {error_msg}\n"
            f"  - Exception type: {type(exc).__name__}\n"
            f"  - Exception args: {exc.args}\n"
            f"  - Full traceback:\n{traceback.format_exc()}"
        )
        return False, None, error_msg


async def place_limit_order(
    client, order_params: dict
) -> tuple[bool, str | None, str | None]:
    """
    Places a limit order on the market.

    Returns:
        (success, order_id, error_message)
    """
    try:
        # client.enable_trading()
        # enable_trading() не требуется, так как check_approval=True в place_limit_order()
        # автоматически проверяет и включает торговлю при необходимости.
        # SDK кэширует результат на enable_trading_check_interval (1 час).

        price = float(order_params["price"])
        price_rounded = round(price, 3)

        MIN_PRICE = 0.001
        MAX_PRICE = 0.999

        if price_rounded < MIN_PRICE:
            error_msg = f"Price {price_rounded} is less than minimum {MIN_PRICE}"
            logger.error(error_msg)
            return False, None, error_msg

        if price_rounded > MAX_PRICE:
            error_msg = f"Price {price_rounded} is greater than maximum {MAX_PRICE}"
            logger.error(error_msg)
            return False, None, error_msg

        order_data = PlaceOrderDataInput(
            marketId=order_params["market_id"],
            tokenId=order_params["token_id"],
            side=order_params["side"],
            orderType=LIMIT_ORDER,
            price=str(price_rounded),
            makerAmountInQuoteToken=order_params["amount"],
        )

        logger.info(
            f"Placing order with params: market_id={order_params['market_id']}, "
            f"token_id={order_params['token_id']}, side={order_params['side']}, "
            f"price={price_rounded}, amount={order_params['amount']}"
        )
        logger.info(
            f"Order data object: {order_data}, order_data attributes: {dir(order_data)}"
        )

        def _place_order_sync():
            return client.place_limit_order(order_data, check_approval=True)

        result = await asyncio.to_thread(_place_order_sync)

        if result.errno != 0:
            result_dict = {}
            for attr in dir(result):
                if not attr.startswith("_"):
                    try:
                        value = getattr(result, attr)
                        if not callable(value):
                            result_dict[attr] = value
                    except Exception:
                        pass
            logger.error(f"Result error object all attributes: {result_dict}")

        if result.errno == 0:
            order_id = "N/A"
            if hasattr(result, "result") and hasattr(result.result, "order_data"):
                order_data_obj = result.result.order_data
                if hasattr(order_data_obj, "order_id"):
                    order_id = order_data_obj.order_id
                elif hasattr(order_data_obj, "id"):
                    order_id = order_data_obj.id
            return True, str(order_id), None

        error_msg = (
            result.errmsg
            if hasattr(result, "errmsg") and result.errmsg
            else f"Error code: {result.errno}"
        )
        logger.error(
            f"Error placing order: {error_msg}\n"
            f"  - errno: {result.errno}\n"
            f"  - errmsg: {getattr(result, 'errmsg', None)}\n"
            f"  - Full result: {result}"
        )
        return False, None, error_msg
    except Exception as exc:
        error_msg = str(exc)
        logger.error(
            f"Exception while placing order: {error_msg}\n"
            f"  - Exception type: {type(exc).__name__}\n"
            f"  - Exception args: {exc.args}\n"
            f"  - Full traceback:\n{traceback.format_exc()}"
        )
        return False, None, error_msg


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


async def get_usdt_balance(client) -> float:
    """
    Получает баланс USDT пользователя из API (асинхронная версия).

    Args:
        client: Клиент Opinion SDK

    Returns:
        Баланс USDT в виде float. Возвращает 0.0 в случае ошибки.
    """
    try:
        # Вызываем API в отдельном потоке, так как SDK синхронный
        response = await asyncio.to_thread(client.get_my_balances)

        # Проверяем ошибки
        if response.errno != 0:
            logger.warning(
                f"Ошибка при получении баланса: errno={response.errno}, errmsg={getattr(response, 'errmsg', 'N/A')}"
            )
            return 0.0

        if not hasattr(response, "result") or not response.result:
            logger.warning("Ответ API не содержит result")
            return 0.0

        if not hasattr(response.result, "balances") or not response.result.balances:
            logger.warning("Ответ API не содержит balances")
            return 0.0

        # Ищем баланс USDT в массиве балансов
        # quote_token - это адрес контракта USDT (0x55d398326f99059ff775485246999027b3197955)
        available = 0.0
        for balance in response.result.balances:
            quote_token = getattr(balance, "quote_token", "")
            if quote_token.lower() == USDT_CONTRACT_ADDRESS.lower():
                available_balance_str = getattr(balance, "available_balance", "0")
                available = float(available_balance_str)
                break

        if available == 0.0:
            available_tokens = [
                getattr(b, "quote_token", "unknown") for b in response.result.balances
            ]
            logger.warning(
                f"USDT баланс не найден. Доступные токены: {available_tokens}"
            )

        return available

    except Exception as e:
        logger.error(f"Исключение при получении баланса из API: {e}")
        logger.error(traceback.format_exc())
        return 0.0


async def get_my_positions(client, limit: int = 100) -> List[Any]:
    """
    Получает позиции пользователя из API (асинхронная версия).

    Args:
        client: Клиент Opinion SDK
        limit: Количество позиций для получения (по умолчанию 100)

    Returns:
        Список объектов позиций со всеми полями из API.
        Каждый объект содержит поля: market_id, market_title, shares_owned,
        current_value_in_quote_token, outcome_side_enum, и другие.
    """
    try:
        logger.info(f"Запрос позиций из API: limit={limit}")

        # Вызываем API в отдельном потоке, так как SDK синхронный
        response = await asyncio.to_thread(client.get_my_positions, limit=limit)

        logger.info(
            f"API Response: errno={response.errno}, errmsg={getattr(response, 'errmsg', 'N/A')}"
        )

        # Проверяем ошибки
        if response.errno != 0:
            logger.warning(
                f"Ошибка при получении позиций: errno={response.errno}, errmsg={getattr(response, 'errmsg', 'N/A')}"
            )
            return []

        if not hasattr(response, "result") or not response.result:
            logger.warning("Ответ API не содержит result")
            return []

        if not hasattr(response.result, "list"):
            logger.warning("Ответ API не содержит result.list")
            return []

        # Возвращаем список объектов позиций со всеми полями
        position_list = response.result.list
        position_count = len(position_list) if position_list else 0
        logger.info(f"Получено {position_count} позиций из API")

        return position_list if position_list else []

    except Exception as e:
        logger.error(f"Исключение при получении позиций из API: {e}")
        logger.error(traceback.format_exc())
        return []
