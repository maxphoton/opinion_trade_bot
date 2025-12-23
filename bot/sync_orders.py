"""
Script for automatic order movement.
Maintains a constant offset (in ticks) between the current market price and the order's target price.

Algorithm:
1. Retrieves all users from the database
2. For each user:
   a. Retrieves active orders from the database
   b. For each order:
      - Gets the current market price (best_bid for BUY, best_ask for SELL)
      - Calculates the new target price using the saved offset_ticks from the database
        (new_target_price = current_price +/- offset_ticks * tick_size)
      - Checks the target price change:
        * If change < 1 tick (0.001) - skips the order
        * If change >= 1 tick - adds to cancellation and placement lists
      - Sends price change notification to the user (regardless of cancellation/placement success)
   c. Cancels old orders in batch via API
   d. Places new orders in batch only if all old orders were successfully cancelled
   e. Updates the database with new order_id, current_price, and target_price
   f. Sends order updated notification to the user after successful database update
3. Outputs final statistics (cancelled, placed, errors)

Features:
- Uses offset_ticks from the database, does not recalculate the delta
- Skips orders with target price change < 1 tick (saves API calls)
- Checks cancellation success via result_data.errno from API response
- Places new orders only if all old orders were successfully cancelled
- Updates the database only after successful placement
- Sends notifications to the user about price changes and successful updates
- Runs as a background task in the bot, synchronizing orders every 60 seconds
"""
import asyncio
import logging
from typing import List, Dict, Optional, Tuple

from database import get_user, get_user_orders, get_all_users, update_order_in_db
from client_factory import create_client, setup_proxy
from config import TICK_SIZE
from opinion_clob_sdk.chain.py_order_utils.model.order import PlaceOrderDataInput
from opinion_clob_sdk.chain.py_order_utils.model.sides import OrderSide

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Настраиваем прокси
setup_proxy()


def get_current_market_price(client, token_id: str, side: str) -> Optional[float]:
    """
    Получает текущую цену рынка для токена.
    
    Args:
        client: Клиент Opinion SDK
        token_id: ID токена (YES или NO)
        side: BUY или SELL - определяет, какую цену брать (best_bid для BUY, best_ask для SELL)
    
    Returns:
        Текущая цена или None в случае ошибки
    """
    try:
        response = client.get_orderbook(token_id=token_id)
        
        if response.errno != 0:
            logger.error(f"Ошибка получения orderbook для токена {token_id}: errno={response.errno}")
            return None
        
        orderbook = response.result if not hasattr(response.result, 'data') else response.result.data
        
        bids = orderbook.bids if hasattr(orderbook, 'bids') else []
        asks = orderbook.asks if hasattr(orderbook, 'asks') else []
        
        if side == "BUY":
            # Для BUY берем best_bid (самый высокий бид)
            if bids and len(bids) > 0:
                # Сортируем биды по убыванию цены
                bid_prices = []
                for bid in bids:
                    if hasattr(bid, 'price'):
                        try:
                            price = float(bid.price)
                            bid_prices.append(price)
                        except (ValueError, TypeError):
                            continue
                if bid_prices:
                    return max(bid_prices)  # Самый высокий бид
        else:  # SELL
            # Для SELL берем best_ask (самый низкий аск)
            if asks and len(asks) > 0:
                # Сортируем аски по возрастанию цены
                ask_prices = []
                for ask in asks:
                    if hasattr(ask, 'price'):
                        try:
                            price = float(ask.price)
                            ask_prices.append(price)
                        except (ValueError, TypeError):
                            continue
                if ask_prices:
                    return min(ask_prices)  # Самый низкий аск
        
        logger.warning(f"Не удалось определить текущую цену для токена {token_id}, side={side}")
        return None
        
    except Exception as e:
        logger.error(f"Ошибка при получении текущей цены для токена {token_id}: {e}")
        return None


