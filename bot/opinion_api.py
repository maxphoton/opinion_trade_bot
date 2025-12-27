"""
Обертка для работы с Opinion API.
Функции для получения данных из API с правильной обработкой ответов.
"""

import logging
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)


def get_my_orders(
    client,
    market_id: int = 0,
    status: str = "",
    limit: int = 10,
    page: int = 1
) -> List[Any]:
    """
    Получает ордеры пользователя из API.
    
    Args:
        client: Клиент Opinion SDK
        market_id: ID рынка для фильтрации (по умолчанию 0 = все рынки)
        status: Фильтр по статусу ордера (строка с числовым кодом статуса):
            - "1" → Pending (открытый/активный ордер, status_enum='Pending')
            - "2" → Finished (исполненный ордер, status_enum='Finished', соответствует 'filled')
            - "3" → Canceled (отмененный ордер, status_enum='Canceled', соответствует 'cancelled')
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
        - status=1, status_enum='Pending' → открытый/активный ордер
        - status=2, status_enum='Finished' → исполненный ордер (filled)
        - status=3, status_enum='Canceled' → отмененный ордер (cancelled)
    """
    try:
        # Формируем параметры для запроса
        params = {
            'market_id': market_id,
            'status': status,
            'limit': limit,
            'page': page
        }
        
        logger.info(f"Запрос ордеров из API: market_id={market_id}, status={status}, limit={limit}, page={page}")
        
        # Вызываем API
        response = client.get_my_orders(**params)
        
        logger.info(f"API Response: errno={response.errno}, errmsg={getattr(response, 'errmsg', 'N/A')}")
        
        # Проверяем ошибки
        if response.errno != 0:
            logger.warning(f"Ошибка при получении ордеров: errno={response.errno}, errmsg={getattr(response, 'errmsg', 'N/A')}")
            return []
        
        # response.result.list содержит список ордеров
        if not hasattr(response, 'result') or not response.result:
            logger.warning("Ответ API не содержит result")
            return []
        
        if not hasattr(response.result, 'list'):
            logger.warning("Ответ API не содержит result.list")
            return []
        
        # Возвращаем список объектов ордеров со всеми полями
        order_list = response.result.list
        order_count = len(order_list) if order_list else 0
        logger.info(f"Получено {order_count} ордеров из API")
        
        return order_list if order_list else []
        
    except Exception as e:
        logger.error(f"Исключение при получении ордеров из API: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return []


def get_order_by_id(client, order_id: str) -> Optional[Any]:
    """
    Получает ордер по его ID из API.
    
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
        
        # Вызываем API
        response = client.get_order_by_id(order_id=order_id)
        
        logger.info(f"API Response: errno={response.errno}, errmsg={getattr(response, 'errmsg', 'N/A')}")
        
        # Проверяем ошибки
        if response.errno != 0:
            logger.warning(f"Ошибка при получении ордера: errno={response.errno}, errmsg={getattr(response, 'errmsg', 'N/A')}")
            return None
        
        if not hasattr(response, 'result') or not response.result:
            logger.warning("Ответ API не содержит result")
            return None
        
        # Ордер находится в response.result.order_data
        if not hasattr(response.result, 'order_data'):
            logger.warning("Ответ API не содержит result.order_data")
            return None
        
        # Возвращаем объект ордера со всеми полями
        order = response.result.order_data
        
        logger.info(f"Получен ордер из API: order_id={getattr(order, 'order_id', 'N/A')}")
        
        return order
        
    except Exception as e:
        logger.error(f"Исключение при получении ордера из API: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

