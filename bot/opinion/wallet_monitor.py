"""
Wallet monitoring task for Opinion Open API.
"""

import asyncio
import logging
import math
from typing import Any, Dict, List, Optional

from aiogram import Bot
from opinion.helper import get_market_url
from opinion.open_api import get_user_positions, get_user_trades
from service.database import (
    get_user_accounts,
    get_wallet_monitors,
    update_wallet_monitor_orders_count,
    update_wallet_monitor_runtime,
)

logger = logging.getLogger(__name__)

API_LIMIT = 20


def _calculate_last_page(total: int, limit: int) -> int:
    if total <= 0:
        return 1
    return max(1, math.ceil(total / limit))


def _extract_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_trade_message(label: str, address: str, trades: List[Dict[str, Any]]) -> str:
    message = (
        "🔔 <b>New trades detected</b>\n\n"
        f"Label: <b>{label}</b>\n"
        f"Wallet: <code>{address}</code>\n\n"
    )
    for trade in trades:
        market_title = trade.get("marketTitle") or "Unknown market"
        market_id = trade.get("marketId")
        root_market_id = trade.get("rootMarketId")
        market_url = get_market_url(market_id, root_market_id)
        outcome = trade.get("outcome") or trade.get("outcomeSideEnum") or "N/A"
        side = trade.get("side") or "N/A"
        amount = trade.get("amount") or "N/A"

        message += (
            f"• <b>{market_title}</b>\n"
            f'🔗 <a href="{market_url}">Market link</a>\n'
            f"✅ Outcome: {outcome}\n"
            f"🧭 Side: {side}\n"
            f"💵 Amount: {amount}\n\n"
        )
    return message.strip()


def _build_position_message(
    label: str, address: str, positions: List[Dict[str, Any]]
) -> str:
    message = (
        "🔔 <b>New positions detected</b>\n\n"
        f"Label: <b>{label}</b>\n"
        f"Wallet: <code>{address}</code>\n\n"
    )
    for position in positions:
        market_title = position.get("marketTitle") or "Unknown market"
        market_id = position.get("marketId")
        root_market_id = position.get("rootMarketId")
        market_url = get_market_url(market_id, root_market_id)
        outcome = position.get("outcome") or position.get("outcomeSideEnum") or "N/A"
        amount = position.get("sharesOwned") or "N/A"

        message += (
            f"• <b>{market_title}</b>\n"
            f'🔗 <a href="{market_url}">Market link</a>\n'
            f"✅ Outcome: {outcome}\n"
            f"💵 Amount: {amount}\n\n"
        )
    return message.strip()


async def _fetch_last_page_trades(
    api_key: str, address: str
) -> tuple[int, List[Dict[str, Any]]]:
    logger.info("Wallet monitor: fetch trades for %s", address)
    response = await get_user_trades(
        api_key=api_key, wallet_address=address, page=1, limit=API_LIMIT
    )
    if not response or response.get("errno") not in (0, None):
        logger.warning(
            "Wallet monitor: trades response error for %s: %s",
            address,
            response,
        )
        return 0, []

    result = response.get("result", {})
    total = _extract_int(result.get("total", 0))
    last_page = _calculate_last_page(total, API_LIMIT)
    logger.info(
        "Wallet monitor: trades total=%s last_page=%s for %s",
        total,
        last_page,
        address,
    )

    if last_page == 1:
        return total, result.get("list", []) or []

    response = await get_user_trades(
        api_key=api_key, wallet_address=address, page=last_page, limit=API_LIMIT
    )
    if not response or response.get("errno") not in (0, None):
        logger.warning(
            "Wallet monitor: trades last page error for %s: %s",
            address,
            response,
        )
        return total, []

    result = response.get("result", {})
    return total, result.get("list", []) or []