def calculate_new_target_price(
    new_current_price: float,
    side: str,
    offset_ticks: int,
    tick_size: float = TICK_SIZE
) -> float:
    """
    Вычисляет новую целевую цену с использованием сохраненного offset_ticks.
    
    Использует ту же логику, что и при создании ордера.
    
    Args:
        new_current_price: Новая текущая цена рынка
        side: BUY или SELL
        offset_ticks: Отступ в тиках (из БД)
        tick_size: Размер тика (по умолчанию 0.001)
    
    Returns:
        Новая целевая цена
    """
    # Вычисляем целевую цену так же, как при создании ордера
    if side == "BUY":
        target = new_current_price - offset_ticks * tick_size
    else:  # SELL
        target = new_current_price + offset_ticks * tick_size
    
    # Ограничиваем диапазоном 0.001 - 0.999 (требования API)
    MIN_PRICE = 0.001
    MAX_PRICE = 0.999
    target = max(MIN_PRICE, min(MAX_PRICE, target))
    target = round(target, 3)
    
    # Проверяем, что после округления цена все еще в допустимом диапазоне
    if target < MIN_PRICE:
        target = MIN_PRICE
    elif target > MAX_PRICE:
        target = MAX_PRICE
    
    return target


async def process_user_orders(telegram_id: int) -> Tuple[List[str], List[Dict], List[Dict]]:
    """
    Обрабатывает ордера пользователя и возвращает списки для отмены и размещения.
    
    Args:
        telegram_id: ID пользователя в Telegram
    
    Returns:
        Tuple: (список order_id для отмены, список параметров новых ордеров, список уведомлений о смещении цены)
    """
    orders_to_cancel = []
    orders_to_place = []
    price_change_notifications = []  # Список уведомлений о смещении цены
    
    # Получаем данные пользователя
    user = await get_user(telegram_id)
    if not user:
        logger.warning(f"Пользователь {telegram_id} не найден в БД")
        return orders_to_cancel, orders_to_place, price_change_notifications
    
    # Создаем клиент
    try:
        client = create_client(user)
    except Exception as e:
        logger.error(f"Ошибка создания клиента для пользователя {telegram_id}: {e}")
        return orders_to_cancel, orders_to_place, price_change_notifications
    
    # Получаем активные ордера из БД
    db_orders = await get_user_orders(telegram_id, status="active")
    
    if not db_orders:
        logger.info(f"У пользователя {telegram_id} нет активных ордеров")
        return orders_to_cancel, orders_to_place, price_change_notifications
    
    logger.info(f"Обработка {len(db_orders)} активных ордеров для пользователя {telegram_id}")
    
    # Обрабатываем каждый ордер
    for db_order in db_orders:
        try:
            order_id = db_order.get("order_id")
            market_id = db_order.get("market_id")
            token_id = db_order.get("token_id")  # Используем token_id из БД
            token_name = db_order.get("token_name")  # YES или NO
            side = db_order.get("side")  # BUY или SELL
            current_price_at_creation = db_order.get("current_price", 0.0)
            target_price = db_order.get("target_price", 0.0)
            offset_ticks = db_order.get("offset_ticks", 0)
            amount = db_order.get("amount", 0.0)
            
            if not order_id or not market_id or not side or not token_id:
                logger.warning(f"Пропуск ордера с неполными данными: {order_id}")
                continue
            
            # Получаем текущую цену рынка
            new_current_price = get_current_market_price(client, token_id, side)
            if not new_current_price:
                logger.warning(f"Не удалось получить текущую цену для ордера {order_id}")
                continue
            
            # Вычисляем новую целевую цену с использованием сохраненного offset_ticks
            new_target_price = calculate_new_target_price(
                new_current_price,
                side,
                offset_ticks
            )
            
            # Проверяем, изменилась ли целевая цена
            # Если новая целевая цена равна старой (с учетом округления), нет смысла перемещать ордер
            target_price_change = abs(new_target_price - target_price)
            
            if target_price_change < TICK_SIZE:
                # Изменение целевой цены меньше одного тика, пропускаем ордер
                logger.info(
                    f"⏭️ Ордер {order_id} пропущен: изменение целевой цены недостаточно "
                    f"({target_price_change:.6f} < {TICK_SIZE}). "
                    f"Старая: {target_price}, Новая: {new_target_price}"
                )
                continue
            
            price_change = new_current_price - current_price_at_creation
            logger.info(f"Цена изменилась для ордера {order_id}:")
            logger.info(f"  Старая текущая цена: {current_price_at_creation}")
            logger.info(f"  Новая текущая цена: {new_current_price}")
            logger.info(f"  Изменение текущей цены: {price_change:+.6f}")
            logger.info(f"  Старая целевая цена: {target_price}")
            logger.info(f"  Новая целевая цена: {new_target_price}")
            logger.info(f"  Изменение целевой цены: {target_price_change:+.6f} (>= {TICK_SIZE})")
            logger.info(f"  Offset (ticks): {offset_ticks}")
            
            # Добавляем уведомление о смещении цены (независимо от успешности отмены/создания)
            price_change_notifications.append({
                "order_id": order_id,
                "market_id": market_id,
                "token_name": token_name,
                "side": side,
                "old_current_price": current_price_at_creation,
                "new_current_price": new_current_price,
                "old_target_price": target_price,
                "new_target_price": new_target_price,
                "price_change": price_change,
                "target_price_change": target_price_change,
                "offset_ticks": offset_ticks,
            })
            
            # Добавляем ордер в список для отмены
            orders_to_cancel.append(order_id)
            
            # Подготавливаем параметры нового ордера
            order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
            
            new_order_params = {
                "old_order_id": order_id,  # Старый order_id для обновления БД
                "market_id": market_id,
                "token_id": token_id,
                "token_name": token_name,  # Добавляем для уведомлений
                "side": order_side,
                "price": new_target_price,
                "amount": amount,
                "current_price_at_creation": new_current_price,  # Сохраняем для обновления БД
                "target_price": new_target_price,  # Сохраняем для обновления БД
            }
            
            orders_to_place.append(new_order_params)
            
        except Exception as e:
            logger.error(f"Ошибка при обработке ордера {db_order.get('order_id', 'unknown')}: {e}")
            continue
    
    return orders_to_cancel, orders_to_place, price_change_notifications


