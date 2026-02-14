"""
Router for fixed-offset limit order placement (/limit_first command).
Places a limit order with a fixed offset of -0.01 (offset_ticks = -10).
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
from opinion.helper import (
    build_cancel_keyboard,
    calculate_target_price,
    get_market_url,
)
from opinion.opinion_api_wrapper import (
    calculate_spread_and_liquidity,
    check_usdt_balance,
    get_categorical_market_submarkets,
    get_latest_price,
    get_market_info,
    get_orderbooks,
    parse_market_url,
    place_limit_order,
)
from opinion_clob_sdk.chain.py_order_utils.model.sides import OrderSide
from service.config import TICK_SIZE
from service.database import (
    get_opinion_account,
    get_user,
    get_user_accounts,
    save_order,
)

logger = logging.getLogger(__name__)


class LimitFirstOrderStates(StatesGroup):
    """States for the fixed-offset limit order placement process."""

    limit_first_account_selection = State()
    limit_first_url = State()
    limit_first_submarket = State()
    limit_first_side = State()
    limit_first_direction = State()
    limit_first_amount = State()
    limit_first_confirm = State()


limit_first_order_router = Router()

FIXED_OFFSET_TICKS = -10


@limit_first_order_router.message(Command("limit_first"))
async def cmd_limit_first(message: Message, state: FSMContext):
    """Handler for /limit_first command - start fixed-offset limit order placement."""
    telegram_id = message.from_user.id
    user = await get_user(telegram_id)

    if not user:
        await message.answer(
            """❌ You are not registered. Use the /start to register."""
        )
        return

    accounts = await get_user_accounts(telegram_id)
    if not accounts:
        await message.answer(
            """❌ You don't have any Opinion profiles yet.

Use /add_profile to add your first Opinion profile."""
        )
        return

    if len(accounts) == 1:
        account_id = accounts[0]["account_id"]
        await state.update_data(account_id=account_id)
        builder = build_cancel_keyboard("limit_first_cancel")
        await message.answer(
            """📊 Place a Fixed Offset Limit Order

Please enter the <a href="https://app.opinion.trade?code=BJea79">Opinion.trade</a> market link:""",
            reply_markup=builder.as_markup(),
            disable_web_page_preview=True,
        )
        await state.set_state(LimitFirstOrderStates.limit_first_url)
        return

    builder = InlineKeyboardBuilder()
    for account in accounts:
        wallet = account["wallet_address"]
        account_id = account["account_id"]
        builder.button(
            text=f"Account {account_id} ({wallet[:8]}...)",
            callback_data=f"limit_first_select_account_{account_id}",
        )
    builder.button(text="✖️ Cancel", callback_data="limit_first_cancel")
    builder.adjust(1)

    await message.answer(
        """📊 Place a Fixed Offset Limit Order

Select an account to use:""",
        reply_markup=builder.as_markup(),
    )
    await state.set_state(LimitFirstOrderStates.limit_first_account_selection)


@limit_first_order_router.callback_query(
    F.data.startswith("limit_first_select_account_"),
    LimitFirstOrderStates.limit_first_account_selection,
)
async def process_account_selection(callback: CallbackQuery, state: FSMContext):
    """Handles account selection."""
    account_id_str = callback.data.replace("limit_first_select_account_", "")
    try:
        account_id = int(account_id_str)
    except ValueError:
        await callback.answer("Invalid account ID", show_alert=True)
        return

    await state.update_data(account_id=account_id)
    builder = build_cancel_keyboard("limit_first_cancel")
    await callback.message.edit_text(
        """📊 Place a Fixed Offset Limit Order

