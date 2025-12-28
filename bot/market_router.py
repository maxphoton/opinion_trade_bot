"""
Router for market order placement flow (/make_market command).
Handles the complete order placement process from URL input to order confirmation.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse, parse_qs

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from opinion_clob_sdk import Client
from opinion_clob_sdk.chain.py_order_utils.model.order import PlaceOrderDataInput
from opinion_clob_sdk.chain.py_order_utils.model.sides import OrderSide
from opinion_clob_sdk.chain.py_order_utils.model.order_type import LIMIT_ORDER

from database import get_user, save_order
from client_factory import create_client
from config import TICK_SIZE

logger = logging.getLogger(__name__)

# ============================================================================
# States for market order placement
# ============================================================================

class MarketOrderStates(StatesGroup):
    """States for the order placement process."""
    waiting_url = State()
    waiting_submarket = State()  # For submarket selection in categorical markets
    waiting_amount = State()
    waiting_side = State()
    waiting_offset_ticks = State()
    waiting_direction = State()
    waiting_reposition_threshold = State()  # Порог отклонения для перестановки ордера
    waiting_confirm = State()


# ============================================================================
# Helper functions for market operations
# ============================================================================

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


async def get_market_info(client: Client, market_id: int, is_categorical: bool = False):
    """Gets market information."""
    try:
        if is_categorical:
            response = client.get_categorical_market(market_id=market_id)
        else:
            response = client.get_market(market_id=market_id, use_cache=True)

        if response.errno == 0:
            return response.result.data
        else:
            logger.error(f"Error getting market: {response.errmsg} (code: {response.errno})")
            return None
    except Exception as e:
        logger.error(f"Error getting market: {e}")
        return None


def get_categorical_market_submarkets(market) -> list:
    """Extracts list of submarkets from categorical market."""
    if hasattr(market, 'child_markets') and market.child_markets:
        return market.child_markets
    return []


async def get_orderbooks(client: Client, yes_token_id: str, no_token_id: str):
    """Gets order books for YES and NO tokens."""
    yes_orderbook = None
    no_orderbook = None
    
    try:
        response = client.get_orderbook(token_id=yes_token_id)
        if response.errno == 0:
            yes_orderbook = response.result if hasattr(response.result, 'bids') else getattr(response.result, 'data', response.result)
    except Exception as e:
        logger.error(f"Error getting orderbook for YES: {e}")
    
    try:
        response = client.get_orderbook(token_id=no_token_id)
        if response.errno == 0:
            no_orderbook = response.result if hasattr(response.result, 'bids') else getattr(response.result, 'data', response.result)
    except Exception as e:
        logger.error(f"Error getting orderbook for NO: {e}")
    
    return yes_orderbook, no_orderbook


def calculate_spread_and_liquidity(orderbook, token_name: str) -> dict:
    """Calculates spread and liquidity for a token."""
    if not orderbook:
        return {
            'best_bid': None,
            'best_ask': None,
            'spread': None,
            'spread_pct': None,
            'mid_price': None,
            'bid_liquidity': 0,
            'ask_liquidity': 0,
            'total_liquidity': 0
        }
    
    bids = orderbook.bids if hasattr(orderbook, 'bids') else []
    asks = orderbook.asks if hasattr(orderbook, 'asks') else []
    
    # Extract best bid (highest price)
    best_bid = None
    if bids and len(bids) > 0:
        bid_prices = [float(bid.price) for bid in bids if hasattr(bid, 'price')]
        if bid_prices:
            best_bid = max(bid_prices)  # Highest bid
    
    # Extract best ask (lowest price)
    best_ask = None
    if asks and len(asks) > 0:
        ask_prices = [float(ask.price) for ask in asks if hasattr(ask, 'price')]
        if ask_prices:
            best_ask = min(ask_prices)  # Lowest ask
    
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
        'best_bid': best_bid,
        'best_ask': best_ask,
        'spread': spread,
        'spread_pct': spread_pct,
        'mid_price': mid_price,
        'bid_liquidity': bid_liquidity,
        'ask_liquidity': ask_liquidity,
        'total_liquidity': total_liquidity
    }


def calculate_target_price(current_price: float, side: str, offset_ticks: int, tick_size: float = TICK_SIZE) -> Tuple[float, bool]:
    """
    Calculates target price for limit order.
    
    API requires price range: 0.001 - 0.999 (inclusive)
    """
    MIN_PRICE = 0.001  # Minimum price per API requirements
    MAX_PRICE = 0.999  # Maximum price per API requirements (not 1.0!)
    
    if side == "BUY":
        target = current_price - offset_ticks * tick_size
    else:  # SELL
        target = current_price + offset_ticks * tick_size
    
    # Limit to MIN_PRICE - MAX_PRICE range (0.001 - 0.999)
    target = max(MIN_PRICE, min(MAX_PRICE, target))
    is_valid = MIN_PRICE <= target <= MAX_PRICE
    target = round(target, 3)
    
    # Check that after rounding the price is still in valid range
    if target < MIN_PRICE:
        target = MIN_PRICE
        is_valid = True
    elif target > MAX_PRICE:
        target = MAX_PRICE
        is_valid = True
    
    return target, is_valid


async def check_usdt_balance(client: Client, required_amount: float) -> Tuple[bool, dict]:
    """Checks if USDT balance is sufficient."""
    try:
        response = client.get_my_balances()
        
        if response.errno != 0:
            return False, {}
        
        balance_data = response.result if not hasattr(response.result, 'data') else response.result.data
        
        available = 0.0
        if hasattr(balance_data, 'balances') and balance_data.balances:
            for balance in balance_data.balances:
                available += float(getattr(balance, 'available_balance', 0))
        elif hasattr(balance_data, 'available_balance'):
            available = float(balance_data.available_balance)
        elif hasattr(balance_data, 'available'):
            available = float(balance_data.available)
        
        return available >= required_amount, balance_data
    except Exception as e:
        logger.error(f"Error checking balance: {e}")
        return False, {}


async def place_order(client: Client, order_params: dict) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Places an order on the market.
    
    Returns:
        Tuple[bool, Optional[str], Optional[str]]: (success, order_id, error_message)
    """
    try:
        client.enable_trading()
        
        price = float(order_params['price'])
        price_rounded = round(price, 3)  # API requires max 3 decimal places
        
        # Additional validation: API requires range 0.001 - 0.999 (inclusive)
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
            marketId=order_params['market_id'],
            tokenId=order_params['token_id'],
            side=order_params['side'],
            orderType=LIMIT_ORDER,
            price=str(price_rounded),
            makerAmountInQuoteToken=order_params['amount']
        )
        
        # Обертываем синхронный вызов API в asyncio.to_thread, чтобы не блокировать event loop
        def _place_order_sync():
            return client.place_order(order_data, check_approval=True)
        
        result = await asyncio.to_thread(_place_order_sync)
        
        if result.errno == 0:
            order_id = 'N/A'
            if hasattr(result, 'result'):
                if hasattr(result.result, 'order_data'):
                    order_data_obj = result.result.order_data
                    if hasattr(order_data_obj, 'order_id'):
                        order_id = order_data_obj.order_id
                    elif hasattr(order_data_obj, 'id'):
                        order_id = order_data_obj.id
            
            return True, str(order_id), None
        else:
            error_msg = result.errmsg if hasattr(result, 'errmsg') and result.errmsg else f"Error code: {result.errno}"
            logger.error(f"Error placing order: {error_msg}")
            return False, None, error_msg
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error placing order: {error_msg}")
        return False, None, error_msg