def cancel_orders_batch(client, order_ids: List[str]) -> List[Dict]:
    """
    Отменяет ордера батчем.
    
    Args:
        client: Клиент Opinion SDK
        order_ids: Список ID ордеров для отмены
    
    Returns:
        Список результатов отмены
    """
    try:
        results = client.cancel_orders_batch(order_ids)
        
        success_count = 0
        failed_count = 0
        
        for i, result in enumerate(results):
            if result.get('success', False):
                success_count += 1
                # Проверяем, есть ли дополнительная информация в результате
                result_data = result.get('result')
                if result_data:
                    if hasattr(result_data, 'errno'):
                        if result_data.errno == 0:
                            logger.info(f"Отменен ордер: {order_ids[i]}")
                        else:
                            logger.error(f"Ошибка при отмене ордера {order_ids[i]}: errno={result_data.errno}, errmsg={getattr(result_data, 'errmsg', 'N/A')}")
                            failed_count += 1
                            success_count -= 1
                    else:
                        logger.info(f"Отменен ордер: {order_ids[i]}")
                else:
                    logger.info(f"Отменен ордер: {order_ids[i]}")
            else:
                failed_count += 1
                error = result.get('error', 'Unknown error')
                logger.error(f"Не удалось отменить ордер {order_ids[i]}: {error}")
        
        logger.info(f"Отменено ордеров: {success_count}, ошибок: {failed_count}")
        return results
        
    except Exception as e:
        logger.error(f"Ошибка при batch отмене ордеров: {e}")
        return []


