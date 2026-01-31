"""
Фабрика для создания клиентов Opinion SDK.
"""

import base64
import logging
from typing import Optional

from opinion_api.api.prediction_market_api import PredictionMarketApi
from opinion_api.api.user_api import UserApi
from opinion_api.api_client import ApiClient
from opinion_clob_sdk import Client
from service.config import settings

logger = logging.getLogger(__name__)


def parse_proxy(proxy_str: str) -> Optional[dict]:
    """
    Парсит строку прокси формата ip:port:login:password и возвращает конфигурацию прокси.

    Формат прокси: ip:port:login:password
    Пример: 91.216.186.156:8000:Ym81H9:ysZcvQ

    Args:
        proxy_str: Строка прокси в формате ip:port:login:password

    Returns:
        Словарь с ключами:
        - proxy_url: URL прокси без аутентификации (http://ip:port)
        - proxy_headers: Заголовки для аутентификации прокси
        Или None в случае ошибки
    """
    if not proxy_str:
        return None

    try:
        parts = proxy_str.split(":")
        if len(parts) != 4:
            logger.warning(
                f"Неверный формат прокси: {proxy_str}. Ожидается ip:port:login:password"
            )
            return None

        ip, port, username, password = parts

        # Формируем URL прокси БЕЗ аутентификации
        proxy_url = f"http://{ip}:{port}"

        # Формируем заголовки для базовой аутентификации
        credentials = f"{username}:{password}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()
        proxy_headers = {"Proxy-Authorization": f"Basic {encoded_credentials}"}

        return {"proxy_url": proxy_url, "proxy_headers": proxy_headers}
    except Exception as e:
        logger.error(f"Ошибка при парсинге прокси: {e}")
        return None


def create_client(account_data: dict) -> Client:
    """
    Создает клиент Opinion SDK из данных аккаунта.
    Настраивает прокси в конфигурации SDK для всех API запросов.

    Важно: SDK использует urllib3, который НЕ использует переменные окружения
    HTTP_PROXY/HTTPS_PROXY автоматически. Прокси нужно устанавливать напрямую
    в configuration.proxy перед созданием ApiClient.

    Args:
        account_data: Словарь с данными аккаунта (wallet_address, private_key, api_key, proxy_str)
        proxy_str обязателен

    Returns:
        Client: Настроенный клиент Opinion SDK

    Raises:
        ValueError: Если не удалось распарсить прокси
    """
    # Создаем клиент
    client = Client(
        host="https://proxy.opinion.trade:8443",
        apikey=account_data["api_key"],
        chain_id=56,  # BNB Chain mainnet
        rpc_url=settings.rpc_url,
        private_key=account_data["private_key"],
        multi_sig_addr=account_data["wallet_address"],
        conditional_tokens_addr=settings.conditional_token_addr,
        multisend_addr=settings.multisend_addr,
        market_cache_ttl=0,  # Cache markets for 5 minutes
        quote_tokens_cache_ttl=3600,  # Cache quote tokens for 1 hour
        enable_trading_check_interval=3600,  # Check trading every hour
    )

    use_proxy = False
    if use_proxy:
        # Устанавливаем прокси в конфигурацию SDK
        proxy_str = account_data["proxy_str"]
        proxy_config = parse_proxy(proxy_str)
        if not proxy_config:
            raise ValueError(f"Не удалось распарсить прокси: {proxy_str}")

        # Устанавливаем прокси URL БЕЗ аутентификации
        client.conf.proxy = proxy_config["proxy_url"]
        # Устанавливаем заголовки для аутентификации прокси
        client.conf.proxy_headers = proxy_config["proxy_headers"]

        # Логируем успешную установку прокси в SDK (без пароля)
        proxy_info = proxy_config["proxy_url"].replace("http://", "")
        logger.info(f"✅ Прокси установлен в конфигурацию SDK: {proxy_info}")

    # Пересоздаем api_client с новой конфигурацией
    # Это необходимо, так как RESTClientObject создается при инициализации ApiClient
    client.api_client = ApiClient(client.conf)
    client.market_api = PredictionMarketApi(client.api_client)
    client.user_api = UserApi(client.api_client)

    return client
