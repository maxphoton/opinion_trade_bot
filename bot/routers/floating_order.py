"""
Router for market order placement flow (/floating_order command).
Handles the complete order placement process from URL input to order confirmation.
"""

import hashlib
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from opinion.client_factory import create_client
from opinion.helper import calculate_target_price, get_market_url, get_offset_bounds
from opinion.opinion_api_wrapper import (
    calculate_spread_and_liquidity,
    check_usdt_balance,
    get_categorical_market_submarkets,
    get_market_info,
    get_orderbooks,
    parse_market_url,
    place_limit_order,
)
from opinion.websocket_sync import get_websocket_sync
from opinion_clob_sdk import Client
from opinion_clob_sdk.chain.py_order_utils.model.sides import OrderSide
from service.config import TICK_SIZE
from service.database import (
    get_opinion_account,
    get_user,
    get_user_accounts,
    save_order,
)

logger = logging.getLogger(__name__)

# ============================================================================
# States for market order placement
# ============================================================================


class MarketOrderStates(StatesGroup):
    """States for the order placement process."""

    waiting_account_selection = State()  # Выбор аккаунта (первый шаг)
    waiting_url = State()
    waiting_submarket = State()  # For submarket selection in categorical markets
    waiting_side = State()
    waiting_direction = State()
    waiting_amount = State()
    waiting_offset_ticks = State()
    waiting_reposition_threshold = State()  # Порог отклонения для перестановки ордера
    waiting_confirm = State()


# ============================================================================
# Router and handlers
# ============================================================================

market_router = Router()


@market_router.message(Command("floating_order"))
async def cmd_floating_order(message: Message, state: FSMContext):
    """Handler for /floating_order command - start of order placement process."""
    logger.info(f"Команда /floating_order от пользователя {message.from_user.id}")
    telegram_id = message.from_user.id
    user = await get_user(telegram_id)

    if not user:
        await message.answer(
            """❌ You are not registered. Use the /start to register."""
        )
        return

    # Получаем все аккаунты пользователя
    accounts = await get_user_accounts(telegram_id)
    if not accounts:
        await message.answer(
            """❌ You don't have any Opinion profiles yet.

Use /add_profile to add your first Opinion profile."""
        )
        return

    # Если аккаунт один, используем его автоматически
    if len(accounts) == 1:
        account_id = accounts[0]["account_id"]
        await state.update_data(account_id=account_id)
        # Переходим к следующему шагу - ввод URL
        builder = InlineKeyboardBuilder()
        builder.button(text="✖️ Cancel", callback_data="cancel")
        await message.answer(
            """📊 Place a Limit Order

Please enter the <a href="https://app.opinion.trade?code=BJea79">Opinion.trade</a> market link:""",
            reply_markup=builder.as_markup(),
            disable_web_page_preview=True,
        )
        await state.set_state(MarketOrderStates.waiting_url)
        return

    # Если аккаунтов несколько, показываем выбор
    builder = InlineKeyboardBuilder()
    for account in accounts:
        wallet = account["wallet_address"]
        account_id = account["account_id"]
        builder.button(
            text=f"Account {account_id} ({wallet[:8]}...)",
            callback_data=f"select_account_{account_id}",
        )
    builder.button(text="✖️ Cancel", callback_data="cancel")
    builder.adjust(1)

    await message.answer(
        """📊 Place a Limit Order

Select an account to use:""",
        reply_markup=builder.as_markup(),
    )
    await state.set_state(MarketOrderStates.waiting_account_selection)


@market_router.callback_query(F.data.startswith("select_account_"))
async def process_account_selection(callback: CallbackQuery, state: FSMContext):
    """Handles account selection."""
    account_id_str = callback.data.replace("select_account_", "")
    try:
        account_id = int(account_id_str)
    except ValueError:
        await callback.answer("Invalid account ID", show_alert=True)
        return

    # Сохраняем account_id в state
    await state.update_data(account_id=account_id)

    # Переходим к следующему шагу - ввод URL
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Cancel", callback_data="cancel")
    await callback.message.edit_text(
        """📊 Place a Limit Order

Please enter the <a href="https://app.opinion.trade?code=BJea79">Opinion.trade</a> market link:""",
        reply_markup=builder.as_markup(),
        disable_web_page_preview=True,
    )
    await state.set_state(MarketOrderStates.waiting_url)
    await callback.answer()


