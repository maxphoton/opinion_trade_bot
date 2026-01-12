"""Диалог для просмотра ордеров пользователя."""

import logging

from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Back, Button, Group
from aiogram_dialog.widgets.text import Const, Format
from client_factory import create_client
from database import get_order_by_id, get_user, get_user_orders, update_order_status

logger = logging.getLogger(__name__)


# Состояния для диалога ордеров
class OrdersSG(StatesGroup):
    """Состояния диалога ордеров."""

    orders_list = State()
    orders_search = State()
    orders_search_results = State()


# Обработчики для списка ордеров
async def get_orders_list_data(dialog_manager: DialogManager, **kwargs):
    """Данные для списка ордеров с пагинацией."""
    # Получаем telegram_id из start_data (передается при запуске диалога)
    telegram_id = (
        dialog_manager.start_data.get("telegram_id")
        if dialog_manager.start_data
        else None
    )

    # Если нет в start_data, получаем из события (fallback)
    if not telegram_id:
        event = dialog_manager.event
        # В aiogram все события имеют from_user напрямую или через callback_query
        if hasattr(event, "from_user") and event.from_user:
            telegram_id = event.from_user.id
        elif (
            hasattr(event, "callback_query")
            and event.callback_query
            and event.callback_query.from_user
        ):
            telegram_id = event.callback_query.from_user.id

    if not telegram_id:
        return {
            "list_text": "❌ Failed to determine user",
            "current_page": 0,
            "total_pages": 0,
            "has_prev": False,
            "has_next": False,
        }

    # Получаем все ордера пользователя
    all_orders = await get_user_orders(telegram_id)
    total = len(all_orders)

    # Проверяем, есть ли pending ордера (не отмененные и не исполненные)
    has_active_orders = any(order.get("status") == "pending" for order in all_orders)

    # Получаем текущую страницу из dialog_data (по умолчанию 0)
    current_page = dialog_manager.dialog_data.get("orders_list_page", 0)
    items_per_page = 10

    # Вычисляем индексы для текущей страницы
    start_idx = current_page * items_per_page
    end_idx = start_idx + items_per_page
    orders_page = all_orders[start_idx:end_idx]

    # Формируем текст
    text = f"""📋 <b>My Orders (created in bot)</b>

Total orders: {total}
Page {current_page + 1} of {(total + items_per_page - 1) // items_per_page if total > 0 else 1}

"""

    if not orders_page:
        text += "You have no orders yet."
    else:
        for i, order in enumerate(orders_page, start_idx + 1):
            order_id = order.get("order_id", "N/A")
            market_id = order.get("market_id", "N/A")
            market_title = order.get("market_title", "N/A")
            token_name = order.get("token_name", "N/A")
            side = order.get("side", "N/A")
            target_price = order.get("target_price", 0)
            amount = order.get("amount", 0)
            status = order.get("status", "unknown")
            # Нормализуем статус: приводим к нижнему регистру и убираем пробелы
            status = str(status).lower().strip() if status else "unknown"
            reposition_threshold_cents = float(order.get("reposition_threshold_cents"))

            created_at = order.get("created_at")
            # SQLite возвращает TIMESTAMP как строку в формате "YYYY-MM-DD HH:MM:SS"
            # Берем первые 16 символов для формата "YYYY-MM-DD HH:MM"
            date_str = (
                created_at[:16] if created_at and len(str(created_at)) >= 16 else "N/A"
            )

            # Статус с эмодзи
            status_emoji = {"pending": "⏳", "canceled": "🔴", "finished": "✅"}.get(
                status, "❓"
            )

            # Направление с эмодзи
            side_emoji = "📈" if side == "BUY" else "📉"

            # Форматируем цену в центах
            target_price_cents = target_price * 100
            price_str = f"{target_price_cents:.2f}".rstrip("0").rstrip(".")

            text += f"""<b>{i}.</b> {status_emoji} {status.upper()} <code>{order_id}</code>
   {side_emoji} {side} {token_name} | {price_str}¢ | {amount} USDT
   📊 Market ID: {market_id} | {market_title[:25]}...
   ⚙️ Reposition threshold: {reposition_threshold_cents:.2f}¢
   📅 {date_str}

"""

    return {
        "list_text": text,
        "current_page": current_page,
        "total_pages": (total + items_per_page - 1) // items_per_page
        if total > 0
        else 1,
        "has_prev": current_page > 0,
        "has_next": end_idx < total,
        "has_active_orders": has_active_orders,
    }