Please enter the <a href="https://app.opinion.trade?code=BJea79">Opinion.trade</a> market link:""",
        reply_markup=builder.as_markup(),
        disable_web_page_preview=True,
    )
    await state.set_state(LimitFirstOrderStates.limit_first_url)
    await callback.answer()


@limit_first_order_router.message(LimitFirstOrderStates.limit_first_url)
async def process_market_url(message: Message, state: FSMContext):
    """Handles market URL input."""
    url = message.text.strip()
    market_id, market_type = parse_market_url(url)

    if not market_id:
        builder = build_cancel_keyboard("limit_first_cancel")
        await message.answer(
            """❌ Failed to extract Market ID from URL. Please try again:""",
            reply_markup=builder.as_markup(),
        )
        return

    is_categorical = market_type == "multi"

    data = await state.get_data()
    account_id = data.get("account_id")
    if not account_id:
        await message.answer(
            """❌ Account not selected. Please start again with /limit_first."""
        )
        await state.clear()
        return

    account = await get_opinion_account(account_id)
    if not account:
        await message.answer(
            """❌ Account not found. Please start again with /limit_first."""
        )
        await state.clear()
        return

    try:
        client = create_client(account)
    except Exception as exc:
        error_str = str(exc)
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
            "Failed to create client for user %s [CODE: %s] [TIME: %s]: %s",
            message.from_user.id,
            error_hash,
            error_time,
            exc,
        )
        return

    await message.answer("""📊 Getting market information...""")
    market = await get_market_info(client, market_id, is_categorical)

    if not market:
        await message.answer(
            """❌ Failed to get market information. Please check the URL."""
        )
        await state.clear()
        return

    market_title = getattr(market, "market_title", "Unknown Market")
    await message.answer(f"""✅ Market found: <b>{market_title}</b>""")

    if is_categorical:
        submarkets = get_categorical_market_submarkets(market)
        if not submarkets:
            await message.answer(
                """❌ Failed to find submarkets in the categorical market"""
            )
            await state.clear()
            return

        submarket_list = []
        for i, subm in enumerate(submarkets, 1):
            submarket_id = getattr(subm, "market_id", getattr(subm, "id", None))
            title = getattr(
                subm,
                "market_title",
                getattr(subm, "title", getattr(subm, "name", f"Submarket {i}")),
            )
            submarket_list.append({"id": submarket_id, "title": title, "data": subm})

        await state.update_data(
            submarkets=submarket_list,
            client=client,
            root_market_id=market_id,
        )

        builder = InlineKeyboardBuilder()
        for i, subm in enumerate(submarket_list, 1):
            builder.button(
                text=f"{subm['title'][:30]}",
                callback_data=f"limit_first_submarket_{i}",
            )
        builder.button(text="✖️ Cancel", callback_data="limit_first_cancel")
        builder.adjust(1)

        await message.answer(
            f"""📋 <b>Categorical Market</b>

Found submarkets: {len(submarket_list)}

Select a submarket:""",
            reply_markup=builder.as_markup(),
        )
        await state.set_state(LimitFirstOrderStates.limit_first_submarket)
        return

    yes_token_id = getattr(market, "yes_token_id", None)
    no_token_id = getattr(market, "no_token_id", None)
    if not yes_token_id or not no_token_id:
        await message.answer("""❌ Failed to determine market tokens""")
        await state.clear()
        return

    await state.update_data(client=client, root_market_id=None)
    await process_market_data(
        message,
        state,
        market,
        market_id,
        None,
        client,
        yes_token_id,
        no_token_id,
    )


async def process_market_data(
    message: Message,
    state: FSMContext,
    market,
    market_id: int,
    root_market_id: int | None,
    client,
    yes_token_id: str,
    no_token_id: str,
):
    """Processes market data and continues order placement."""
    yes_orderbook, no_orderbook = await get_orderbooks(
        client, yes_token_id, no_token_id
    )

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

    yes_info = calculate_spread_and_liquidity(yes_orderbook, "YES")
    no_info = calculate_spread_and_liquidity(no_orderbook, "NO")

    await state.update_data(
        market_id=market_id,
        root_market_id=root_market_id,
        market=market,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        yes_orderbook=yes_orderbook,
        no_orderbook=no_orderbook,
        yes_info=yes_info,
        no_info=no_info,
        client=client,
    )

    market_info_parts = []
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
            spread_line = (
                f"  Spread: {yes_info['spread'] * 100:.2f}¢ "
                f"({yes_info['spread_pct']:.2f}%) | "
                f"Liquidity: ${yes_info['total_liquidity']:,.2f}"
            )
            yes_lines.append(spread_line)
        elif yes_info["total_liquidity"] > 0:
            yes_lines.append(f"  Liquidity: ${yes_info['total_liquidity']:,.2f}")
        market_info_parts.append("\n".join(yes_lines))

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
            spread_line = (
                f"  Spread: {no_info['spread'] * 100:.2f}¢ "
                f"({no_info['spread_pct']:.2f}%) | "
                f"Liquidity: ${no_info['total_liquidity']:,.2f}"
            )
            no_lines.append(spread_line)
        elif no_info["total_liquidity"] > 0:
            no_lines.append(f"  Liquidity: ${no_info['total_liquidity']:,.2f}")
        market_info_parts.append("\n".join(no_lines))

    market_info_text = "\n\n".join(market_info_parts) if market_info_parts else ""

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ YES", callback_data="limit_first_side_yes")
    builder.button(text="❌ NO", callback_data="limit_first_side_no")
    builder.button(text="✖️ Cancel", callback_data="limit_first_cancel")
    builder.adjust(2)

    await message.answer(
        f"""📋 Market Found: {market.market_title}