# ============================================================================
# Router and handlers
# ============================================================================

market_router = Router()


@market_router.message(Command("make_market"))
async def cmd_make_market(message: Message, state: FSMContext):
    """Handler for /make_market command - start of order placement process."""
    user = await get_user(message.from_user.id)
    
    if not user:
        await message.answer(
            """❌ You are not registered. Use the /start command to register."""
        )
        return
    
    # Create keyboard with "Cancel" button
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Cancel", callback_data="cancel")
    
    await message.answer(
        """📊 Place a Limit Order

Please enter the Opinion.trade market link:""",
        reply_markup=builder.as_markup()
    )
    await state.set_state(MarketOrderStates.waiting_url)


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
            reply_markup=builder.as_markup()
        )
        return
    
    is_categorical = market_type == "multi"
    
    # Get user data and create client
    user = await get_user(message.from_user.id)
    client = create_client(user)
    
    # Get market information
    await message.answer("""📊 Getting market information...""")
    market = await get_market_info(client, market_id, is_categorical)
    
    if not market:
        await message.answer("""❌ Failed to get market information. Please check the URL.""")
        await state.clear()
        return
    
    # If this is a categorical market, need to select submarket
    if is_categorical:
        submarkets = get_categorical_market_submarkets(market)
        
        if not submarkets:
            await message.answer("""❌ Failed to find submarkets in the categorical market""")
            await state.clear()
            return
        
        # Build submarket list for selection
        submarket_list = []
        for i, subm in enumerate(submarkets, 1):
            submarket_id = getattr(subm, 'market_id', getattr(subm, 'id', None))
            title = getattr(subm, 'market_title', getattr(subm, 'title', getattr(subm, 'name', f'Submarket {i}')))
            submarket_list.append({
                'id': submarket_id,
                'title': title,
                'data': subm
            })
        
        # Save submarket list and client to state
        await state.update_data(submarkets=submarket_list, client=client)
        
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
            reply_markup=builder.as_markup()
        )
        await state.set_state(MarketOrderStates.waiting_submarket)
        return
    
    # For regular market continue as usual
    # Get order books
    yes_token_id = getattr(market, 'yes_token_id', None)
    no_token_id = getattr(market, 'no_token_id', None)
    
    if not yes_token_id or not no_token_id:
        await message.answer("""❌ Failed to determine market tokens""")
        await state.clear()
        return
    
    # Save client to state
    await state.update_data(client=client)
    
    # Continue processing regular market
    await process_market_data(message, state, market, market_id, client, yes_token_id, no_token_id)