async def on_orders_list_prev(
    callback: CallbackQuery, button: Button, manager: DialogManager
):
    """Переход на предыдущую страницу."""
    current_page = manager.dialog_data.get("orders_list_page", 0)
    if current_page > 0:
        manager.dialog_data["orders_list_page"] = current_page - 1
    await manager.switch_to(OrdersSG.orders_list)
    await callback.answer()


async def on_orders_list_next(
    callback: CallbackQuery, button: Button, manager: DialogManager
):
    """Переход на следующую страницу."""
    current_page = manager.dialog_data.get("orders_list_page", 0)
    manager.dialog_data["orders_list_page"] = current_page + 1
    await manager.switch_to(OrdersSG.orders_list)
    await callback.answer()


async def on_orders_search(
    callback: CallbackQuery, button: Button, manager: DialogManager
):
    """Переход к поиску ордеров."""
    await manager.switch_to(OrdersSG.orders_search)
    await callback.answer()


async def on_cancel_order(
    callback: CallbackQuery, button: Button, manager: DialogManager
):
    """Активация/деактивация режима отмены ордера - отправляет инструкцию, список остается видимым."""
    cancel_mode = manager.dialog_data.get("cancel_mode", False)

    if cancel_mode:
        # Отключаем режим отмены
        manager.dialog_data["cancel_mode"] = False
        await callback.message.answer("✅ Cancel mode disabled. List remains visible.")
    else:
        # Включаем режим отмены
        manager.dialog_data["cancel_mode"] = True
        await callback.message.answer(
            """❌ <b>Cancel Order Mode</b>

Enter the order ID to cancel (you can copy it from the list above).

To exit cancel mode, press the Cancel Order button again."""
        )

    await callback.answer()


async def on_exit(callback: CallbackQuery, button: Button, manager: DialogManager):
    """Обработчик кнопки Exit - отправляет сообщение и закрывает диалог."""
    await callback.message.answer(
        """Use the /make_market command to start a new farm.
Use the /orders command to manage your orders.
Use the /help command to view instructions.
Use the /support command to contact administrator."""
    )
    await manager.done()
    await callback.answer()


# Обработчик ввода order_id в режиме отмены
async def cancel_order_input_handler(
    message: Message, message_input: MessageInput, manager: DialogManager
):
    """Обработчик ввода order_id для отмены ордера (работает в окне списка)."""
    # Проверяем, активен ли режим отмены
    if not manager.dialog_data.get("cancel_mode", False):
        # Если режим отмены не активен, игнорируем сообщение
        return

    order_id = message.text.strip()

    if not order_id:
        await message.answer("❌ Please enter order ID.")
        return

    # Получаем telegram_id напрямую из сообщения
    telegram_id = message.from_user.id

    # Проверяем, что ордер существует и принадлежит пользователю
    order = await get_order_by_id(order_id)
    if not order:
        await message.answer(f"❌ Order <code>{order_id}</code> not found in database.")
        manager.dialog_data["cancel_mode"] = False
        return

    # Проверяем, что пользователь является владельцем ордера
    if order.get("telegram_id") != telegram_id:
        await message.answer(
            "❌ You don't have permission to cancel this order. The order belongs to another user."
        )
        logger.warning(
            f"User {telegram_id} attempted to cancel another user's order {order_id} (owner: {order.get('telegram_id')})"
        )
        manager.dialog_data["cancel_mode"] = False
        return

    # Получаем данные пользователя для создания клиента
    user = await get_user(telegram_id)
    if not user:
        await message.answer("❌ User not found in database.")
        manager.dialog_data["cancel_mode"] = False
        return

    # Создаем клиент
    client = create_client(user)

    try:
        # Отменяем ордер
        result = client.cancel_order(order_id=order_id)

        if result.errno == 0:
            # Обновляем статус в БД
            await update_order_status(order_id, "canceled")
            await message.answer(
                f"✅ Order <code>{order_id}</code> successfully cancelled."
            )
            logger.info(f"User {telegram_id} cancelled order {order_id}")
        else:
            # Получаем текст ошибки, если доступен
            errmsg = getattr(result, "errmsg", "Unknown error")
            error_message = f"❌ Failed to cancel order <code>{order_id}</code>.\n\nError code: {result.errno}\nError message: {errmsg}"
            await message.answer(error_message)
            logger.warning(
                f"Failed to cancel order {order_id} for user {telegram_id}: errno={result.errno}, errmsg={errmsg}"
            )
    except Exception as e:
        await message.answer(
            f"❌ Error cancelling order <code>{order_id}</code>: {str(e)}"
        )
        logger.error(f"Error cancelling order {order_id} for user {telegram_id}: {e}")

    # Отключаем режим отмены
    manager.dialog_data["cancel_mode"] = False
    # Обновляем окно списка
    await manager.switch_to(OrdersSG.orders_list)