📊 Market ID: {market_id}

{market_info_text}

📈 Select side:""",
        reply_markup=builder.as_markup(),
    )
    await state.set_state(LimitFirstOrderStates.limit_first_side)


@limit_first_order_router.callback_query(
    F.data.startswith("limit_first_submarket_"),
    LimitFirstOrderStates.limit_first_submarket,
)
async def process_submarket(callback: CallbackQuery, state: FSMContext):
    """Handles submarket selection in categorical markets."""
    try:
        submarket_index = int(callback.data.split("_")[3]) - 1
    except (ValueError, IndexError):
        await callback.answer("Invalid submarket selection", show_alert=True)
        return

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

    client = data.get("client")
    await callback.message.edit_text(
        f"""📊 Getting submarket information: {selected_submarket["title"]}..."""
    )
    market = await get_market_info(client, submarket_id, is_categorical=False)
    if not market:
        await callback.message.edit_text("""❌ Failed to get submarket information""")
        await state.clear()
        await callback.answer()
        return

    yes_token_id = getattr(market, "yes_token_id", None)
    no_token_id = getattr(market, "no_token_id", None)
    if not yes_token_id or not no_token_id:
        await callback.message.edit_text("""❌ Failed to determine submarket tokens""")
        await state.clear()
        await callback.answer()
        return

    await callback.answer()
    await process_market_data(
        callback.message,
        state,
        market,
        submarket_id,
        data.get("root_market_id"),
        client,
        yes_token_id,
        no_token_id,
    )


@limit_first_order_router.callback_query(
    F.data.startswith("limit_first_side_"), LimitFirstOrderStates.limit_first_side
)
async def process_side(callback: CallbackQuery, state: FSMContext):
    """Handles side selection (YES/NO)."""
    side = callback.data.split("_")[3].upper()
    data = await state.get_data()

    if side == "YES":
        token_id = data.get("yes_token_id")
        token_name = "YES"
    else:
        token_id = data.get("no_token_id")
        token_name = "NO"

    client = data.get("client")
    current_price = await get_latest_price(client, token_id)

    if current_price is None:
        await callback.message.answer(
            "❌ Failed to determine current price for selected token"
        )
        await state.clear()
        await callback.answer()
        return

    await state.update_data(
        token_id=token_id,
        token_name=token_name,
        current_price=current_price,
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="📈 BUY (limit buy)", callback_data="limit_first_dir_buy")
    builder.button(text="📉 SELL (limit sell)", callback_data="limit_first_dir_sell")
    builder.button(text="✖️ Cancel", callback_data="limit_first_cancel")
    builder.adjust(1)

    current_price_cents = current_price * 100
    current_price_str = f"{current_price_cents:.2f}".rstrip("0").rstrip(".")

    await callback.message.edit_text(
        f"""✅ Selected: {token_name}