def place_orders_batch(client, orders_params: List[Dict]) -> List:
    """
    Размещает ордера батчем.
    
    Args:
        client: Клиент Opinion SDK
        orders_params: Список параметров ордеров
    
    Returns:
        Список результатов размещения
    """
    try:
        from opinion_clob_sdk.chain.py_order_utils.model.order_type import LIMIT_ORDER
        
        client.enable_trading()
        
        # Преобразуем параметры в PlaceOrderDataInput
        orders = []
        for params in orders_params:
            price_rounded = round(float(params["price"]), 3)
            
            # makerAmountInQuoteToken может быть int или float, не обязательно str
            amount_value = params["amount"]
            if isinstance(amount_value, str):
                amount_value = float(amount_value)
            
            order_input = PlaceOrderDataInput(
                marketId=params["market_id"],
                tokenId=params["token_id"],
                side=params["side"],
                orderType=LIMIT_ORDER,
                price=str(price_rounded),
                makerAmountInQuoteToken=amount_value  # int или float, не str
            )
            orders.append(order_input)
        
        # Размещаем ордера батчем
        results = client.place_orders_batch(orders, check_approval=False)
        
        success_count = 0
        failed_count = 0
        
        for i, result in enumerate(results):
            # Результаты batch методов возвращают словари с полями: success, result, error
            if result.get('success', False):
                success_count += 1
                # Структура из логов: result['result'].result.order_data.order_id
                # result['result'] - OpenapiOrderPost200Response с errno и result
                # result['result'].result - V2AddOrderResp с order_data
                # result['result'].result.order_data - V2OrderData с order_id
                order_id = 'unknown'
                try:
                    result_data = result.get('result')
                    if result_data and result_data.errno == 0:
                        order_id = result_data.result.order_data.order_id
                        logger.info(f"Размещен ордер: {order_id}")
                    else:
                        errmsg = getattr(result_data, 'errmsg', 'N/A') if result_data else 'No result_data'
                        logger.warning(f"Ошибка размещения ордера {i}: errno={getattr(result_data, 'errno', 'N/A')}, errmsg={errmsg}")
                except (AttributeError, TypeError) as e:
                    logger.error(f"Не удалось извлечь order_id из результата {i}: {e}")
            else:
                failed_count += 1
                error = result.get('error', 'Unknown error')
                logger.error(f"Не удалось разместить ордер {i}: {error}")
        
        logger.info(f"Размещено ордеров: {success_count}, ошибок: {failed_count}")
        return results
        
    except Exception as e:
        logger.error(f"Ошибка при batch размещении ордеров: {e}")
        import traceback
        traceback.print_exc()
        return []




async def send_price_change_notification(bot, telegram_id: int, notification: Dict):
    """Отправляет уведомление пользователю о смещении цены."""
    try:
        old_price_cents = notification["old_current_price"] * 100
        new_price_cents = notification["new_current_price"] * 100
        old_target_cents = notification["old_target_price"] * 100
        new_target_cents = notification["new_target_price"] * 100
        price_change_cents = notification["price_change"] * 100
        
        # Convert offset_ticks to cents
        offset_ticks = notification['offset_ticks']
        offset_cents = offset_ticks * TICK_SIZE * 100
        
        side_emoji = "📈" if notification["side"] == "BUY" else "📉"
        change_sign = "+" if notification["price_change"] > 0 else ""
        
        message = f"""🔔 <b>Price Change Detected</b>

{side_emoji} <b>{notification['token_name']} {notification['side']}</b>
📊 Market ID: {notification['market_id']}

💰 <b>Current Price:</b>
   Old: {old_price_cents:.2f}¢
   New: {new_price_cents:.2f}¢
   Change: {change_sign}{price_change_cents:.2f}¢

🎯 <b>Target Price:</b>
   Old: {old_target_cents:.2f}¢
   New: {new_target_cents:.2f}¢

⚙️ Offset: {offset_cents:.2f}¢

Order will be moved to maintain the offset.
You will notify about it."""
        
        await bot.send_message(chat_id=telegram_id, text=message)
        logger.info(f"Sent price change notification to user {telegram_id} for order {notification['order_id']}")
    except Exception as e:
        logger.error(f"Failed to send price change notification to user {telegram_id}: {e}")