# Окно списка ордеров
orders_list_window = Window(
    Format("{list_text}"),
    Group(
        Button(
            Const("◀️ Back"),
            id="prev_page",
            on_click=on_orders_list_prev,
            when="has_prev",
        ),
        Button(
            Const("Next ▶️"),
            id="next_page",
            on_click=on_orders_list_next,
            when="has_next",
        ),
    ),
    Group(
        Button(Const("🔍 Search"), id="search", on_click=on_orders_search),
        Button(
            Const("❌ Cancel Order"),
            id="cancel_order",
            on_click=on_cancel_order,
            when="has_active_orders",
        ),
    ),
    Button(Const("🚪 Exit"), id="exit", on_click=on_exit),
    MessageInput(cancel_order_input_handler),
    state=OrdersSG.orders_list,
    getter=get_orders_list_data,
)


# Поиск ордеров
async def orders_search_handler(
    message: Message, message_input: MessageInput, manager: DialogManager
):
    """Обработчик ввода запроса поиска."""
    query = message.text.strip().lower()

    if not query:
        await message.answer("❌ Please enter a search query.")
        return

    # Получаем telegram_id пользователя
    telegram_id = message.from_user.id

    # Получаем все ордера пользователя
    all_orders = await get_user_orders(telegram_id)

    # Выполняем поиск
    search_results = []
    for order in all_orders:
        # Поиск по order_id, market_id, token_name, side
        order_id = str(order.get("order_id", "")).lower()
        market_id = str(order.get("market_id", "")).lower()
        market_title = str(order.get("market_title", "")).lower()
        token_name = str(order.get("token_name", "")).lower()
        side = str(order.get("side", "")).lower()

        if (
            query in order_id
            or query in market_id
            or query in market_title
            or query in token_name
            or query in side
        ):
            search_results.append(order)

    if not search_results:
        await message.answer(f"❌ No results found for query '{query}'.")
        await manager.switch_to(OrdersSG.orders_list)
        return

    # Сохраняем результаты поиска
    manager.dialog_data["search_query"] = query
    manager.dialog_data["search_results"] = search_results
    manager.dialog_data["search_results_page"] = 0

    await manager.switch_to(OrdersSG.orders_search_results)
    await message.answer(f"✅ Found orders: {len(search_results)}")