async def _fetch_last_page_positions(
    api_key: str, address: str
) -> tuple[int, List[Dict[str, Any]]]:
    logger.info("Wallet monitor: fetch positions for %s", address)
    response = await get_user_positions(
        api_key=api_key, wallet_address=address, page=1, limit=API_LIMIT
    )
    if not response or response.get("errno") not in (0, None):
        logger.warning(
            "Wallet monitor: positions response error for %s: %s",
            address,
            response,
        )
        return 0, []

    result = response.get("result", {})
    total = _extract_int(result.get("total", 0))
    last_page = _calculate_last_page(total, API_LIMIT)
    logger.info(
        "Wallet monitor: positions total=%s last_page=%s for %s",
        total,
        last_page,
        address,
    )

    if last_page == 1:
        return total, result.get("list", []) or []

    response = await get_user_positions(
        api_key=api_key, wallet_address=address, page=last_page, limit=API_LIMIT
    )
    if not response or response.get("errno") not in (0, None):
        logger.warning(
            "Wallet monitor: positions last page error for %s: %s",
            address,
            response,
        )
        return total, []

    result = response.get("result", {})
    return total, result.get("list", []) or []


async def _process_monitor_record(bot: Bot, record: Dict[str, Any]) -> None:
    monitor_id = record["id"]
    address = record["address"]
    label = record["label"]
    telegram_id = record["tguserid"]
    last_runtime = _extract_int(record.get("lastruntime", 0))
    last_count_orders = _extract_int(record.get("lastcountorders", 0))

    logger.info(
        "Wallet monitor: start record id=%s user=%s address=%s label=%s last_runtime=%s last_count=%s",
        monitor_id,
        telegram_id,
        address,
        label,
        last_runtime,
        last_count_orders,
    )

    accounts = await get_user_accounts(telegram_id)
    if not accounts:
        logger.warning(
            "Skip wallet monitor for user %s: no Opinion profiles", telegram_id
        )
        return

    api_key = accounts[0]["api_key"]
    logger.info(
        "Wallet monitor: using api key from account %s for user %s",
        accounts[0].get("account_id"),
        telegram_id,
    )

    total_trades, trades = await _fetch_last_page_trades(api_key, address)
    new_trades = []
    newest_runtime: Optional[int] = None
    for trade in trades:
        created_at = _extract_int(trade.get("createdAt"))
        if created_at > last_runtime:
            new_trades.append(trade)
            if newest_runtime is None or created_at > newest_runtime:
                newest_runtime = created_at

    if new_trades:
        new_trades.sort(key=lambda t: _extract_int(t.get("createdAt")))
        message = _build_trade_message(label, address, new_trades)
        await bot.send_message(chat_id=telegram_id, text=message)
        logger.info(
            "Wallet monitor: sent %s trade(s) to user %s for %s",
            len(new_trades),
            telegram_id,
            address,
        )
        if newest_runtime is not None:
            await update_wallet_monitor_runtime(monitor_id, newest_runtime)
            logger.info(
                "Wallet monitor: updated lastruntime=%s for record %s",
                newest_runtime,
                monitor_id,
            )
    else:
        logger.info(
            "Wallet monitor: no new trades for %s (last_runtime=%s)",
            address,
            last_runtime,
        )

    total_positions, positions = await _fetch_last_page_positions(api_key, address)
    if last_count_orders == 0:
        await update_wallet_monitor_orders_count(monitor_id, total_positions)
        logger.info(
            "Wallet monitor: init lastcountorders=%s for record %s",
            total_positions,
            monitor_id,
        )
        return

    if total_positions > last_count_orders:
        new_count = total_positions - last_count_orders
        if new_count < len(positions):
            positions = positions[-new_count:]
        message = _build_position_message(label, address, positions)
        await bot.send_message(chat_id=telegram_id, text=message)
        logger.info(
            "Wallet monitor: sent %s position(s) to user %s for %s",
            len(positions),
            telegram_id,
            address,
        )
    else:
        logger.info(
            "Wallet monitor: no new positions for %s (last_count=%s, total=%s)",
            address,
            last_count_orders,
            total_positions,
        )

    if total_positions != last_count_orders:
        await update_wallet_monitor_orders_count(monitor_id, total_positions)
        logger.info(
            "Wallet monitor: updated lastcountorders=%s for record %s",
            total_positions,
            monitor_id,
        )


async def monitor_wallets_cycle(bot: Bot) -> None:
    """Runs a single monitoring cycle for all tracked wallets."""
    records = await get_wallet_monitors()
    logger.info("Wallet monitor: cycle start (%s records)", len(records))
    for record in records:
        await _process_monitor_record(bot, record)
        await asyncio.sleep(0.2)
    logger.info("Wallet monitor: cycle complete")