async def process_market_data(message: Message, state: FSMContext, market, market_id: int, 
                              client: Client, yes_token_id: str, no_token_id: str):
    """Processes market data and continues order placement process."""
    yes_orderbook, no_orderbook = await get_orderbooks(client, yes_token_id, no_token_id)
    
    # Check if order books have orders
    yes_has_orders = yes_orderbook and hasattr(yes_orderbook, 'bids') and hasattr(yes_orderbook, 'asks') and (len(yes_orderbook.bids) > 0 or len(yes_orderbook.asks) > 0)
    no_has_orders = no_orderbook and hasattr(no_orderbook, 'bids') and hasattr(no_orderbook, 'asks') and (len(no_orderbook.bids) > 0 or len(no_orderbook.asks) > 0)
    
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
        client=client
    )
    
    # Format market information in new format
    market_info_parts = []
    
    # Information for YES token
    if yes_info['best_bid'] is not None or yes_info['best_ask'] is not None:
        yes_bid = f"{yes_info['best_bid'] * 100:.2f}¢" if yes_info['best_bid'] is not None else "no"
        yes_ask = f"{yes_info['best_ask'] * 100:.2f}¢" if yes_info['best_ask'] is not None else "no"
        yes_lines = [f"✅ YES: Bid: {yes_bid} | Ask: {yes_ask}"]
        
        if yes_info['spread']:
            spread_line = f"  Spread: {yes_info['spread'] * 100:.2f}¢ ({yes_info['spread_pct']:.2f}%) | Liquidity: ${yes_info['total_liquidity']:,.2f}"
            yes_lines.append(spread_line)
        elif yes_info['total_liquidity'] > 0:
            yes_lines.append(f"  Liquidity: ${yes_info['total_liquidity']:,.2f}")
        
        market_info_parts.append("\n".join(yes_lines))
    
    # Information for NO token
    if no_info['best_bid'] is not None or no_info['best_ask'] is not None:
        no_bid = f"{no_info['best_bid'] * 100:.2f}¢" if no_info['best_bid'] is not None else "no"
        no_ask = f"{no_info['best_ask'] * 100:.2f}¢" if no_info['best_ask'] is not None else "no"
        no_lines = [f"❌ NO: Bid: {no_bid} | Ask: {no_ask}"]
        
        if no_info['spread']:
            spread_line = f"  Spread: {no_info['spread'] * 100:.2f}¢ ({no_info['spread_pct']:.2f}%) | Liquidity: ${no_info['total_liquidity']:,.2f}"
            no_lines.append(spread_line)
        elif no_info['total_liquidity'] > 0:
            no_lines.append(f"  Liquidity: ${no_info['total_liquidity']:,.2f}")
        
        market_info_parts.append("\n".join(no_lines))
    
    # Create keyboard with "Cancel" button
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Cancel", callback_data="cancel")
    
    # Format full message with empty line between blocks
    market_info_text = "\n\n".join(market_info_parts) if market_info_parts else ""
    
    await message.answer(
        f"""📋 Market Found: {market.market_title}
📊 Market ID: {market_id}

{market_info_text}

💰 Enter the amount for farming (in USDT, e.g. 10):""",
        reply_markup=builder.as_markup()
    )
    await state.set_state(MarketOrderStates.waiting_amount)


