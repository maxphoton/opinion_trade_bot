"""
Фабрика для создания клиентов Opinion SDK.
"""

import base64
import logging
import os
from typing import Optional

from config import settings
from opinion_api.api.prediction_market_api import PredictionMarketApi
from opinion_api.api.user_api import UserApi
from opinion_api.api_client import ApiClient
from opinion_clob_sdk import Client

logger = logging.getLogger(__name__)


def parse_proxy_config() -> Optional[dict]:
    """
    Парсит строку прокси формата host:port:username:password и возвращает конфигурацию прокси.

    Формат прокси: host:port:username:password
    Пример: 91.216.186.156:8000:Ym81H9:ysZcvQ

    Returns:
        Словарь с ключами:
        - proxy_url: URL прокси без аутентификации (http://host:port)
        - proxy_headers: Заголовки для аутентификации прокси
    """
    proxy_str = settings.proxy or os.getenv("PROXY")

    if not proxy_str:
        return None

    try:
        parts = proxy_str.split(":")
        if len(parts) != 4:
            logger.warning(
                f"Неверный формат прокси: {proxy_str}. Ожидается host:port:username:password"
            )
            return None

        host, port, username, password = parts

        # Формируем URL прокси БЕЗ аутентификации
        proxy_url = f"http://{host}:{port}"

        # Формируем заголовки для базовой аутентификации
        credentials = f"{username}:{password}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()
        proxy_headers = {"Proxy-Authorization": f"Basic {encoded_credentials}"}

        return {"proxy_url": proxy_url, "proxy_headers": proxy_headers}
    except Exception as e:
        logger.error(f"Ошибка при парсинге прокси: {e}")
        return None


def get_proxy_url() -> Optional[str]:
    """
    Парсит строку прокси и возвращает полный URL с аутентификацией.
    Используется для переменных окружения (httpx, requests).

    Returns:
        URL прокси в формате http://username:password@host:port или None
    """
    proxy_config = parse_proxy_config()
    if not proxy_config:
        return None

    # Для переменных окружения формируем полный URL с аутентификацией
    proxy_str = settings.proxy or os.getenv("PROXY")
    if proxy_str:
        parts = proxy_str.split(":")
        if len(parts) == 4:
            host, port, username, password = parts
            return f"http://{username}:{password}@{host}:{port}"

    return None


def setup_proxy():
    """
    Централизованная настройка прокси для всех API запросов.
    Устанавливает переменные окружения HTTP_PROXY и HTTPS_PROXY для совместимости
    с другими библиотеками (httpx, requests), хотя SDK использует urllib3 напрямую.
    """
    proxy_url = get_proxy_url()

    if proxy_url:
        # Устанавливаем переменные окружения для использования другими библиотеками
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        os.environ["http_proxy"] = (
            proxy_url  # Некоторые библиотеки используют нижний регистр
        )
        os.environ["https_proxy"] = proxy_url
    else:
        logger.info("ℹ️ Прокси не настроен, запросы идут напрямую")


def create_client(user_data: dict) -> Client:
    """
    Создает клиент Opinion SDK из данных пользователя.
    Настраивает прокси в конфигурации SDK для всех API запросов.

    Важно: SDK использует urllib3, который НЕ использует переменные окружения
    HTTP_PROXY/HTTPS_PROXY автоматически. Прокси нужно устанавливать напрямую
    в configuration.proxy перед созданием ApiClient.

    Args:
        user_data: Словарь с данными пользователя (wallet_address, private_key, api_key)

    Returns:
        Client: Настроенный клиент Opinion SDK
    """
    # Создаем клиент
    client = Client(
        host="https://proxy.opinion.trade:8443",
        apikey=user_data["api_key"],
        chain_id=56,  # BNB Chain mainnet
        rpc_url=settings.rpc_url,
        private_key=user_data["private_key"],
        multi_sig_addr=user_data["wallet_address"],
        conditional_tokens_addr=settings.conditional_token_addr,
        multisend_addr=settings.multisend_addr,
        market_cache_ttl=0,  # Cache markets for 5 minutes
        quote_tokens_cache_ttl=3600,  # Cache quote tokens for 1 hour
        enable_trading_check_interval=3600,  # Check trading every hour
    )

    # Устанавливаем прокси в конфигурацию SDK
    # SDK использует urllib3, который требует явной установки прокси в configuration
    # Для аутентификации прокси нужно использовать proxy_headers, а не встраивать в URL
    proxy_config = parse_proxy_config()
    if proxy_config:
        # Устанавливаем прокси URL БЕЗ аутентификации
        client.conf.proxy = proxy_config["proxy_url"]
        # Устанавливаем заголовки для аутентификации прокси
        client.conf.proxy_headers = proxy_config["proxy_headers"]

        # Пересоздаем api_client с новой конфигурацией (с прокси)
        # Это необходимо, так как RESTClientObject создается при инициализации ApiClient
        client.api_client = ApiClient(client.conf)
        client.market_api = PredictionMarketApi(client.api_client)
        client.user_api = UserApi(client.api_client)

        # Логируем успешную установку прокси в SDK (без пароля)
        proxy_info = proxy_config["proxy_url"].replace("http://", "")
        logger.info(f"✅ Прокси установлен в конфигурацию SDK: {proxy_info}")

    return client