💵 Current price: {current_price_str}¢

Select order direction:""",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()
    await state.set_state(LimitFirstOrderStates.limit_first_direction)


@limit_first_order_router.callback_query(
    F.data.startswith("limit_first_dir_"), LimitFirstOrderStates.limit_first_direction
)
async def process_direction(callback: CallbackQuery, state: FSMContext):
    """Handles direction selection (BUY/SELL)."""
    direction = callback.data.split("_")[3].upper()
    data = await state.get_data()
    token_name = data.get("token_name")
    order_side = OrderSide.BUY if direction == "BUY" else OrderSide.SELL

    await state.update_data(direction=direction, order_side=order_side)
    builder = build_cancel_keyboard("limit_first_cancel")
    await callback.message.edit_text(
        f"""✅ Selected direction: {direction} {token_name}

💰 Enter the amount (in USDT, e.g. 10):""",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()
    await state.set_state(LimitFirstOrderStates.limit_first_amount)


@limit_first_order_router.message(LimitFirstOrderStates.limit_first_amount)
async def process_amount(message: Message, state: FSMContext):
    """Handles amount input for fixed-offset limit orders."""
    try:
        amount = float(message.text.strip())
    except ValueError:
        builder = build_cancel_keyboard("limit_first_cancel")
        await message.answer(
            """❌ Invalid amount format. Enter a number:""",
            reply_markup=builder.as_markup(),
        )
        return

    if amount <= 0:
        builder = build_cancel_keyboard("limit_first_cancel")
        await message.answer(
            """❌ Amount must be a positive number. Please try again:""",
            reply_markup=builder.as_markup(),
        )
        return

    data = await state.get_data()
    direction = data.get("direction")
    client = data.get("client")
    token_name = data.get("token_name")
    current_price = data.get("current_price")

    if direction == "BUY":
        has_balance, current_balance = await check_usdt_balance(client, amount)
        if not has_balance:
            builder = build_cancel_keyboard("limit_first_cancel")
            await message.answer(
                f"""❌ Insufficient USDT balance to place a BUY order for {amount} USDT.

💰 Available balance: {current_balance:.6f} USDT

Enter a different amount:""",
                reply_markup=builder.as_markup(),
            )
            return

    target_price, is_valid = calculate_target_price(
        current_price, direction, FIXED_OFFSET_TICKS, TICK_SIZE
    )
    if not is_valid:
        await message.answer(
            "❌ Failed to calculate target price for the fixed offset."
        )
        await state.clear()
        return

    await state.update_data(amount=amount, target_price=target_price)

    current_price_cents = current_price * 100
    target_price_cents = target_price * 100
    offset_cents = FIXED_OFFSET_TICKS * TICK_SIZE * 100

    current_price_str = f"{current_price_cents:.2f}".rstrip("0").rstrip(".")
    target_price_str = f"{target_price_cents:.2f}".rstrip("0").rstrip(".")
    offset_str = f"{offset_cents:.2f}".rstrip("0").rstrip(".")

    confirm_text = f"""📋 <b>Fixed Offset Limit Order</b>

🪙 {direction} {token_name}
Current price: {current_price_str}¢
Target price: {target_price_str}¢
Offset: {offset_str}¢ (fixed)
Amount: {amount} USDT