async def send_order_updated_notification(bot, telegram_id: int, order_params: Dict, new_order_id: str):
    """Отправляет уведомление пользователю об успешном обновлении ордера в БД."""
    try:
        current_price_cents = order_params["current_price_at_creation"] * 100
        target_price_cents = order_params["target_price"] * 100
        
        side_emoji = "📈" if order_params.get("side") == OrderSide.BUY else "📉"
        side_text = "BUY" if order_params.get("side") == OrderSide.BUY else "SELL"
        
        message = f"""✅ <b>Order Updated Successfully</b>

{side_emoji} <b>{order_params.get('token_name', 'N/A')} {side_text}</b>
📊 Market ID: {order_params['market_id']}

🆔 <b>New Order ID:</b>
<code>{new_order_id}</code>

💰 <b>Current Price:</b> {current_price_cents:.2f}¢
🎯 <b>Target Price:</b> {target_price_cents:.2f}¢
💵 <b>Amount:</b> {order_params['amount']} USDT

Order has been successfully moved to maintain the offset."""
        
        await bot.send_message(chat_id=telegram_id, text=message)
        logger.info(f"Sent order updated notification to user {telegram_id} for order {new_order_id}")
    except Exception as e:
        logger.error(f"Failed to send order updated notification to user {telegram_id}: {e}")