@market_router.callback_query(F.data.startswith("submarket_"), MarketOrderStates.waiting_submarket)
async def process_submarket(callback: CallbackQuery, state: FSMContext):
    """Handles submarket selection in categorical market."""
    try:
        submarket_index = int(callback.data.split("_")[1]) - 1
        
        data = await state.get_data()
        submarkets = data.get('submarkets', [])
        
        if submarket_index < 0 or submarket_index >= len(submarkets):
            await callback.message.edit_text("""❌ Invalid submarket selection""")
            await state.clear()
            await callback.answer()
            return
        
        selected_submarket = submarkets[submarket_index]
        submarket_id = selected_submarket['id']
        
        if not submarket_id:
            await callback.message.edit_text("""❌ Failed to determine submarket ID""")
            await state.clear()
            await callback.answer()
            return
        
        # Get full information about selected submarket
        client = data['client']
        await callback.message.edit_text(f"""📊 Getting submarket information: {selected_submarket['title']}...""")
        
        market = await get_market_info(client, submarket_id, is_categorical=False)
        
        if not market:
            await callback.message.edit_text("""❌ Failed to get submarket information""")
            await state.clear()
            await callback.answer()
            return
        
        # Get submarket tokens
        yes_token_id = getattr(market, 'yes_token_id', None)
        no_token_id = getattr(market, 'no_token_id', None)
        
        if not yes_token_id or not no_token_id:
            await callback.message.edit_text("""❌ Failed to determine submarket tokens""")
            await state.clear()
            await callback.answer()
            return
        
        await callback.answer()
        
        # Continue processing as regular market
        await process_market_data(callback.message, state, market, submarket_id, client, yes_token_id, no_token_id)
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
                reply_markup=builder.as_markup()
            )
            return
        
        data = await state.get_data()
        client = data['client']
        
        # Check balance
        has_balance, _ = await check_usdt_balance(client, amount)
        
        if not has_balance:
            builder = InlineKeyboardBuilder()
            builder.button(text="✖️ Cancel", callback_data="cancel")
            await message.answer(
                f"""❌ Insufficient USDT balance to place an order for {amount} USDT.

Enter a different amount:""",
                reply_markup=builder.as_markup()
            )
            return
        
        await state.update_data(amount=amount)
        
        # Create keyboard for side selection
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ YES", callback_data="side_yes")
        builder.button(text="❌ NO", callback_data="side_no")
        builder.button(text="✖️ Cancel", callback_data="cancel")
        builder.adjust(2)
        
        await message.answer(
            f"""✅ USDT balance is sufficient to place a BUY order for {amount} USDT

📈 Select side:""",
            reply_markup=builder.as_markup()
        )
        await state.set_state(MarketOrderStates.waiting_side)
    except ValueError:
        builder = InlineKeyboardBuilder()
        builder.button(text="✖️ Cancel", callback_data="cancel")
        await message.answer(
            """❌ Invalid amount format. Enter a number:""",
            reply_markup=builder.as_markup()
        )


