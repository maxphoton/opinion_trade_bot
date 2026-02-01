"""
Shared helper functions for router workflows.
"""

from typing import Optional, Tuple

from aiogram.utils.keyboard import InlineKeyboardBuilder
from service.config import TICK_SIZE


def build_cancel_keyboard(callback_data: str) -> InlineKeyboardBuilder:
    """Creates a keyboard with a single cancel button."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Cancel", callback_data=callback_data)
    return builder


def format_orderbook_levels(orderbook) -> tuple[str, str]:
    """Formats best bids and asks into text blocks."""
    bids = orderbook.bids if hasattr(orderbook, "bids") else []
    asks = orderbook.asks if hasattr(orderbook, "asks") else []

    sorted_bids = []
    for bid in bids or []:
        if hasattr(bid, "price"):
            try:
                price = float(bid.price)
                sorted_bids.append(price)
            except (ValueError, TypeError):
                continue
    sorted_bids.sort(reverse=True)

    sorted_asks = []
    for ask in asks or []:
        if hasattr(ask, "price"):
            try:
                price = float(ask.price)
                sorted_asks.append(price)
            except (ValueError, TypeError):
                continue
    sorted_asks.sort()

    best_bids = [price * 100 for price in sorted_bids[:5]]
    best_asks = [price * 100 for price in sorted_asks[:5]]

    last_bid = sorted_bids[-1] * 100 if sorted_bids else None
    last_ask = sorted_asks[-1] * 100 if sorted_asks else None

    bids_text = "<b>Best 5 bids:</b>\n"
    for i, bid_price in enumerate(best_bids, 1):
        bids_text += f"{i}. {bid_price:.1f} ¢\n"
    if last_bid and last_bid not in best_bids:
        bids_text += f"...\n{last_bid:.1f} ¢\n"

    asks_text = "<b>Best 5 asks:</b>\n"
    for i, ask_price in enumerate(best_asks, 1):
        asks_text += f"{i}. {ask_price:.1f} ¢\n"
    if last_ask and last_ask not in best_asks:
        asks_text += f"...\n{last_ask:.1f} ¢\n"

    return bids_text, asks_text


def calculate_target_price(
    current_price: float, side: str, offset_ticks: int, tick_size: float = TICK_SIZE
) -> Tuple[float, bool]:
    """
    Calculates target price for limit order.

    API requires price range: 0.001 - 0.999 (inclusive)
    """
    MIN_PRICE = 0.001
    MAX_PRICE = 0.999

    if side == "BUY":
        target = current_price - offset_ticks * tick_size
    else:
        target = current_price + offset_ticks * tick_size

    target = max(MIN_PRICE, min(MAX_PRICE, target))
    is_valid = MIN_PRICE <= target <= MAX_PRICE
    target = round(target, 3)

    if target < MIN_PRICE:
        target = MIN_PRICE
        is_valid = True
    elif target > MAX_PRICE:
        target = MAX_PRICE
        is_valid = True

    return target, is_valid


def get_offset_bounds(
    direction: Optional[str], max_offset_buy: int, max_offset_sell: int
) -> Tuple[int, int]:
    """Returns direction-aware min/max offset bounds (in ticks)."""
    if direction == "BUY":
        min_offset = -max_offset_sell
        max_offset = max_offset_buy
    elif direction == "SELL":
        min_offset = -max_offset_buy
        max_offset = max_offset_sell
    else:
        min_offset = -max(max_offset_buy, max_offset_sell)
        max_offset = max(max_offset_buy, max_offset_sell)

    return min_offset, max_offset
