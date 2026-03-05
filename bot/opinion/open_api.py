"""
Opinion Open API helpers for wallet monitoring.
"""

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

OPEN_API_BASE_URL = "https://proxy.opinion.trade:8443/openapi"
DEFAULT_TIMEOUT = 15.0


async def _get_open_api_json(
    endpoint: str,
    api_key: str,
    params: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Performs GET request to Opinion Open API and returns JSON response."""
    url = f"{OPEN_API_BASE_URL}{endpoint}"
    headers = {
        "apikey": api_key,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            errno = data.get("errno")
            if errno not in (0, None):
                logger.warning(
                    "Open API error response: errno=%s data=%s",
                    errno,
                    data,
                )
            return data
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Open API HTTP error: %s %s (status=%s)",
            exc.request.method,
            exc.request.url,
            exc.response.status_code,
        )
    except httpx.RequestError as exc:
        logger.warning("Open API request error: %s", exc)
    except ValueError as exc:
        logger.warning("Open API JSON parse error: %s", exc)
    except Exception as exc:
        logger.error("Open API unexpected error: %s", exc)

    return None


async def get_user_trades(
    api_key: str,
    wallet_address: str,
    page: int = 1,
    limit: int = 20,
) -> Optional[Dict[str, Any]]:
    """Fetches user trades from Open API."""
    params = {"page": page, "limit": limit}
    return await _get_open_api_json(
        endpoint=f"/trade/user/{wallet_address}",
        api_key=api_key,
        params=params,
    )


async def get_user_positions(
    api_key: str,
    wallet_address: str,
    page: int = 1,
    limit: int = 20,
) -> Optional[Dict[str, Any]]:
    """Fetches user positions from Open API."""
    params = {"page": page, "limit": limit}
    return await _get_open_api_json(
        endpoint=f"/positions/user/{wallet_address}",
        api_key=api_key,
        params=params,
    )