@market_router.callback_query(F.data.startswith("side_"), MarketOrderStates.waiting_side)
async def process_side(callback: CallbackQuery, state: FSMContext):
    """Handles side selection (YES/NO)."""
    side = callback.data.split("_")[1].upper()
    
    data = await state.get_data()
    
    if side == "YES":
        token_id = data['yes_token_id']
        token_name = "YES"
        current_price = data['yes_info']['mid_price']
        orderbook = data.get('yes_orderbook')
    else:
        token_id = data['no_token_id']
        token_name = "NO"
        current_price = data['no_info']['mid_price']
        orderbook = data.get('no_orderbook')
    
    if not current_price:
        await callback.message.answer("❌ Failed to determine current price for selected token")
        await state.clear()
        await callback.answer()
        return
    
    if not orderbook:
        await callback.message.answer("❌ Failed to get orderbook for selected token")
        await state.clear()
        await callback.answer()
        return
    
    # Extract bids and asks from orderbook
    bids = orderbook.bids if hasattr(orderbook, 'bids') else []
    asks = orderbook.asks if hasattr(orderbook, 'asks') else []
    
    # Sort bids by descending price (highest first)
    sorted_bids = []
    if bids and len(bids) > 0:
        for bid in bids:
            if hasattr(bid, 'price'):
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
            if hasattr(ask, 'price'):
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
        await callback.message.answer("❌ No bids found in orderbook")
        await state.clear()
        await callback.answer()
        return
    
    # Calculate maximum tick values for BUY and SELL
    tick_size = TICK_SIZE
    MIN_PRICE = 0.001
    MAX_PRICE = 0.999
    
    # For BUY: so price doesn't become < MIN_PRICE (0.001)
    max_offset_buy = int((current_price - MIN_PRICE) / tick_size)
    
    # For SELL: so price doesn't become > MAX_PRICE (0.999)
    max_offset_sell = int((MAX_PRICE - current_price) / tick_size)
    
    min_offset = 0
    
    await state.update_data(
        token_id=token_id,
        token_name=token_name,
        current_price=current_price,
        tick_size=tick_size,
        max_offset_buy=max_offset_buy,
        max_offset_sell=max_offset_sell,
        best_bid=best_bid
    )
    
    # Format text with best bids
    bids_text = "Best 5 bids:\n"
    for i, bid_price in enumerate(best_bids, 1):
        bids_text += f"{i}. {bid_price:.1f} ¢\n"
    if last_bid and last_bid not in best_bids:
        bids_text += f"...\n{last_bid:.1f} ¢\n"
    
    # Format text with best asks
    asks_text = "Best 5 asks:\n"
    for i, ask_price in enumerate(best_asks, 1):
        asks_text += f"{i}. {ask_price:.1f} ¢\n"
    if last_ask and last_ask not in best_asks:
        asks_text += f"...\n{last_ask:.1f} ¢\n"
    
    # Create keyboard with "Cancel" button
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Cancel", callback_data="cancel")
    
    await callback.message.edit_text(
        f"""✅ Selected: {token_name}

💵 Current price: {current_price:.6f} ({current_price * 100:.2f}¢)

{bids_text}
{asks_text}
Set the price offset (in ¢) relative to the best bid ({best_bid:.1f}¢). For example 0.1:""",
        reply_markup=builder.as_markup()
    )
    await callback.answer()
    await state.set_state(MarketOrderStates.waiting_offset_ticks)


