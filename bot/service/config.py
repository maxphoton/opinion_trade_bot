"""
Настройки для телеграм бота.
"""

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки бота из переменных окружения."""

    bot_token: str
    master_key: str  # 32 bytes hex для шифрования
    rpc_url: str  # URL RPC ноды BNB Chain
    admin_telegram_id: int = 0  # ID администратора для команды /get_db

    # Прокси для всех API запросов (опционально)
    # Формат: host:port:username:password (например: 91.216.186.156:8000:Ym81H9:ysZcvQ)
    proxy: Optional[str] = None

    # Опциональные параметры для Opinion SDK
    conditional_token_addr: str = "0xAD1a38cEc043e70E83a3eC30443dB285ED10D774"
    multisend_addr: str = "0x998739BFdAAdde7C933B942a68053933098f9EDa"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # Игнорировать лишние переменные из .env
    )


settings = Settings()

# Константы для работы с рынками
TICK_SIZE = 0.001  # Размер тика для цен на Opinion.trade

# Адрес контракта USDT на BNB Chain
USDT_CONTRACT_ADDRESS = "0x55d398326f99059ff775485246999027b3197955"