@market_router.message(MarketOrderStates.waiting_url)
async def process_market_url(message: Message, state: FSMContext):
    """Handles market URL input."""
    url = message.text.strip()
    market_id, market_type = parse_market_url(url)

    if not market_id:
        builder = InlineKeyboardBuilder()
        builder.button(text="✖️ Cancel", callback_data="cancel")
        await message.answer(
            """❌ Failed to extract Market ID from URL. Please try again:""",
            reply_markup=builder.as_markup(),
        )
        return

    is_categorical = market_type == "multi"

    # Получаем account_id из state
    data = await state.get_data()
    account_id = data.get("account_id")
    if not account_id:
        await message.answer(
            """❌ Account not selected. Please start again with /floating_order."""
        )
        await state.clear()
        return

    # Получаем данные аккаунта
    account = await get_opinion_account(account_id)
    if not account:
        await message.answer(
            """❌ Account not found. Please start again with /floating_order."""
        )
        await state.clear()
        return

    # Создаем клиент с обработкой ошибок
    try:
        client = create_client(account)
    except Exception as e:
        # Генерируем код ошибки для сопоставления с логами
        error_str = str(e)
        error_hash = hashlib.md5(error_str.encode()).hexdigest()[:8].upper()
        error_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        await message.answer(
            f"""❌ Failed to create API client.

Error code: <code>{error_hash}</code>
Time: {error_time}

Please contact administrator via /support and provide the error code above."""
        )
        await state.clear()
        logger.error(
            f"Ошибка создания клиента для пользователя {message.from_user.id} [CODE: {error_hash}] [TIME: {error_time}]: {e}"
        )
        return

    # Get market information
    await message.answer("""📊 Getting market information...""")
    market = await get_market_info(client, market_id, is_categorical)

    if not market:
        await message.answer(
            """❌ Failed to get market information. Please check the URL."""
        )
        await state.clear()
        return

    # Show market name after successful retrieval
    market_title = getattr(market, "market_title", "Unknown Market")
    await message.answer(f"""✅ Market found: <b>{market_title}</b>""")

    # If this is a categorical market, need to select submarket
    if is_categorical:
        submarkets = get_categorical_market_submarkets(market)

        if not submarkets:
            await message.answer(
                """❌ Failed to find submarkets in the categorical market"""
            )
            await state.clear()
            return

        # Build submarket list for selection
        submarket_list = []
        for i, subm in enumerate(submarkets, 1):
            submarket_id = getattr(subm, "market_id", getattr(subm, "id", None))
            title = getattr(
                subm,
                "market_title",
                getattr(subm, "title", getattr(subm, "name", f"Submarket {i}")),
            )
            submarket_list.append({"id": submarket_id, "title": title, "data": subm})

        # Save submarket list, client and root_market_id to state
        # Для categorical markets market_id - это root market ID
        await state.update_data(
            submarkets=submarket_list, client=client, root_market_id=market_id
        )

        # Create keyboard for submarket selection
        builder = InlineKeyboardBuilder()
        for i, subm in enumerate(submarket_list, 1):
            builder.button(text=f"{subm['title'][:30]}", callback_data=f"submarket_{i}")
        builder.button(text="✖️ Cancel", callback_data="cancel")
        builder.adjust(1)

        await message.answer(
            f"""📋 <b>Categorical Market</b>

Found submarkets: {len(submarket_list)}

Select a submarket:""",
            reply_markup=builder.as_markup(),
        )
        await state.set_state(MarketOrderStates.waiting_submarket)
        return

    # For regular market continue as usual
    # Get order books
    yes_token_id = getattr(market, "yes_token_id", None)
    no_token_id = getattr(market, "no_token_id", None)

    if not yes_token_id or not no_token_id:
        await message.answer("""❌ Failed to determine market tokens""")
        await state.clear()
        return

    # Save client to state
    await state.update_data(client=client)

    # Continue processing regular market
    await process_market_data(
        message, state, market, market_id, client, yes_token_id, no_token_id
    )