@market_router.message(MarketOrderStates.waiting_offset_ticks)
async def process_offset_ticks(message: Message, state: FSMContext):
    """
    Handles offset input in cents.
    User enters offset in cents, we convert to ticks for validation and further processing.
    """
    try:
        offset_cents = float(message.text.strip())
        
        data = await state.get_data()
        best_bid = data.get('best_bid')
        current_price = data['current_price']
        tick_size = data.get('tick_size', TICK_SIZE)
        max_offset_buy = data.get('max_offset_buy', 0)
        max_offset_sell = data.get('max_offset_sell', 0)
        
        if not best_bid:
            await message.answer("❌ Error: best bid not found")
            await state.clear()
            return
        
        # Convert offset in cents to ticks
        offset_ticks = int(round(offset_cents / (100 * tick_size)))
        
        # Validation: check value is in valid range
        builder = InlineKeyboardBuilder()
        builder.button(text="✖️ Cancel", callback_data="cancel")
        
        min_offset = 0
        if offset_ticks < min_offset:
            await message.answer(
                f"❌ Offset must be at least {min_offset} cents.\n"
                f"Enter a value from {min_offset} to {max(max_offset_buy, max_offset_sell) * tick_size * 100:.1f} cents:",
                reply_markup=builder.as_markup()
            )
            return
        
        # Check maximum value (take max of BUY and SELL)
        max_offset = max(max_offset_buy, max_offset_sell)
        max_offset_cents = max_offset * tick_size * 100
        
        if offset_ticks > max_offset:
            await message.answer(
                f"❌ Offset is too large!\n\n"
                f"• Maximum for BUY: {max_offset_buy * tick_size * 100:.1f} cents\n"
                f"• Maximum for SELL: {max_offset_sell * tick_size * 100:.1f} cents\n\n"
                f"Enter a value from {min_offset} to {max_offset_cents:.1f} cents:",
                reply_markup=builder.as_markup()
            )
            return
        
        await state.update_data(offset_ticks=offset_ticks)
        
        # Create keyboard for direction selection
        builder = InlineKeyboardBuilder()
        
        # Check if BUY direction is valid with this number of ticks
        if offset_ticks <= max_offset_buy:
            builder.button(text="📈 BUY (buy, below current price)", callback_data="dir_buy")
        
        # Check if SELL direction is valid with this number of ticks
        if offset_ticks <= max_offset_sell:
            builder.button(text="📉 SELL (sell, above current price)", callback_data="dir_sell")
        
        builder.button(text="✖️ Cancel", callback_data="cancel")
        builder.adjust(1)
        
        # If no direction is available (shouldn't happen after validation)
        if not builder.buttons:
            await message.answer(
                f"❌ Error: Offset {offset_cents:.1f} cents is invalid for both directions.\n"
                f"Enter a value from {min_offset} to {max_offset_cents:.1f} cents:"
            )
            return
        
        # Convert prices to cents for display
        current_price_cents = current_price * 100
        tick_size_cents = tick_size * 100
        
        # Format without trailing zeros
        current_price_str = f"{current_price_cents:.2f}".rstrip('0').rstrip('.')
        tick_size_str = f"{tick_size_cents:.2f}".rstrip('0').rstrip('.')
        
        await message.answer(
            f"""✅ Offset: {offset_cents:.1f}¢ ({offset_ticks} ticks)

📊 Settings:
• Current price: {current_price_str}¢
• Tick size: {tick_size_str}¢

Select order direction:""",
            reply_markup=builder.as_markup()
        )
        await state.set_state(MarketOrderStates.waiting_direction)
    except ValueError:
        data = await state.get_data()
        tick_size = data.get('tick_size', TICK_SIZE)
        max_offset_buy = data.get('max_offset_buy', 0)
        max_offset_sell = data.get('max_offset_sell', 0)
        max_offset = max(max_offset_buy, max_offset_sell)
        max_offset_cents = max_offset * tick_size * 100
        builder = InlineKeyboardBuilder()
        builder.button(text="✖️ Cancel", callback_data="cancel")
        await message.answer(
            f"❌ Invalid format. Enter a number from 0 to {max_offset_cents:.1f} cents:",
            reply_markup=builder.as_markup()
        )