async def async_sync_all_orders(bot):
    """
    Асинхронная функция синхронизации ордеров с уведомлениями пользователям.
    
    Args:
        bot: Экземпляр aiogram Bot для отправки уведомлений
    """
    logger.info("="*80)
    logger.info("Начало автоматического перемещения ордеров (async)")
    logger.info("="*80)
    
    # Получаем всех пользователей
    users = await get_all_users()
    logger.info(f"Найдено пользователей: {len(users)}")
    
    if not users:
        logger.warning("В базе данных нет пользователей")
        return
    
    # Общая статистика
    total_cancelled = 0
    total_placed = 0
    total_errors = 0
    
    # Обрабатываем ордера для каждого пользователя
    for telegram_id in users:
        logger.info(f"\n{'='*80}")
        logger.info(f"Обработка пользователя {telegram_id}")
        logger.info(f"{'='*80}")
        
        try:
            # Получаем списки ордеров для отмены и размещения, а также уведомления
            orders_to_cancel, orders_to_place, price_change_notifications = await process_user_orders(telegram_id)
            
            # Отправляем уведомления о смещении цены (независимо от успешности отмены/создания)
            for notification in price_change_notifications:
                await send_price_change_notification(bot, telegram_id, notification)
            
            if not orders_to_cancel and not orders_to_place:
                logger.info(f"Нет ордеров для перемещения у пользователя {telegram_id}")
                continue
            
            logger.info(f"Ордеров для отмены: {len(orders_to_cancel)}")
            logger.info(f"Ордеров для размещения: {len(orders_to_place)}")
            
            if not orders_to_cancel or not orders_to_place:
                logger.warning(f"Несоответствие: отмена={len(orders_to_cancel)}, размещение={len(orders_to_place)}")
                continue
            
            # Получаем клиент для пользователя
            user = await get_user(telegram_id)
            # create_client остается синхронным, но это быстрая операция
            client = create_client(user)
            
            # Отменяем старые ордера
            cancelled_count = 0
            if orders_to_cancel:
                # Обертываем синхронный вызов в asyncio.to_thread, чтобы не блокировать event loop
                cancel_results = await asyncio.to_thread(cancel_orders_batch, client, orders_to_cancel)
                
                # Проверяем успешность отмены более тщательно
                for i, result in enumerate(cancel_results):
                    order_id = orders_to_cancel[i]
                    is_success = False
                    
                    if result.get('success', False):
                        # Дополнительная проверка через result_data.errno
                        result_data = result.get('result')
                        if result_data and hasattr(result_data, 'errno'):
                            if result_data.errno == 0:
                                is_success = True
                            else:
                                logger.error(f"Ошибка при отмене ордера {order_id}: errno={result_data.errno}, errmsg={getattr(result_data, 'errmsg', 'N/A')}")
                        else:
                            # Если нет result_data, считаем успешным если success=True
                            is_success = True
                    
                    if is_success:
                        cancelled_count += 1
                
                total_cancelled += cancelled_count
                
                # Проверяем, что все ордера успешно отменены
                if cancelled_count != len(orders_to_cancel):
                    failed_count = len(orders_to_cancel) - cancelled_count
                    logger.error(f"Не удалось отменить {failed_count} из {len(orders_to_cancel)} ордеров")
                    logger.warning("Пропускаем размещение новых ордеров, так как не все старые были отменены")
                    continue
            
            # Размещаем новые ордера только если все старые успешно отменены
            if orders_to_place and cancelled_count == len(orders_to_cancel):
                # Обертываем синхронный вызов в asyncio.to_thread, чтобы не блокировать event loop
                place_results = await asyncio.to_thread(place_orders_batch, client, orders_to_place)
                # Подсчитываем успешно размещенные ордера (результаты - это словари с полем 'success')
                placed_count = len([r for r in place_results if isinstance(r, dict) and r.get('success', False)])
                total_placed += placed_count
                
                # Обновляем цены в БД для успешно размещенных ордеров и отправляем уведомления
                for i, result in enumerate(place_results):
                    if not result.get('success', False):
                        continue
                    
                    order_params = orders_to_place[i]
                    old_order_id = order_params.get("old_order_id")
                    
                    # Структура из логов: result['result'].result.order_data.order_id
                    try:
                        result_data = result.get('result')
                        if result_data and result_data.errno == 0:
                            new_order_id = result_data.result.order_data.order_id
                            
                            if new_order_id and old_order_id:
                                # Обновляем ордер в БД
                                await update_order_in_db(
                                    old_order_id,
                                    new_order_id,
                                    order_params["current_price_at_creation"],
                                    order_params["target_price"]
                                )
                                # Отправляем уведомление об успешном обновлении
                                await send_order_updated_notification(bot, telegram_id, order_params, new_order_id)
                    except (AttributeError, TypeError) as e:
                        logger.error(f"Не удалось извлечь order_id из результата размещения {i}: {e}")
            
        except Exception as e:
            logger.error(f"Ошибка при обработке пользователя {telegram_id}: {e}")
            total_errors += 1
            continue
    
    # Итоговая статистика
    logger.info(f"\n{'='*80}")
    logger.info("Итоговая статистика")
    logger.info(f"{'='*80}")
    logger.info(f"Отменено ордеров: {total_cancelled}")
    logger.info(f"Размещено ордеров: {total_placed}")
    logger.info(f"Ошибок: {total_errors}")
    logger.info("="*80)