async def process_market_data(
    message: Message,
    state: FSMContext,
    market,
    market_id: int,
    client: Client,
    yes_token_id: str,
    no_token_id: str,
):
    """Processes market data and continues order placement process."""
    yes_orderbook, no_orderbook = await get_orderbooks(
        client, yes_token_id, no_token_id
    )

    # Check if order books have orders
    yes_has_orders = (
        yes_orderbook
        and hasattr(yes_orderbook, "bids")
        and hasattr(yes_orderbook, "asks")
        and (len(yes_orderbook.bids) > 0 or len(yes_orderbook.asks) > 0)
    )
    no_has_orders = (
        no_orderbook
        and hasattr(no_orderbook, "bids")
        and hasattr(no_orderbook, "asks")
        and (len(no_orderbook.bids) > 0 or len(no_orderbook.asks) > 0)
    )

    if not yes_has_orders and not no_has_orders:
        await message.answer(
            """⚠️ <b>Market is inactive</b>

Order books have no orders (bids and asks are empty).
Possible reasons:
• Market has expired or closed
• Market has not started trading yet
• No liquidity on the market"""
        )
        await state.clear()
        return

    # Calculate spread and liquidity
    yes_info = calculate_spread_and_liquidity(yes_orderbook, "YES")
    no_info = calculate_spread_and_liquidity(no_orderbook, "NO")

    # Save data to state
    await state.update_data(
        market_id=market_id,
        market=market,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        yes_orderbook=yes_orderbook,
        no_orderbook=no_orderbook,
        yes_info=yes_info,
        no_info=no_info,
        client=client,
    )

    # Format market information in new format
    market_info_parts = []

    # Information for YES token
    if yes_info["best_bid"] is not None or yes_info["best_ask"] is not None:
        yes_bid = (
            f"{yes_info['best_bid'] * 100:.2f}¢"
            if yes_info["best_bid"] is not None
            else "no"
        )
        yes_ask = (
            f"{yes_info['best_ask'] * 100:.2f}¢"
            if yes_info["best_ask"] is not None
            else "no"
        )
        yes_lines = [f"✅ YES: Bid: {yes_bid} | Ask: {yes_ask}"]

        if yes_info["spread"]:
            spread_line = f"  Spread: {yes_info['spread'] * 100:.2f}¢ ({yes_info['spread_pct']:.2f}%) | Liquidity: ${yes_info['total_liquidity']:,.2f}"
            yes_lines.append(spread_line)
        elif yes_info["total_liquidity"] > 0:
            yes_lines.append(f"  Liquidity: ${yes_info['total_liquidity']:,.2f}")

        market_info_parts.append("\n".join(yes_lines))

    # Information for NO token
    if no_info["best_bid"] is not None or no_info["best_ask"] is not None:
        no_bid = (
            f"{no_info['best_bid'] * 100:.2f}¢"
            if no_info["best_bid"] is not None
            else "no"
        )
        no_ask = (
            f"{no_info['best_ask'] * 100:.2f}¢"
            if no_info["best_ask"] is not None
            else "no"
        )
        no_lines = [f"❌ NO: Bid: {no_bid} | Ask: {no_ask}"]

        if no_info["spread"]:
            spread_line = f"  Spread: {no_info['spread'] * 100:.2f}¢ ({no_info['spread_pct']:.2f}%) | Liquidity: ${no_info['total_liquidity']:,.2f}"
            no_lines.append(spread_line)
        elif no_info["total_liquidity"] > 0:
            no_lines.append(f"  Liquidity: ${no_info['total_liquidity']:,.2f}")

        market_info_parts.append("\n".join(no_lines))

    # Create keyboard with "Cancel" button
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Cancel", callback_data="cancel")

    # Format full message with empty line between blocks
    market_info_text = "\n\n".join(market_info_parts) if market_info_parts else ""

    # Create keyboard for side selection
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ YES", callback_data="side_yes")
    builder.button(text="❌ NO", callback_data="side_no")
    builder.button(text="✖️ Cancel", callback_data="cancel")
    builder.adjust(2)

    await message.answer(
        f"""📋 Market Found: {market.market_title}
📊 Market ID: {market_id}

{market_info_text}

📈 Select side:""",
        reply_markup=builder.as_markup(),
    )
    await state.set_state(MarketOrderStates.waiting_side)


@market_router.callback_query(
    F.data.startswith("submarket_"), MarketOrderStates.waiting_submarket
)
async def process_submarket(callback: CallbackQuery, state: FSMContext):
    """Handles submarket selection in categorical market."""
    try:
        submarket_index = int(callback.data.split("_")[1]) - 1

        data = await state.get_data()
        submarkets = data.get("submarkets", [])

        if submarket_index < 0 or submarket_index >= len(submarkets):
            await callback.message.edit_text("""❌ Invalid submarket selection""")
            await state.clear()
            await callback.answer()
            return

        selected_submarket = submarkets[submarket_index]
        submarket_id = selected_submarket["id"]

        if not submarket_id:
            await callback.message.edit_text("""❌ Failed to determine submarket ID""")
            await state.clear()
            await callback.answer()
            return

        # Get full information about selected submarket
        client = data.get("client")
        await callback.message.edit_text(
            f"""📊 Getting submarket information: {selected_submarket["title"]}..."""
        )

        market = await get_market_info(client, submarket_id, is_categorical=False)

        if not market:
            await callback.message.edit_text(
                """❌ Failed to get submarket information"""
            )
            await state.clear()
            await callback.answer()
            return

        # Get submarket tokens
        yes_token_id = getattr(market, "yes_token_id", None)
        no_token_id = getattr(market, "no_token_id", None)

        if not yes_token_id or not no_token_id:
            await callback.message.edit_text(
                """❌ Failed to determine submarket tokens"""
            )
            await state.clear()
            await callback.answer()
            return

        await callback.answer()

        # Continue processing as regular market
        await process_market_data(
            callback.message,
            state,
            market,
            submarket_id,
            client,
            yes_token_id,
            no_token_id,
        )
    except (ValueError, IndexError, KeyError) as e:
        logger.error(f"Error processing submarket selection: {e}")
        await callback.message.edit_text("""❌ Error processing submarket selection""")
        await state.clear()
        await callback.answer()