@market_router.callback_query(F.data.startswith("dir_"), MarketOrderStates.waiting_direction)
async def process_direction(callback: CallbackQuery, state: FSMContext):
    """Handles direction selection (BUY/SELL)."""
    direction = callback.data.split("_")[1].upper()
    
    data = await state.get_data()
    current_price = data['current_price']
    offset_ticks = data['offset_ticks']
    tick_size = data.get('tick_size', TICK_SIZE)
    token_name = data['token_name']
    max_offset_buy = data.get('max_offset_buy', 0)
    max_offset_sell = data.get('max_offset_sell', 0)
    
    # Additional validation: check offset is valid for selected direction
    if direction == "BUY" and offset_ticks > max_offset_buy:
        await callback.message.answer(
            f"""❌ Error: Offset {offset_ticks} ticks is too large for BUY!

Maximum for BUY: {max_offset_buy} ticks"""
        )
        await state.clear()
        await callback.answer()
        return
    
    if direction == "SELL" and offset_ticks > max_offset_sell:
        await callback.message.answer(
            f"""❌ Error: Offset {offset_ticks} ticks is too large for SELL!

Maximum for SELL: {max_offset_sell} ticks"""
        )
        await state.clear()
        await callback.answer()
        return
    
    # Calculate target price
    target_price, is_valid = calculate_target_price(current_price, direction, offset_ticks, tick_size)
    
    if not is_valid or target_price <= 0:
        await callback.message.answer(
            f"""❌ Error: Calculated price ({target_price:.6f}) is invalid!

Offset {offset_ticks} ticks is too large for current price {current_price:.6f}"""
        )
        await state.clear()
        await callback.answer()
        return
    
    order_side = OrderSide.BUY if direction == "BUY" else OrderSide.SELL
    
    await state.update_data(
        direction=direction,
        order_side=order_side,
        target_price=target_price
    )
    
    # Ask for reposition threshold
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Cancel", callback_data="cancel")
    
    await callback.message.edit_text(
        """⚙️ <b>Reposition Threshold</b>

Enter the price deviation threshold (in cents) for repositioning the order.

For example:
• <code>0.5</code> - reposition when price changes by 0.5 cents or more
• <code>1.0</code> - reposition when price changes by 1 cent or more
• <code>2.0</code> - reposition when price changes by 2 cents or more

Recommended: <code>0.5</code> cents

Enter the threshold:""",
        reply_markup=builder.as_markup()
    )
    await callback.answer()
    await state.set_state(MarketOrderStates.waiting_reposition_threshold)


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
        """Use the /make_market command to start a new farm.
Use the /orders command to manage your orders.
Use the /support command to contact administrator."""
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
                reply_markup=builder.as_markup()
            )
            return
        
        # Save threshold to state
        await state.update_data(reposition_threshold_cents=threshold_cents)
        
        # Get all data for confirmation
        data = await state.get_data()
        market = data['market']
        token_name = data['token_name']
        direction = data['direction']
        current_price = data['current_price']
        target_price = data['target_price']
        offset_ticks = data['offset_ticks']
        amount = data['amount']
        tick_size = data.get('tick_size', TICK_SIZE)
        
        # Convert offset from ticks to cents
        offset_cents = offset_ticks * tick_size * 100
        
        # Convert prices to cents and remove trailing zeros
        current_price_cents = current_price * 100
        target_price_cents = target_price * 100
        
        # Format prices without trailing zeros
        current_price_str = f"{current_price_cents:.2f}".rstrip('0').rstrip('.')
        target_price_str = f"{target_price_cents:.2f}".rstrip('0').rstrip('.')
        offset_cents_str = f"{offset_cents:.2f}".rstrip('0').rstrip('.')
        
        confirm_text = (
            f"""📋 <b>Settings Confirmation</b>

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
        )
        
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
            reply_markup=builder.as_markup()
        )


@market_router.callback_query(F.data.startswith("confirm_"), MarketOrderStates.waiting_confirm)
async def process_confirm(callback: CallbackQuery, state: FSMContext):
    """Handles order placement confirmation."""
    confirm = callback.data.split("_")[1]
    
    if confirm != "yes":
        await callback.message.edit_text("""❌ Order placement cancelled""")
        await state.clear()
        await callback.answer()
        return
    
    data = await state.get_data()
    client = data['client']
    
    order_params = {
        'market_id': data['market_id'],
        'token_id': data['token_id'],
        'side': data['order_side'],
        'price': str(data['target_price']),
        'amount': data['amount'],
        'token_name': data['token_name']
    }
    
    await callback.message.edit_text("""🔄 Placing order...""")
    
    success, order_id, error_message = await place_order(client, order_params)
    
    if success:
        # Save order to database
        try:
            telegram_id = callback.from_user.id
            market_id = data['market_id']
            market = data.get('market')
            market_title = getattr(market, 'market_title', None) if market else None
            token_id = data['token_id']
            token_name = data['token_name']
            side = data['direction']  # BUY or SELL
            current_price = data['current_price']
            target_price = data['target_price']
            offset_ticks = data['offset_ticks']
            tick_size = data.get('tick_size', TICK_SIZE)
            offset_cents = offset_ticks * tick_size * 100
            amount = data['amount']
            reposition_threshold_cents = data.get('reposition_threshold_cents', 0.5)
            
            # Сохраняем ордер в базу данных
            await save_order(
                telegram_id=telegram_id,
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
                status='pending',
                reposition_threshold_cents=reposition_threshold_cents
            )
            logger.info(f"Order {order_id} successfully saved to DB for user {telegram_id}")
        except Exception as e:
            logger.error(f"Error saving order to DB: {e}")
        
        await callback.message.edit_text(
            f"""✅ <b>Order successfully placed!</b>

📋 <b>Final Information:</b>
• Side: {data['direction']} {data['token_name']}
• Price: {data['target_price']:.6f}
• Amount: {data['amount']} USDT
• Offset: {offset_cents:.2f}¢
• Reposition threshold: {reposition_threshold_cents:.2f}¢
• Order ID: <code>{order_id}</code>"""
        )
    else:
        error_text = f"""❌ <b>Failed to place order</b>

{error_message if error_message else 'Please check your balance and order parameters.'}"""
        await callback.message.edit_text(error_text)
    
    await state.clear()
    await callback.answer()

