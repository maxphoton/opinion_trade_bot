"""
Модуль для проверки работоспособности прокси.
Содержит функции для валидации формата, проверки работоспособности и фоновой задачи.
"""

import asyncio
import logging
from typing import Optional, Tuple

import httpx
from service.database import (
    get_all_users,
    get_opinion_account,
    get_user_accounts,
    update_proxy_status,
)

logger = logging.getLogger(__name__)


def validate_proxy_format(proxy_str: str) -> Tuple[bool, str]:
    """
    Валидирует формат прокси строки.

    Args:
        proxy_str: Строка прокси в формате ip:port:login:password

    Returns:
        Tuple[bool, str]: (is_valid, error_message)
    """
    if not proxy_str:
        return False, "Прокси не указан"

    if not isinstance(proxy_str, str):
        return False, "Прокси должен быть строкой"

    parts = proxy_str.split(":")
    if len(parts) != 4:
        return (
            False,
            f"Неверный формат прокси. Ожидается ip:port:login:password, получено {len(parts)} частей",
        )

    ip, port_str, login, password = parts

    # Проверяем IP
    if not ip or not ip.strip():
        return False, "IP адрес не может быть пустым"

    # Проверяем порт
    try:
        port = int(port_str)
        if port < 1 or port > 65535:
            return False, f"Порт должен быть в диапазоне 1-65535, получено {port}"
    except ValueError:
        return False, f"Порт должен быть числом, получено: {port_str}"

    # Проверяем логин и пароль
    if not login or not login.strip():
        return False, "Логин не может быть пустым"

    if not password or not password.strip():
        return False, "Пароль не может быть пустым"

    return True, ""


def parse_proxy(proxy_str: str) -> Optional[dict]:
    """
    Парсит строку прокси формата ip:port:login:password.

    Args:
        proxy_str: Строка прокси в формате ip:port:login:password

    Returns:
        Словарь с ключами: {'host': str, 'port': int, 'username': str, 'password': str}
        Или None в случае ошибки
    """
    is_valid, error_message = validate_proxy_format(proxy_str)
    if not is_valid:
        logger.warning(f"Ошибка валидации прокси: {error_message}")
        return None

    try:
        parts = proxy_str.split(":")
        ip, port_str, username, password = parts

        return {
            "host": ip,
            "port": int(port_str),
            "username": username,
            "password": password,
        }
    except Exception as e:
        logger.error(f"Ошибка при парсинге прокси: {e}")
        return None


async def check_proxy_health(proxy_str: str, timeout: float = 10.0) -> str:
    """
    Проверяет работоспособность прокси.

    Args:
        proxy_str: Прокси в формате ip:port:login:password
        timeout: Таймаут для проверки в секундах (по умолчанию 10)

    Returns:
        str: Статус прокси ('working' или 'failed')
    """
    parsed = parse_proxy(proxy_str)
    if not parsed:
        logger.error(f"Не удалось распарсить прокси для проверки: {proxy_str}")
        return "failed"

    host = parsed["host"]
    port = parsed["port"]
    username = parsed["username"]
    password = parsed["password"]

    async def _attempt_check() -> str:
        """Попытка выполнить проверку прокси один раз."""
        try:
            proxy_url_with_auth = f"http://{username}:{password}@{host}:{port}"

            async with httpx.AsyncClient(
                proxy=proxy_url_with_auth,
                timeout=timeout,
            ) as client:
                response = await client.get("http://httpbin.org/ip", timeout=timeout)
                if response.status_code == 200:
                    logger.info(f"✅ Прокси {host}:{port} работает")
                    return "working"

                logger.warning(
                    f"❌ Прокси {host}:{port} вернул статус {response.status_code}"
                )
                return "failed"
        except httpx.TimeoutException:
            logger.warning(f"⏱️ Таймаут при проверке прокси {host}:{port}")
            return "failed"
        except httpx.ProxyError as e:
            logger.warning(f"❌ Ошибка прокси {host}:{port}: {e}")
            return "failed"
        except Exception as e:
            logger.error(f"❌ Ошибка при проверке прокси {host}:{port}: {e}")
            return "failed"

    retry_delays = [3, 5, 10]
    for attempt in range(len(retry_delays) + 1):
        status = await _attempt_check()
        if status == "working":
            return "working"
        if attempt >= len(retry_delays):
            return "failed"

        wait = retry_delays[attempt]
        logger.info(f"⏳ Повторная проверка прокси {host}:{port} через {wait} сек.")
        await asyncio.sleep(wait)