@market_router.message(MarketOrderStates.waiting_amount)
async def process_amount(message: Message, state: FSMContext):
    """Handles amount input for farming."""
    try:
        amount = float(message.text.strip())

        if amount <= 0:
            builder = InlineKeyboardBuilder()
            builder.button(text="✖️ Cancel", callback_data="cancel")
            await message.answer(
                """❌ Amount must be a positive number. Please try again:""",
                reply_markup=builder.as_markup(),
            )
            return

        await state.update_data(amount=amount)

        data = await state.get_data()

        # Get orderbook data for offset input
        token_name = data.get("token_name")
        current_price = data.get("current_price")
        direction = data.get("direction")
        client = data.get("client")

        # Check balance only for BUY orders
        if direction == "BUY":
            has_balance, current_balance = await check_usdt_balance(client, amount)

            if not has_balance:
                builder = InlineKeyboardBuilder()
                builder.button(text="✖️ Cancel", callback_data="cancel")
                await message.answer(
                    f"""❌ Insufficient USDT balance to place a BUY order for {amount} USDT.

💰 Available balance: {current_balance:.6f} USDT

Enter a different amount:""",
                    reply_markup=builder.as_markup(),
                )
                return

        # Get orderbook based on selected side
        if token_name == "YES":
            orderbook = data.get("yes_orderbook")
        else:
            orderbook = data.get("no_orderbook")

        if not orderbook:
            await message.answer("❌ Failed to get orderbook for selected token")
            await state.clear()
            return

        # Extract bids and asks from orderbook
        bids = orderbook.bids if hasattr(orderbook, "bids") else []
        asks = orderbook.asks if hasattr(orderbook, "asks") else []

        # Sort bids by descending price (highest first)
        sorted_bids = []
        if bids and len(bids) > 0:
            for bid in bids:
                if hasattr(bid, "price"):
                    try:
                        price = float(bid.price)
                        sorted_bids.append((price, bid))
                    except (ValueError, TypeError):
                        continue
            sorted_bids.sort(key=lambda x: x[0], reverse=True)

        # Sort asks by ascending price (lowest first)
        sorted_asks = []
        if asks and len(asks) > 0:
            for ask in asks:
                if hasattr(ask, "price"):
                    try:
                        price = float(ask.price)
                        sorted_asks.append((price, ask))
                    except (ValueError, TypeError):
                        continue
            sorted_asks.sort(key=lambda x: x[0])

        # Get best 5 bids (highest prices)
        best_bids = []
        for i, (price, bid) in enumerate(sorted_bids[:5]):
            price_cents = price * 100
            best_bids.append(price_cents)

        # Get best 5 asks (lowest prices)
        best_asks = []
        for i, (price, ask) in enumerate(sorted_asks[:5]):
            price_cents = price * 100
            best_asks.append(price_cents)

        # Find maximum distant bid (lowest of all bids)
        last_bid = None
        if sorted_bids:
            last_bid_price = sorted_bids[-1][0]
            last_bid = last_bid_price * 100

        # Find maximum distant ask (highest of all asks)
        last_ask = None
        if sorted_asks:
            last_ask_price = sorted_asks[-1][0]
            last_ask = last_ask_price * 100

        # Best bid (highest) - first in sorted list
        best_bid = best_bids[0] if best_bids else None

        if not best_bid:
            await message.answer("❌ No bids found in orderbook")
            await state.clear()
            return

        # Calculate maximum tick values for BUY and SELL
        tick_size = TICK_SIZE
        MIN_PRICE = 0.001
        MAX_PRICE = 0.999

        # For BUY: so price doesn't become < MIN_PRICE (0.001)
        max_offset_buy = int((current_price - MIN_PRICE) / tick_size)

        # For SELL: so price doesn't become > MAX_PRICE (0.999)
        max_offset_sell = int((MAX_PRICE - current_price) / tick_size)

        # Save orderbook processing results to state
        await state.update_data(
            tick_size=tick_size,
            max_offset_buy=max_offset_buy,
            max_offset_sell=max_offset_sell,
            best_bid=best_bid,
        )

        # Format text with best bids
        bids_text = "<b>Best 5 bids:</b>\n"
        for i, bid_price in enumerate(best_bids, 1):
            bids_text += f"{i}. {bid_price:.1f} ¢\n"
        if last_bid and last_bid not in best_bids:
            bids_text += f"...\n{last_bid:.1f} ¢\n"

        # Format text with best asks
        asks_text = "<b>Best 5 asks:</b>\n"
        for i, ask_price in enumerate(best_asks, 1):
            asks_text += f"{i}. {ask_price:.1f} ¢\n"
        if last_ask and last_ask not in best_asks:
            asks_text += f"...\n{last_ask:.1f} ¢\n"

        # Create keyboard with "Cancel" button
        builder = InlineKeyboardBuilder()
        builder.button(text="✖️ Cancel", callback_data="cancel")

        # Convert price to cents for display
        current_price_cents = current_price * 100
        current_price_str = f"{current_price_cents:.2f}".rstrip("0").rstrip(".")

        await message.answer(
            f"""✅ Amount: {amount} USDT

💵 Current price: {current_price_str}¢

{bids_text}
{asks_text}
Set the price offset (in ¢) relative to the current price.
Use a negative value to move closer to the current price (e.g., <code>-0.8</code>).
For example <code>0.8</code>:""",
            reply_markup=builder.as_markup(),
        )
        await state.set_state(MarketOrderStates.waiting_offset_ticks)
    except ValueError:
        builder = InlineKeyboardBuilder()
        builder.button(text="✖️ Cancel", callback_data="cancel")
        await message.answer(
            """❌ Invalid amount format. Enter a number:""",
            reply_markup=builder.as_markup(),
        )