async def get_search_results_data(dialog_manager: DialogManager, **kwargs):
    """Данные для результатов поиска с пагинацией."""
    search_results = dialog_manager.dialog_data.get("search_results", [])
    search_query = dialog_manager.dialog_data.get("search_query", "")
    total = len(search_results)

    # Получаем текущую страницу из dialog_data (по умолчанию 0)
    current_page = dialog_manager.dialog_data.get("search_results_page", 0)
    items_per_page = 10

    # Вычисляем индексы для текущей страницы
    start_idx = current_page * items_per_page
    end_idx = start_idx + items_per_page
    orders_page = search_results[start_idx:end_idx]

    # Формируем текст
    text = f"""🔍 <b>Search Results:</b> '{search_query}'

Found: {total}
Page {current_page + 1} of {(total + items_per_page - 1) // items_per_page if total > 0 else 1}

"""

    for i, order in enumerate(orders_page, start_idx + 1):
        order_id = order.get("order_id", "N/A")
        market_id = order.get("market_id", "N/A")
        market_title = order.get("market_title", "N/A")
        token_name = order.get("token_name", "N/A")
        side = order.get("side", "N/A")
        target_price = order.get("target_price", 0)
        amount = order.get("amount", 0)
        status = order.get("status", "unknown")
        # Нормализуем статус: приводим к нижнему регистру и убираем пробелы
        status = str(status).lower().strip() if status else "unknown"
        reposition_threshold_cents = float(order.get("reposition_threshold_cents"))

        created_at = order.get("created_at")
        # SQLite возвращает TIMESTAMP как строку в формате "YYYY-MM-DD HH:MM:SS"
        # Берем первые 16 символов для формата "YYYY-MM-DD HH:MM"
        date_str = (
            created_at[:16] if created_at and len(str(created_at)) >= 16 else "N/A"
        )

        # Статус с эмодзи
        status_emoji = {"pending": "⏳", "canceled": "🔴", "finished": "✅"}.get(
            status, "❓"
        )

        # Направление с эмодзи
        side_emoji = "📈" if side == "BUY" else "📉"

        # Форматируем цену в центах
        target_price_cents = target_price * 100
        price_str = f"{target_price_cents:.2f}".rstrip("0").rstrip(".")

        text += f"""<b>{i}.</b> {status_emoji} {status.upper()} <code>{order_id}</code>
   {side_emoji} {side} {token_name} | {price_str}¢ | {amount} USDT
   📊 Market ID: {market_id} | {market_title[:25]}...
   ⚙️ Reposition threshold: {reposition_threshold_cents:.2f}¢
   📅 {date_str}

"""

    return {
        "list_text": text,
        "current_page": current_page,
        "total_pages": (total + items_per_page - 1) // items_per_page
        if total > 0
        else 1,
        "has_prev": current_page > 0,
        "has_next": end_idx < total,
        "search_query": search_query,
    }


async def on_search_results_prev(
    callback: CallbackQuery, button: Button, manager: DialogManager
):
    """Переход на предыдущую страницу результатов поиска."""
    current_page = manager.dialog_data.get("search_results_page", 0)
    if current_page > 0:
        manager.dialog_data["search_results_page"] = current_page - 1
    await manager.switch_to(OrdersSG.orders_search_results)
    await callback.answer()


async def on_search_results_next(
    callback: CallbackQuery, button: Button, manager: DialogManager
):
    """Переход на следующую страницу результатов поиска."""
    current_page = manager.dialog_data.get("search_results_page", 0)
    manager.dialog_data["search_results_page"] = current_page + 1
    await manager.switch_to(OrdersSG.orders_search_results)
    await callback.answer()


async def on_search_results_back(
    callback: CallbackQuery, button: Button, manager: DialogManager
):
    """Возврат к списку ордеров."""
    manager.dialog_data["search_results_page"] = 0
    manager.dialog_data.pop("search_query", None)
    manager.dialog_data.pop("search_results", None)
    # Сбрасываем страницу списка
    manager.dialog_data["orders_list_page"] = 0
    await manager.switch_to(OrdersSG.orders_list)
    await callback.answer()


orders_search_window = Window(
    Const("Enter search query:\n(order_id, market_id, market_title, token_name, side)"),
    MessageInput(orders_search_handler),
    Group(
        Back(Const("◀️ Back")),
        Button(Const("🚪 Exit"), id="exit", on_click=on_exit),
    ),
    state=OrdersSG.orders_search,
)


orders_search_results_window = Window(
    Format("{list_text}"),
    Group(
        Button(
            Const("◀️ Back"),
            id="prev_page",
            on_click=on_search_results_prev,
            when="has_prev",
        ),
        Button(
            Const("Next ▶️"),
            id="next_page",
            on_click=on_search_results_next,
            when="has_next",
        ),
    ),
    Group(
        Back(Const("◀️ Back to list")),
        Button(Const("🚪 Exit"), id="exit", on_click=on_exit),
    ),
    state=OrdersSG.orders_search_results,
    getter=get_search_results_data,
)


# Создаем диалог
orders_dialog = Dialog(
    orders_list_window, orders_search_window, orders_search_results_window
)
