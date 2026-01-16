"""
Модуль для автоматической отмены старых активных ордеров.

Проверяет все активные ордера и отменяет те, которые старше 5 дней,
отправляя пользователям уведомления.
"""

import logging
from datetime import datetime, timedelta
from typing import List

from aiogram import Bot
from client_factory import create_client
from database import get_user, update_order_status

logger = logging.getLogger(__name__)

# Количество дней, после которых ордер считается старым
ORDER_EXPIRY_DAYS = 5


async def get_old_active_orders(days: int = ORDER_EXPIRY_DAYS) -> List[dict]:
    """
    Получает все активные ордера старше указанного количества дней.

    Args:
        days: Количество дней (по умолчанию 5)

    Returns:
        Список словарей с данными ордеров
    """
    import aiosqlite
    from database import DB_PATH

    columns = [
        "id",
        "telegram_id",
        "order_id",
        "market_id",
        "market_title",
        "token_id",
        "token_name",
        "side",
        "current_price",
        "target_price",
        "offset_ticks",
        "offset_cents",
        "amount",
        "status",
        "reposition_threshold_cents",
        "created_at",
    ]

    # Вычисляем дату, до которой ордера считаются старыми
    # SQLite использует формат 'YYYY-MM-DD HH:MM:SS' для TIMESTAMP
    cutoff_date = datetime.now() - timedelta(days=days)
    cutoff_date_str = cutoff_date.strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            f"""
            SELECT {", ".join(columns)} FROM orders 
            WHERE status = 'pending' AND datetime(created_at) < datetime(?)
            ORDER BY created_at ASC
        """,
            (cutoff_date_str,),
        ) as cursor:
            rows = await cursor.fetchall()

    orders = []
    for row in rows:
        order_dict = dict(zip(columns, row))
        orders.append(order_dict)

    return orders


async def cancel_old_order(bot: Bot, order: dict) -> bool:
    """
    Отменяет старый ордер и отправляет уведомление пользователю.

    Args:
        bot: Экземпляр aiogram Bot для отправки сообщений
        order: Словарь с данными ордера

    Returns:
        True если ордер успешно отменен, False в противном случае
    """
    telegram_id = order["telegram_id"]
    order_id = order["order_id"]
    market_id = order["market_id"]
    token_name = order["token_name"]
    side = order["side"]

    try:
        # Получаем данные пользователя
        user = await get_user(telegram_id)
        if not user:
            logger.warning(
                f"Пользователь {telegram_id} не найден для ордера {order_id}"
            )
            return False

        # Создаем клиент
        client = create_client(user)

        # Отменяем ордер через API
        result = client.cancel_order(order_id=order_id)

        if result.errno == 0:
            # Обновляем статус в БД
            await update_order_status(order_id, "canceled")
            logger.info(
                f"Отменен старый ордер {order_id} для пользователя {telegram_id} (старше {ORDER_EXPIRY_DAYS} дней)"
            )

            # Отправляем уведомление пользователю
            try:
                message = f"""⏹️ <b>Order Expired and Cancelled</b>

Your order has been automatically cancelled because it was active for more than {ORDER_EXPIRY_DAYS} days.

<b>Order Details:</b>
• Order ID: <code>{order_id}</code>
• Market ID: {market_id}
• Token: {token_name} {side}

Currently, active orders can be kept for no longer than {ORDER_EXPIRY_DAYS} days. Please follow updates for changes to this policy.

You can create a new order using the /make_market command."""

                await bot.send_message(chat_id=telegram_id, text=message)
                logger.info(
                    f"Отправлено уведомление пользователю {telegram_id} об отмене ордера {order_id}"
                )
            except Exception as e:
                logger.error(
                    f"Ошибка при отправке уведомления пользователю {telegram_id} об ордере {order_id}: {e}"
                )

            return True
        else:
            errmsg = getattr(result, "errmsg", "Unknown error")
            logger.error(
                f"Не удалось отменить ордер {order_id} для пользователя {telegram_id}: errno={result.errno}, errmsg={errmsg}"
            )
            return False

    except Exception as e:
        logger.error(
            f"Ошибка при отмене старого ордера {order_id} для пользователя {telegram_id}: {e}"
        )
        return False


async def expire_old_orders(bot: Bot) -> dict:
    """
    Проверяет и отменяет все активные ордера старше ORDER_EXPIRY_DAYS дней.

    Args:
        bot: Экземпляр aiogram Bot для отправки сообщений

    Returns:
        Словарь со статистикой: {"checked": int, "expired": int, "failed": int}
    """
    logger.info("=" * 80)
    logger.info("Начало проверки старых ордеров")
    logger.info("=" * 80)

    try:
        # Получаем все старые активные ордера
        old_orders = await get_old_active_orders(ORDER_EXPIRY_DAYS)

        if not old_orders:
            logger.info(
                f"Старых активных ордеров (старше {ORDER_EXPIRY_DAYS} дней) не найдено"
            )
            logger.info("=" * 80)
            return {"checked": 0, "expired": 0, "failed": 0}

        logger.info(f"Найдено {len(old_orders)} старых активных ордеров для отмены")

        expired_count = 0
        failed_count = 0

        # Обрабатываем каждый ордер
        for order in old_orders:
            order_id = order["order_id"]
            telegram_id = order["telegram_id"]
            created_at = order["created_at"]

            # Вычисляем возраст ордера
            try:
                if isinstance(created_at, str):
                    created_date = datetime.fromisoformat(created_at)
                else:
                    created_date = created_at
                age_days = (datetime.now() - created_date).days
            except Exception as e:
                logger.warning(f"Ошибка при вычислении возраста ордера {order_id}: {e}")
                age_days = ORDER_EXPIRY_DAYS

            logger.info(
                f"Обработка ордера {order_id} (User: {telegram_id}, Market: {order['market_id']}, Age: {age_days} days)"
            )

            # Отменяем ордер
            if await cancel_old_order(bot, order):
                expired_count += 1
            else:
                failed_count += 1

        logger.info("=" * 80)
        logger.info(
            f"Проверка старых ордеров завершена: проверено {len(old_orders)}, отменено {expired_count}, ошибок {failed_count}"
        )
        logger.info("=" * 80)

        return {
            "checked": len(old_orders),
            "expired": expired_count,
            "failed": failed_count,
        }

    except Exception as e:
        logger.error(f"Ошибка при проверке старых ордеров: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return {"checked": 0, "expired": 0, "failed": 0}