def main():
    """Главная функция синхронизации."""
    logger.info("="*80)
    logger.info("Начало автоматического перемещения ордеров")
    logger.info("="*80)
    
    # Получаем всех пользователей
    users = get_all_users()
    logger.info(f"Найдено пользователей: {len(users)}")
    
    if not users:
        logger.warning("В базе данных нет пользователей")
        return
    
    # Общая статистика
    total_cancelled = 0
    total_placed = 0
    total_errors = 0
    
    # Обрабатываем ордера для каждого пользователя
    for telegram_id in users:
        logger.info(f"\n{'='*80}")
        logger.info(f"Обработка пользователя {telegram_id}")
        logger.info(f"{'='*80}")
        
        try:
            # Получаем списки ордеров для отмены и размещения
            orders_to_cancel, orders_to_place, price_change_notifications = process_user_orders(telegram_id)
            
            if not orders_to_cancel and not orders_to_place:
                logger.info(f"Нет ордеров для перемещения у пользователя {telegram_id}")
                continue
            
            logger.info(f"Ордеров для отмены: {len(orders_to_cancel)}")
            logger.info(f"Ордеров для размещения: {len(orders_to_place)}")
            
            if not orders_to_cancel or not orders_to_place:
                logger.warning(f"Несоответствие: отмена={len(orders_to_cancel)}, размещение={len(orders_to_place)}")
                continue
            
            # Получаем клиент для пользователя
            user = get_user(telegram_id)
            client = create_client(user)
            
            # Отменяем старые ордера
            cancelled_count = 0
            if orders_to_cancel:
                cancel_results = cancel_orders_batch(client, orders_to_cancel)
                
                # Проверяем успешность отмены более тщательно
                for i, result in enumerate(cancel_results):
                    order_id = orders_to_cancel[i]
                    is_success = False
                    
                    if result.get('success', False):
                        # Дополнительная проверка через result_data.errno
                        result_data = result.get('result')
                        if result_data and hasattr(result_data, 'errno'):
                            if result_data.errno == 0:
                                is_success = True
                            else:
                                logger.error(f"Ошибка при отмене ордера {order_id}: errno={result_data.errno}, errmsg={getattr(result_data, 'errmsg', 'N/A')}")
                        else:
                            # Если нет result_data, считаем успешным если success=True
                            is_success = True
                    
                    if is_success:
                        cancelled_count += 1
                
                total_cancelled += cancelled_count
                
                # Проверяем, что все ордера успешно отменены
                if cancelled_count != len(orders_to_cancel):
                    failed_count = len(orders_to_cancel) - cancelled_count
                    logger.error(f"Не удалось отменить {failed_count} из {len(orders_to_cancel)} ордеров")
                    logger.warning("Пропускаем размещение новых ордеров, так как не все старые были отменены")
                    continue
            
            # Размещаем новые ордера только если все старые успешно отменены
            if orders_to_place and cancelled_count == len(orders_to_cancel):
                # Синхронный вызов (эта функция используется только для отдельного запуска скрипта)
                place_results = place_orders_batch(client, orders_to_place)
                # Подсчитываем успешно размещенные ордера (результаты - это словари с полем 'success')
                total_placed += len([r for r in place_results if isinstance(r, dict) and r.get('success', False)])
                
                # Обновляем цены в БД для успешно размещенных ордеров
                for i, result in enumerate(place_results):
                    if not result.get('success', False):
                        continue
                    
                    order_params = orders_to_place[i]
                    old_order_id = order_params.get("old_order_id")
                    
                    # Структура из логов: result['result'].result.order_data.order_id
                    try:
                        result_data = result.get('result')
                        if result_data and result_data.errno == 0:
                            new_order_id = result_data.result.order_data.order_id
                            
                            if new_order_id and old_order_id:
                                # Синхронный вызов (эта функция используется только для отдельного запуска скрипта)
                                update_order_in_db(
                                    old_order_id,
                                    new_order_id,
                                    order_params["current_price_at_creation"],
                                    order_params["target_price"]
                                )
                    except (AttributeError, TypeError) as e:
                        logger.error(f"Не удалось извлечь order_id из результата размещения {i}: {e}")
            
        except Exception as e:
            logger.error(f"Ошибка при обработке пользователя {telegram_id}: {e}")
            total_errors += 1
            continue
    
    # Итоговая статистика
    logger.info(f"\n{'='*80}")
    logger.info("Итоговая статистика")
    logger.info(f"{'='*80}")
    logger.info(f"Отменено ордеров: {total_cancelled}")
    logger.info(f"Размещено ордеров: {total_placed}")
    logger.info(f"Ошибок: {total_errors}")
    logger.info("="*80)


if __name__ == "__main__":
    main()