async def check_account_proxy(account_id: int, bot=None) -> Optional[str]:
    """
    Проверяет прокси для аккаунта.

    Args:
        account_id: ID аккаунта Opinion
        bot: Экземпляр aiogram Bot для отправки уведомлений (опционально)

    Returns:
        str: Статус прокси ('working' или 'failed') или None в случае ошибки
    """
    account = await get_opinion_account(account_id)
    if not account:
        logger.warning(f"Аккаунт {account_id} не найден")
        return None

    proxy_str = account.get("proxy_str")
    if not proxy_str:
        logger.info(f"У аккаунта {account_id} не настроен прокси")
        return None

    telegram_id = account["telegram_id"]
    old_status = account.get("proxy_status", "unknown")

    logger.info(
        f"Проверка прокси для аккаунта {account_id} (пользователь {telegram_id})"
    )

    # Проверяем прокси
    new_status = await check_proxy_health(proxy_str)

    # Обновляем статус в БД
    await update_proxy_status(account_id, new_status)

    # Отправляем уведомления при изменении статуса
    if bot and old_status != new_status:
        try:
            if old_status == "working" and new_status == "failed":
                # Прокси перестал работать
                message = f"""⚠️ <b>Proxy is not working</b>

Proxy for Opinion profile has stopped working.

Account: {account["wallet_address"][:10]}...

Proxy status: <b>failed</b>

Orders for this account will not be synchronized until the proxy is restored.

The proxy will be automatically checked every 10 minutes."""
                await bot.send_message(
                    chat_id=telegram_id, text=message, parse_mode="HTML"
                )
                logger.info(
                    f"Отправлено уведомление пользователю {telegram_id} о неработающем прокси"
                )
            elif old_status == "failed" and new_status == "working":
                # Прокси восстановился
                message = f"""✅ <b>Proxy restored</b>

Proxy for Opinion profile is working again.

Account: {account["wallet_address"][:10]}...

Proxy status: <b>working</b>

Order synchronization has been resumed."""
                await bot.send_message(
                    chat_id=telegram_id, text=message, parse_mode="HTML"
                )
                logger.info(
                    f"Отправлено уведомление пользователю {telegram_id} о восстановлении прокси"
                )
        except Exception as e:
            logger.error(
                f"Ошибка при отправке уведомления пользователю {telegram_id}: {e}"
            )

    return new_status


async def async_check_all_proxies(bot=None):
    """
    Фоновая задача для проверки всех прокси.

    Проверяет все аккаунты с настроенным прокси и обновляет их статусы.
    Отправляет уведомления пользователям при изменении статуса с 'working' на 'failed'.

    Args:
        bot: Экземпляр aiogram Bot для отправки уведомлений (опционально)
    """
    logger.info("Начало проверки всех прокси")

    # Получаем всех пользователей
    users = await get_all_users()
    total_accounts = 0
    checked_accounts = 0
    failed_accounts = 0

    for telegram_id in users:
        accounts = await get_user_accounts(telegram_id)
        for account in accounts:
            total_accounts += 1
            if account.get("proxy_str"):
                checked_accounts += 1
                status = await check_account_proxy(account["account_id"], bot)
                if status == "failed":
                    failed_accounts += 1
                # Небольшая задержка между проверками, чтобы не перегружать систему
                await asyncio.sleep(1)

    logger.info(
        f"Проверка прокси завершена: всего аккаунтов {total_accounts}, "
        f"с прокси {checked_accounts}, неработающих {failed_accounts}"
    )