@market_router.callback_query(
    F.data.startswith("side_"), MarketOrderStates.waiting_side
)
async def process_side(callback: CallbackQuery, state: FSMContext):
    """Handles side selection (YES/NO)."""
    side = callback.data.split("_")[1].upper()

    data = await state.get_data()

    if side == "YES":
        token_id = data.get("yes_token_id")
        token_name = "YES"
        yes_info = data.get("yes_info", {})
        current_price = yes_info.get("mid_price") if yes_info else None
    else:
        token_id = data.get("no_token_id")
        token_name = "NO"
        no_info = data.get("no_info", {})
        current_price = no_info.get("mid_price") if no_info else None

    if not current_price:
        await callback.message.answer(
            "❌ Failed to determine current price for selected token"
        )
        await state.clear()
        await callback.answer()
        return

    # Save only basic token data - orderbook processing will be done later when needed
    await state.update_data(
        token_id=token_id,
        token_name=token_name,
        current_price=current_price,
    )

    # Create keyboard for direction selection
    builder = InlineKeyboardBuilder()
    builder.button(text="📈 BUY (buy, below current price)", callback_data="dir_buy")
    builder.button(text="📉 SELL (sell, above current price)", callback_data="dir_sell")
    builder.button(text="✖️ Cancel", callback_data="cancel")
    builder.adjust(1)

    # Convert price to cents for display
    current_price_cents = current_price * 100
    current_price_str = f"{current_price_cents:.2f}".rstrip("0").rstrip(".")

    await callback.message.edit_text(
        f"""✅ Selected: {token_name}

💵 Current price: {current_price_str}¢

Select order direction:""",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()
    await state.set_state(MarketOrderStates.waiting_direction)


@market_router.message(MarketOrderStates.waiting_offset_ticks)
async def process_offset_ticks(message: Message, state: FSMContext):
    """
    Handles offset input in cents.
    User enters offset in cents, we convert to ticks for validation and further processing.
    """
    try:
        offset_cents = float(message.text.strip())

        data = await state.get_data()
        best_bid = data.get("best_bid")
        current_price = data.get("current_price")
        tick_size = data.get("tick_size", TICK_SIZE)
        max_offset_buy = data.get("max_offset_buy", 0)
        max_offset_sell = data.get("max_offset_sell", 0)
        direction = data.get("direction")

        if not best_bid:
            await message.answer("❌ Error: best bid not found")
            await state.clear()
            return

        if not direction:
            await message.answer("❌ Error: Direction not found. Please start again.")
            await state.clear()
            return

        # Convert offset in cents to ticks
        offset_ticks = int(round(offset_cents / (100 * tick_size)))

        # Validation: check value is in valid range
        builder = InlineKeyboardBuilder()
        builder.button(text="✖️ Cancel", callback_data="cancel")

        min_offset, max_offset = get_offset_bounds(
            direction, max_offset_buy, max_offset_sell
        )
        min_offset_cents = min_offset * tick_size * 100
        max_offset_cents = max_offset * tick_size * 100

        if offset_ticks < min_offset:
            await message.answer(
                f"❌ Offset is too small!\n"
                f"Enter a value from {min_offset_cents:.1f} to {max_offset_cents:.1f} cents:",
                reply_markup=builder.as_markup(),
            )
            return

        if offset_ticks > max_offset:
            await message.answer(
                f"❌ Offset is too large!\n"
                f"Enter a value from {min_offset_cents:.1f} to {max_offset_cents:.1f} cents:",
                reply_markup=builder.as_markup(),
            )
            return

        await state.update_data(offset_ticks=offset_ticks)

        # Validate offset for selected direction (direction-aware bounds already applied)

        # Calculate target price based on direction and offset
        target_price, is_valid = calculate_target_price(
            current_price, direction, offset_ticks, tick_size
        )

        if not is_valid or target_price <= 0:
            await message.answer(
                f"❌ Error: Calculated price ({target_price:.6f}) is invalid!\n\n"
                f"Offset {offset_ticks} ticks is too large for current price {current_price:.6f}.\n"
                f"Enter a smaller offset:",
                reply_markup=builder.as_markup(),
            )
            return

        await state.update_data(target_price=target_price)

        # Ask for reposition threshold
        builder = InlineKeyboardBuilder()
        builder.button(text="✖️ Cancel", callback_data="cancel")

        # Convert prices to cents for display
        current_price_cents = current_price * 100
        tick_size_cents = tick_size * 100
        target_price_cents = target_price * 100

        # Format without trailing zeros
        current_price_str = f"{current_price_cents:.2f}".rstrip("0").rstrip(".")
        tick_size_str = f"{tick_size_cents:.2f}".rstrip("0").rstrip(".")
        target_price_str = f"{target_price_cents:.2f}".rstrip("0").rstrip(".")

        await message.answer(
            f"""✅ Offset: {offset_cents:.1f}¢ ({offset_ticks} ticks)

📊 Settings:
• Current price: {current_price_str}¢
• Target price: {target_price_str}¢
• Tick size: {tick_size_str}¢

⚙️ <b>Reposition Threshold</b>

Enter the price deviation threshold (in cents) for repositioning the order.

For example:
• <code>0.5</code> - reposition when price changes by 0.5 cents or more
• <code>1.0</code> - reposition when price changes by 1 cent or more
• <code>2.0</code> - reposition when price changes by 2 cents or more

Recommended: <code>0.5</code> cents

Enter the threshold:""",
            reply_markup=builder.as_markup(),
        )
        await state.set_state(MarketOrderStates.waiting_reposition_threshold)
    except ValueError:
        data = await state.get_data()
        tick_size = data.get("tick_size", TICK_SIZE)
        max_offset_buy = data.get("max_offset_buy", 0)
        max_offset_sell = data.get("max_offset_sell", 0)
        direction = data.get("direction")

        min_offset, max_offset = get_offset_bounds(
            direction, max_offset_buy, max_offset_sell
        )
        min_offset_cents = min_offset * tick_size * 100
        max_offset_cents = max_offset * tick_size * 100

        builder = InlineKeyboardBuilder()
        builder.button(text="✖️ Cancel", callback_data="cancel")
        await message.answer(
            f"❌ Invalid format. Enter a number from {min_offset_cents:.1f} to {max_offset_cents:.1f} cents:",
            reply_markup=builder.as_markup(),
        )


@market_router.callback_query(
    F.data.startswith("dir_"), MarketOrderStates.waiting_direction
)
async def process_direction(callback: CallbackQuery, state: FSMContext):
    """Handles direction selection (BUY/SELL)."""
    direction = callback.data.split("_")[1].upper()

    data = await state.get_data()
    token_name = data.get("token_name")

    order_side = OrderSide.BUY if direction == "BUY" else OrderSide.SELL

    await state.update_data(direction=direction, order_side=order_side)

    # Ask for amount
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Cancel", callback_data="cancel")

    await callback.message.edit_text(
        f"""✅ Selected direction: {direction} {token_name}

💰 Enter the amount for farming (in USDT, e.g. 10):""",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()
    await state.set_state(MarketOrderStates.waiting_amount)


@market_router.callback_query(F.data == "cancel")
async def process_cancel(callback: CallbackQuery, state: FSMContext):
    """
    Universal handler for 'Cancel' button for all order placement states.
    Works in all MarketOrderStates.
    """
    try:
        # Try to edit message (if it's an inline button)
        await callback.message.edit_text("❌ Order placement cancelled")
    except Exception:
        # If editing failed, send new message
        await callback.message.answer("❌ Order placement cancelled")

    await state.clear()
    await callback.answer()

    # Send instruction message
    await callback.message.answer(
        """Use the /floating_order to place floating order.
Use the /market to place a market order.
Use the /limit to place a limit order.
Use the /limit_first command for keeps your limit orders always first in the order book.
Use the /orders to manage your orders.
Use the /check_profile to view profile statistics.
Use the /profile_list to view all your profiles.
Use the /help to view instructions.
Use the /support to contact administrator.

sDocs: https://bidask-bot.gitbook.io/docs/""",
        disable_web_page_preview=True,
    )


@market_router.message(MarketOrderStates.waiting_reposition_threshold)
async def process_reposition_threshold(message: Message, state: FSMContext):
    """Handles reposition threshold input (in cents)."""
    try:
        threshold_cents = float(message.text.strip())

        # Validation: must be positive
        if threshold_cents <= 0:
            builder = InlineKeyboardBuilder()
            builder.button(text="✖️ Cancel", callback_data="cancel")
            await message.answer(
                "❌ Threshold must be a positive number.\n\nEnter the threshold in cents (e.g., 0.5):",
                reply_markup=builder.as_markup(),
            )
            return

        # Get data to calculate offset for validation
        data = await state.get_data()
        offset_ticks = data.get("offset_ticks")
        tick_size = data.get("tick_size", TICK_SIZE)

        # Convert offset from ticks to cents
        offset_cents = offset_ticks * tick_size * 100
        offset_cents_abs = abs(offset_cents)

        # Validation: threshold must be less than offset magnitude
        if threshold_cents >= offset_cents_abs:
            builder = InlineKeyboardBuilder()
            builder.button(text="✖️ Cancel", callback_data="cancel")
            offset_cents_formatted = f"{offset_cents_abs:.2f}".rstrip("0").rstrip(".")
            threshold_cents_formatted = f"{threshold_cents:.2f}".rstrip("0").rstrip(".")
            await message.answer(
                f"""❌ Threshold ({threshold_cents_formatted}¢) must be less than offset ({offset_cents_formatted}¢).

If threshold is greater than or equal to the offset, the order will never be repositioned.

Enter a threshold less than {offset_cents_formatted}¢:""",
                reply_markup=builder.as_markup(),
            )
            return

        # Save threshold to state
        await state.update_data(reposition_threshold_cents=threshold_cents)

        # Get all data for confirmation
        market = data.get("market")
        token_name = data.get("token_name")
        direction = data.get("direction")
        current_price = data.get("current_price")
        target_price = data.get("target_price")
        amount = data.get("amount")

        # Convert prices to cents and remove trailing zeros
        current_price_cents = current_price * 100
        target_price_cents = target_price * 100

        # Format prices without trailing zeros
        current_price_str = f"{current_price_cents:.2f}".rstrip("0").rstrip(".")
        target_price_str = f"{target_price_cents:.2f}".rstrip("0").rstrip(".")
        offset_cents_str = f"{offset_cents:.2f}".rstrip("0").rstrip(".")

        confirm_text = f"""📋 <b>Settings Confirmation</b>

📊 <b>Market:</b>
Name: {market.market_title}
Outcome: {token_name}

💰 <b>Farm settings:</b>
Side: {direction} {token_name}
Current price: {current_price_str}¢
Current target price: {target_price_str}¢
Offset: {offset_cents_str}¢
Reposition threshold: {threshold_cents:.2f}¢

Amount: {amount} USDT"""

        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Place Order", callback_data="confirm_yes")
        builder.button(text="✖️ Cancel", callback_data="cancel")
        builder.adjust(2)

        await message.answer(confirm_text, reply_markup=builder.as_markup())
        await state.set_state(MarketOrderStates.waiting_confirm)

    except ValueError:
        builder = InlineKeyboardBuilder()
        builder.button(text="✖️ Cancel", callback_data="cancel")
        await message.answer(
            "❌ Invalid format. Enter a number (e.g., 0.5 for 0.5 cents):",
            reply_markup=builder.as_markup(),
        )


@market_router.callback_query(
    F.data.startswith("confirm_"), MarketOrderStates.waiting_confirm
)
async def process_confirm(callback: CallbackQuery, state: FSMContext):
    """Handles order placement confirmation."""
    confirm = callback.data.split("_")[1]

    if confirm != "yes":
        await callback.message.edit_text("""❌ Order placement cancelled""")
        await state.clear()
        try:
            await callback.answer()
        except Exception:
            pass  # Ignore if query is too old
        return

    data = await state.get_data()
    client = data.get("client")

    order_params = {
        "market_id": data.get("market_id"),
        "token_id": data.get("token_id"),
        "side": data.get("order_side"),
        "price": str(data.get("target_price")),
        "amount": data.get("amount"),
        "token_name": data.get("token_name"),
    }

    # Answer callback early to avoid "query is too old" error
    try:
        await callback.answer()
    except Exception:
        pass  # Ignore if query is too old

    await callback.message.edit_text("""🔄 Placing order...""")

    success, order_id, error_message = await place_limit_order(client, order_params)

    if success:
        # Save order to database
        try:
            account_id = data.get("account_id")
            if not account_id:
                logger.error("Account ID not found in state data")
                await callback.message.edit_text(
                    """❌ Account not found. Please start again with /floating_order."""
                )
                await state.clear()
                try:
                    await callback.answer()
                except Exception:
                    pass  # Ignore if query is too old
                return

            market_id = data.get("market_id")
            root_market_id = data.get("root_market_id")  # None для binary markets
            market = data.get("market")
            market_title = getattr(market, "market_title", None) if market else None
            token_id = data.get("token_id")
            token_name = data.get("token_name")
            side = data.get("direction")  # BUY or SELL
            current_price = data.get("current_price")
            target_price = data.get("target_price")
            offset_ticks = data.get("offset_ticks")
            tick_size = data.get("tick_size", TICK_SIZE)
            offset_cents = offset_ticks * tick_size * 100 if offset_ticks else 0
            amount = data.get("amount")
            reposition_threshold_cents = data.get("reposition_threshold_cents", 0.5)

            # Сохраняем ордер в базу данных
            await save_order(
                account_id=account_id,
                order_id=order_id,
                market_id=market_id,
                market_title=market_title,
                token_id=token_id,
                token_name=token_name,
                side=side,
                current_price=current_price,
                target_price=target_price,
                offset_ticks=offset_ticks,
                offset_cents=offset_cents,
                amount=amount,
                status="pending",
                reposition_threshold_cents=reposition_threshold_cents,
                root_market_id=root_market_id,
            )
            logger.info(
                f"Order {order_id} successfully saved to DB for account {account_id}"
            )

            # Подписываемся на маркет через WebSocket (если менеджер запущен)
            try:
                websocket_sync = get_websocket_sync()
                if websocket_sync:
                    await websocket_sync.subscribe_to_market(market_id, root_market_id)
                    logger.info(
                        f"Подписка на маркет {market_id} (root: {root_market_id}) через WebSocket"
                    )
                else:
                    logger.debug(
                        "WebSocket менеджер не запущен, подписка будет выполнена при старте"
                    )
            except Exception as e:
                logger.warning(
                    f"Ошибка при подписке на маркет {market_id} через WebSocket: {e}"
                )

        except Exception as e:
            logger.error(f"Error saving order to DB: {e}")

        market_id = data.get("market_id")
        root_market_id = data.get("root_market_id")
        market_url = get_market_url(market_id, root_market_id) if market_id else None
        market_link_line = (
            f'• Market link: <a href="{market_url}">Open market</a>\n'
            if market_url
            else ""
        )

        await callback.message.edit_text(
            f"""✅ <b>Order successfully placed!</b>

📋 <b>Final Information:</b>
• Side: {data.get("direction")} {data.get("token_name")}
• Price: {data.get("target_price", 0):.6f}
• Amount: {data.get("amount", 0)} USDT
• Offset: {offset_cents:.2f}¢
• Reposition threshold: {reposition_threshold_cents:.2f}¢
• Order ID: <code>{order_id}</code>
{market_link_line}

📌 <b>Useful commands:</b>
• /floating_order - start a new farm
• /orders - manage your orders
• /check_profile - view profile statistics""",
            disable_web_page_preview=True,
        )
    else:
        error_text = f"""❌ <b>Failed to place order</b>

{error_message if error_message else "Please check your balance and order parameters."}

📌 <b>Useful commands:</b>
• /floating_order - start a new farm
• /orders - manage your orders
• /check_profile - view profile statistics"""
        await callback.message.edit_text(error_text)

    await state.clear()