Place this limit order?"""

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Place Order", callback_data="limit_first_confirm_yes")
    builder.button(text="✖️ Cancel", callback_data="limit_first_cancel")
    builder.adjust(2)

    await message.answer(confirm_text, reply_markup=builder.as_markup())
    await state.set_state(LimitFirstOrderStates.limit_first_confirm)


@limit_first_order_router.callback_query(
    F.data.startswith("limit_first_confirm_"), LimitFirstOrderStates.limit_first_confirm
)
async def process_confirm(callback: CallbackQuery, state: FSMContext):
    """Handles order placement confirmation."""
    confirm = callback.data.replace("limit_first_confirm_", "")
    if confirm != "yes":
        await callback.message.edit_text("""❌ Order placement cancelled""")
        await state.clear()
        await callback.answer()
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

    await callback.answer()
    await callback.message.edit_text("""🔄 Placing limit order...""")

    success, order_id, error_message = await place_limit_order(client, order_params)

    if success:
        try:
            account_id = data.get("account_id")
            market_id = data.get("market_id")
            root_market_id = data.get("root_market_id")
            market = data.get("market")
            market_title = getattr(market, "market_title", None) if market else None
            token_id = data.get("token_id")
            token_name = data.get("token_name")
            side = data.get("direction")
            current_price = data.get("current_price")
            target_price = data.get("target_price")
            amount = data.get("amount")
            offset_cents = FIXED_OFFSET_TICKS * TICK_SIZE * 100

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
                offset_ticks=FIXED_OFFSET_TICKS,
                offset_cents=offset_cents,
                amount=amount,
                status="pending",
                reposition_threshold_cents=0,
                root_market_id=root_market_id,
            )
            logger.info(
                "Fixed offset limit order %s saved to DB for account %s",
                order_id,
                account_id,
            )
        except Exception as exc:
            logger.error("Error saving limit_first order to DB: %s", exc)

        market_id = data.get("market_id")
        root_market_id = data.get("root_market_id")
        market_url = get_market_url(market_id, root_market_id) if market_id else None
        market_link_line = (
            f'• Market link: <a href="{market_url}">Open market</a>\n'
            if market_url
            else ""
        )

        await callback.message.edit_text(
            f"""✅ <b>Limit order placed!</b>

• Side: {data.get("direction")} {data.get("token_name")}
• Price: {data.get("target_price", 0):.6f}
• Amount: {data.get("amount", 0)} USDT
• Offset: {FIXED_OFFSET_TICKS * TICK_SIZE * 100:.2f}¢
• Order ID: <code>{order_id}</code>
{market_link_line}

📌 <b>Useful commands:</b>
• /limit_first - place a fixed offset limit order
• /limit - place a limit order
• /market - place a market order
• /orders - manage your orders
• /follow &lt;address&gt; &lt;label&gt; - follow a wallet
• /unfollow &lt;address&gt; - stop monitoring a wallet""",
            disable_web_page_preview=True,
        )
    else:
        await callback.message.edit_text(
            f"""❌ <b>Failed to place limit order</b>

{error_message if error_message else "Please check your balance and order parameters."}

📌 <b>Useful commands:</b>
• /limit_first - place a fixed offset limit order
• /limit - place a limit order
• /market - place a market order
• /orders - manage your orders
• /follow &lt;address&gt; &lt;label&gt; - follow a wallet
• /unfollow &lt;address&gt; - stop monitoring a wallet"""
        )

    await state.clear()


@limit_first_order_router.callback_query(F.data == "limit_first_cancel")
async def process_cancel(callback: CallbackQuery, state: FSMContext):
    """Universal cancel handler for /limit_first flow."""
    try:
        await callback.message.edit_text("❌ Order placement cancelled")
    except Exception:
        await callback.message.answer("❌ Order placement cancelled")

    await state.clear()
    await callback.answer()

    await callback.message.answer(
        """Use the /floating_order to place floating order.
Use the /market to place a market order.
Use the /limit to place a limit order.
Use the /limit_first command for keeps your limit orders always first in the order book.
Use the /orders to manage your orders.
Use the /follow &lt;address&gt; &lt;label&gt; to follow a wallet.
Use the /unfollow &lt;address&gt; to stop monitoring a wallet.
Use the /check_profile to view profile statistics.
Use the /profile_list to view all your profiles.
Use the /help to view instructions.
Use the /support to contact administrator.
Docs: https://bidask-bot.gitbook.io/docs/""",
        disable_web_page_preview=True,
    )
