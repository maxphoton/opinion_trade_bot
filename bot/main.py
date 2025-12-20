"""
Телеграм бот для размещения лимитных ордеров на Opinion.trade.

Алгоритм работы:
1. Команда /start - регистрация (кошелек, приватный ключ, API ключ)
2. Данные шифруются и сохраняются в SQLite
3. Команда /make_market - размещение ордера (логика из simple_flow.py)
"""

import asyncio
import base64
import csv
import io
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse, parse_qs

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from opinion_clob_sdk import Client
from opinion_clob_sdk.sdk import InvalidParamError, OpenApiError
from opinion_clob_sdk.chain.py_order_utils.model.order import PlaceOrderDataInput
from opinion_clob_sdk.chain.py_order_utils.model.sides import OrderSide
from opinion_clob_sdk.chain.py_order_utils.model.order_type import LIMIT_ORDER
from opinion_clob_sdk.chain.exception import BalanceNotEnough

# Импортируем локальные модули
from aes import encrypt, decrypt
from config import settings

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(token=settings.bot_token)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# Путь к базе данных SQLite (в той же папке, что и скрипт)
DB_PATH = Path(__file__).parent / "users.db"


# ============================================================================
# Состояния FSM для регистрации
# ============================================================================

class RegistrationStates(StatesGroup):
    """Состояния процесса регистрации."""
    waiting_wallet = State()
    waiting_private_key = State()
    waiting_api_key = State()


class MarketOrderStates(StatesGroup):
    """Состояния процесса размещения ордера."""
    waiting_url = State()
    waiting_submarket = State()  # Для выбора подрынка в категориальных рынках
    waiting_amount = State()
    waiting_side = State()
    waiting_offset_ticks = State()
    waiting_direction = State()
    waiting_confirm = State()


# ============================================================================
# Функции для работы с базой данных
# ============================================================================

def init_database():
    """Инициализирует базу данных SQLite."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            wallet_address TEXT NOT NULL,
            wallet_nonce BLOB NOT NULL,
            private_key_cipher BLOB NOT NULL,
            private_key_nonce BLOB NOT NULL,
            api_key_cipher BLOB NOT NULL,
            api_key_nonce BLOB NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")


def get_user(telegram_id: int) -> Optional[dict]:
    """Получает данные пользователя из базы данных."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute(
        "SELECT * FROM users WHERE telegram_id = ?",
        (telegram_id,)
    )
    
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return None
    
    # Расшифровываем данные
    try:
        wallet_address = decrypt(row[2], row[3])
        private_key = decrypt(row[4], row[5])
        api_key = decrypt(row[6], row[7])
        
        return {
            'telegram_id': row[0],
            'username': row[1],
            'wallet_address': wallet_address,
            'private_key': private_key,
            'api_key': api_key,
        }
    except Exception as e:
        logger.error(f"Ошибка расшифровки данных пользователя {telegram_id}: {e}")
        return None


def save_user(telegram_id: int, username: Optional[str], wallet_address: str, 
              private_key: str, api_key: str):
    """Сохраняет данные пользователя в базу данных с шифрованием."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Шифруем данные
    wallet_cipher, wallet_nonce = encrypt(wallet_address)
    private_key_cipher, private_key_nonce = encrypt(private_key)
    api_key_cipher, api_key_nonce = encrypt(api_key)
    
    # Сохраняем или обновляем пользователя
    cursor.execute("""
        INSERT OR REPLACE INTO users 
        (telegram_id, username, wallet_address, wallet_nonce, 
         private_key_cipher, private_key_nonce, api_key_cipher, api_key_nonce)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        telegram_id, username, wallet_cipher, wallet_nonce,
        private_key_cipher, private_key_nonce, api_key_cipher, api_key_nonce
    ))
    
    conn.commit()
    conn.close()
    logger.info(f"Пользователь {telegram_id} сохранен в базу данных")


def export_users_to_csv() -> str:
    """
    Экспортирует таблицу users в CSV формат.
    
    Returns:
        str: CSV содержимое в виде строки
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Получаем все данные из таблицы users
    cursor.execute("SELECT * FROM users")
    rows = cursor.fetchall()
    
    # Получаем названия колонок
    column_names = [description[0] for description in cursor.description]
    
    conn.close()
    
    # Создаем CSV в памяти
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Записываем заголовки
    writer.writerow(column_names)
    
    # Записываем данные
    # Примечание: BLOB данные (шифрованные ключи) будут представлены как hex строки
    for row in rows:
        csv_row = []
        for value in row:
            if isinstance(value, bytes):
                # Конвертируем BLOB в hex строку для читаемости
                csv_row.append(value.hex())
            else:
                csv_row.append(value)
        writer.writerow(csv_row)
    
    return output.getvalue()


# ============================================================================
# Функции для работы с Opinion SDK (адаптированы из simple_flow.py)
# ============================================================================

def parse_proxy_config() -> Optional[dict]:
    """
    Парсит строку прокси формата host:port:username:password и возвращает конфигурацию прокси.
    
    Формат прокси: host:port:username:password
    Пример: 91.216.186.156:8000:Ym81H9:ysZcvQ
    
    Returns:
        Словарь с ключами:
        - proxy_url: URL прокси без аутентификации (http://host:port)
        - proxy_headers: Заголовки для аутентификации прокси
        Или None, если прокси не настроен
    """
    # Читаем прокси из настроек или переменных окружения
    proxy_str = settings.proxy or os.getenv('PROXY')
    
    if not proxy_str:
        return None
    
    try:
        # Парсим строку формата host:port:username:password
        parts = proxy_str.split(':')
        if len(parts) != 4:
            raise ValueError(f"Неверный формат прокси. Ожидается host:port:username:password, получено: {proxy_str}")
        
        host, port, username, password = parts
        
        # Формируем URL прокси БЕЗ аутентификации (urllib3 требует отдельные заголовки)
        proxy_url = f"http://{host}:{port}"
        
        # Создаем заголовок для базовой аутентификации прокси
        # urllib3.ProxyManager использует заголовок Proxy-Authorization для аутентификации
        credentials = f"{username}:{password}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()
        proxy_headers = {
            'Proxy-Authorization': f'Basic {encoded_credentials}'
        }
        
        # Логируем без пароля для безопасности
        logger.info(f"✅ Настроен прокси: {username}@{host}:{port}")
        
        return {
            'proxy_url': proxy_url,
            'proxy_headers': proxy_headers
        }
        
    except Exception as e:
        logger.error(f"❌ Ошибка парсинга прокси: {e}")
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
    proxy_str = settings.proxy or os.getenv('PROXY')
    if proxy_str:
        parts = proxy_str.split(':')
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
        os.environ['HTTP_PROXY'] = proxy_url
        os.environ['HTTPS_PROXY'] = proxy_url
        os.environ['http_proxy'] = proxy_url  # Некоторые библиотеки используют нижний регистр
        os.environ['https_proxy'] = proxy_url
    else:
        logger.info("ℹ️ Прокси не настроен, запросы идут напрямую")


def create_client(user_data: dict) -> Client:
    """
    Создает клиент Opinion SDK из данных пользователя.
    Настраивает прокси в конфигурации SDK для всех API запросов.
    
    Важно: SDK использует urllib3, который НЕ использует переменные окружения
    HTTP_PROXY/HTTPS_PROXY автоматически. Прокси нужно устанавливать напрямую
    в configuration.proxy перед созданием ApiClient.
    """
    # Создаем клиент
    client = Client(
        host='https://proxy.opinion.trade:8443',
        apikey=user_data['api_key'],
        chain_id=56,  # BNB Chain mainnet
        rpc_url=settings.rpc_url,
        private_key=user_data['private_key'],
        multi_sig_addr=user_data['wallet_address'],
        conditional_tokens_addr=settings.conditional_token_addr,
        multisend_addr=settings.multisend_addr,
        market_cache_ttl=0,        # Cache markets for 5 minutes
        quote_tokens_cache_ttl=3600, # Cache quote tokens for 1 hour
        enable_trading_check_interval=3600 # Check trading every hour
    )
    
    # Устанавливаем прокси в конфигурацию SDK
    # SDK использует urllib3, который требует явной установки прокси в configuration
    # Для аутентификации прокси нужно использовать proxy_headers, а не встраивать в URL
    proxy_config = parse_proxy_config()
    if proxy_config:
        # Устанавливаем прокси URL БЕЗ аутентификации
        client.conf.proxy = proxy_config['proxy_url']
        # Устанавливаем заголовки для аутентификации прокси
        client.conf.proxy_headers = proxy_config['proxy_headers']
        
        # Пересоздаем api_client с новой конфигурацией (с прокси)
        # Это необходимо, так как RESTClientObject создается при инициализации ApiClient
        from opinion_api.api_client import ApiClient
        from opinion_api.api.prediction_market_api import PredictionMarketApi
        from opinion_api.api.user_api import UserApi
        
        client.api_client = ApiClient(client.conf)
        client.market_api = PredictionMarketApi(client.api_client)
        client.user_api = UserApi(client.api_client)
        
        # Логируем успешную установку прокси в SDK (без пароля)
        proxy_info = proxy_config['proxy_url'].replace('http://', '')
        logger.info(f"✅ Прокси установлен в конфигурацию SDK: {proxy_info}")
    
    return client


def parse_market_url(url: str) -> Tuple[Optional[int], Optional[str]]:
    """Парсит URL Opinion.trade и извлекает marketId и тип рынка."""
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
    """Получает информацию о рынке."""
    try:
        if is_categorical:
            response = client.get_categorical_market(market_id=market_id)
        else:
            response = client.get_market(market_id=market_id, use_cache=True)

        if response.errno == 0:
            return response.result.data
        else:
            logger.error(f"Ошибка получения рынка: {response.errmsg} (код: {response.errno})")
            return None
    except Exception as e:
        logger.error(f"Ошибка получения рынка: {e}")
        return None


def get_categorical_market_submarkets(market) -> list:
    """Извлекает список подрынков из категориального рынка."""
    if hasattr(market, 'child_markets') and market.child_markets:
        return market.child_markets
    return []


async def get_orderbooks(client: Client, yes_token_id: str, no_token_id: str):
    """Получает стаканы ордеров для YES и NO токенов."""
    yes_orderbook = None
    no_orderbook = None
    
    try:
        response = client.get_orderbook(token_id=yes_token_id)
        if response.errno == 0:
            yes_orderbook = response.result if hasattr(response.result, 'bids') else getattr(response.result, 'data', response.result)
    except Exception as e:
        logger.error(f"Ошибка получения стакана для YES: {e}")
    
    try:
        response = client.get_orderbook(token_id=no_token_id)
        if response.errno == 0:
            no_orderbook = response.result if hasattr(response.result, 'bids') else getattr(response.result, 'data', response.result)
    except Exception as e:
        logger.error(f"Ошибка получения стакана для NO: {e}")
    
    return yes_orderbook, no_orderbook


def calculate_spread_and_liquidity(orderbook, token_name: str) -> dict:
    """Рассчитывает спред и ликвидность для токена."""
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
    
    # Отладочный вывод: проверяем структуру данных (первые 5 элементов)
    if bids and len(bids) > 0:
        logger.debug(f"[DEBUG {token_name}] Первые 5 bids:")
        for i, bid in enumerate(bids[:5]):
            logger.debug(f"  bids[{i}]: price={bid.price if hasattr(bid, 'price') else 'N/A'}, size={bid.size if hasattr(bid, 'size') else 'N/A'}")
    
    if asks and len(asks) > 0:
        logger.debug(f"[DEBUG {token_name}] Первые 5 asks:")
        for i, ask in enumerate(asks[:5]):
            logger.debug(f"  asks[{i}]: price={ask.price if hasattr(ask, 'price') else 'N/A'}, size={ask.size if hasattr(ask, 'size') else 'N/A'}")
    
    # Извлекаем лучший бид (самый высокий)
    # Бид должен быть самым высоким, но на всякий случай ищем максимум
    best_bid = None
    if bids and len(bids) > 0:
        bid_prices = [float(bid.price) for bid in bids if hasattr(bid, 'price')]
        if bid_prices:
            best_bid = max(bid_prices)  # Самый высокий бид
    
    # Извлекаем лучший аск (самый низкий)
    # Аски могут быть не отсортированы, поэтому ищем минимум
    best_ask = None
    if asks and len(asks) > 0:
        ask_prices = [float(ask.price) for ask in asks if hasattr(ask, 'price')]
        if ask_prices:
            best_ask = min(ask_prices)  # Самый низкий аск
    
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


def calculate_target_price(current_price: float, side: str, offset_ticks: int, tick_size: float = 0.001) -> Tuple[float, bool]:
    """
    Рассчитывает целевую цену для лимитного ордера.
    
    API требует диапазон цены: 0.001 - 0.999 (включительно)
    """
    MIN_PRICE = 0.001  # Минимальная цена по требованиям API
    MAX_PRICE = 0.999  # Максимальная цена по требованиям API (не 1.0!)
    
    if side == "BUY":
        target = current_price - offset_ticks * tick_size
    else:  # SELL
        target = current_price + offset_ticks * tick_size
    
    # Ограничиваем диапазоном MIN_PRICE - MAX_PRICE (0.001 - 0.999)
    target = max(MIN_PRICE, min(MAX_PRICE, target))
    is_valid = MIN_PRICE <= target <= MAX_PRICE
    target = round(target, 3)
    
    # Проверяем, что после округления цена все еще в допустимом диапазоне
    if target < MIN_PRICE:
        target = MIN_PRICE
        is_valid = True
    elif target > MAX_PRICE:
        target = MAX_PRICE
        is_valid = True
    
    return target, is_valid


async def check_usdt_balance(client: Client, required_amount: float) -> Tuple[bool, dict]:
    """Проверяет достаточность USDT баланса."""
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
        logger.error(f"Ошибка проверки баланса: {e}")
        return False, {}


async def place_order(client: Client, order_params: dict) -> Tuple[bool, Optional[str]]:
    """Размещает ордер на рынке."""
    try:
        client.enable_trading()
        
        price = float(order_params['price'])
        price_rounded = round(price, 3)  # API требует максимум 3 знака
        
        # Дополнительная валидация: API требует диапазон 0.001 - 0.999 (включительно)
        MIN_PRICE = 0.001
        MAX_PRICE = 0.999
        
        if price_rounded < MIN_PRICE:
            logger.error(f"Цена {price_rounded} меньше минимальной {MIN_PRICE}")
            return False, None
        
        if price_rounded > MAX_PRICE:
            logger.error(f"Цена {price_rounded} больше максимальной {MAX_PRICE}")
            return False, None
        
        order_data = PlaceOrderDataInput(
            marketId=order_params['market_id'],
            tokenId=order_params['token_id'],
            side=order_params['side'],
            orderType=LIMIT_ORDER,
            price=str(price_rounded),
            makerAmountInQuoteToken=order_params['amount']
        )
        
        result = client.place_order(order_data, check_approval=True)
        
        if result.errno == 0:
            order_id = 'N/A'
            if hasattr(result, 'result'):
                if hasattr(result.result, 'order_data'):
                    order_data_obj = result.result.order_data
                    if hasattr(order_data_obj, 'order_id'):
                        order_id = order_data_obj.order_id
                    elif hasattr(order_data_obj, 'id'):
                        order_id = order_data_obj.id
            
            return True, str(order_id)
        else:
            logger.error(f"Ошибка размещения ордера: {result.errmsg}")
            return False, None
    except Exception as e:
        logger.error(f"Ошибка размещения ордера: {e}")
        return False, None


# ============================================================================
# Обработчики команд
# ============================================================================

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Обработчик команды /start - начало регистрации."""
    user = get_user(message.from_user.id)
    
    if user:
        await message.answer(
            "✅ Вы уже зарегистрированы!\n\n"
            "Используйте команду /make_market для размещения ордера."
        )
        return
    
    await message.answer(
        "🔐 <b>Регистрация в боте</b>\n\n"
        "⚠️ <b>Внимание:</b> Все данные (кошелек, приватный ключ, API ключ) "
        "шифруются с помощью закрытого ключа и хранятся в зашифрованном виде.\n"
        "Данные не используются в первоначальном виде и не передаются третьим лицам.\n\n"
        "Введите адрес вашего кошелька Opinion.trade:",
        parse_mode="HTML"
    )
    await state.set_state(RegistrationStates.waiting_wallet)


@router.message(RegistrationStates.waiting_wallet)
async def process_wallet(message: Message, state: FSMContext):
    """Обработка ввода адреса кошелька."""
    wallet_address = message.text.strip()
    
    if not wallet_address or len(wallet_address) < 10:
        await message.answer("❌ Неверный формат адреса кошелька. Попробуйте еще раз:")
        return
    
    await state.update_data(wallet_address=wallet_address)
    await message.answer("Введите ваш приватный ключ:")
    await state.set_state(RegistrationStates.waiting_private_key)


@router.message(RegistrationStates.waiting_private_key)
async def process_private_key(message: Message, state: FSMContext):
    """Обработка ввода приватного ключа."""
    private_key = message.text.strip()
    
    if not private_key or len(private_key) < 20:
        await message.answer("❌ Неверный формат приватного ключа. Попробуйте еще раз:")
        return
    
    await state.update_data(private_key=private_key)
    await message.answer("Введите ваш API ключ от Opinion Labs:")
    await state.set_state(RegistrationStates.waiting_api_key)


@router.message(RegistrationStates.waiting_api_key)
async def process_api_key(message: Message, state: FSMContext):
    """Обработка ввода API ключа и завершение регистрации."""
    api_key = message.text.strip()
    
    if not api_key:
        await message.answer("❌ Неверный формат API ключа. Попробуйте еще раз:")
        return
    
    data = await state.get_data()
    
    # Сохраняем пользователя в базу данных
    save_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        wallet_address=data['wallet_address'],
        private_key=data['private_key'],
        api_key=api_key
    )
    
    await state.clear()
    await message.answer(
        "✅ <b>Регистрация завершена!</b>\n\n"
        "Ваши данные сохранены в зашифрованном виде.\n\n"
        "Используйте команду /make_market для размещения ордера.",
        parse_mode="HTML"
    )


@router.message(Command("make_market"))
async def cmd_make_market(message: Message, state: FSMContext):
    """Обработчик команды /make_market - начало процесса размещения ордера."""
    user = get_user(message.from_user.id)
    
    if not user:
        await message.answer(
            "❌ Вы не зарегистрированы. Используйте команду /start для регистрации."
        )
        return
    
    # Создаем клавиатуру с кнопкой "Отменить"
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отменить", callback_data="cancel")
    
    await message.answer(
        "📊 <b>Размещение лимитного ордера</b>\n\n"
        "Введите ссылку на рынок Opinion.trade:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await state.set_state(MarketOrderStates.waiting_url)


@router.message(Command("get_db"))
async def cmd_get_db(message: Message):
    """Обработчик команды /get_db - экспорт базы данных в CSV (только для администратора)."""
    # Проверяем права администратора
    if message.from_user.id != settings.admin_telegram_id:
        await message.answer("❌ У вас нет прав для выполнения этой команды.")
        return
    
    try:
        # Экспортируем данные в CSV
        csv_content = export_users_to_csv()
        
        # Создаем файл для отправки
        csv_file = BufferedInputFile(
            csv_content.encode('utf-8'),
            filename="users_export.csv"
        )
        
        await message.answer_document(
            document=csv_file,
            caption="📊 Экспорт базы данных пользователей"
        )
        logger.info(f"Администратор {message.from_user.id} экспортировал базу данных")
    except Exception as e:
        logger.error(f"Ошибка экспорта базы данных: {e}")
        await message.answer(f"❌ Ошибка при экспорте базы данных: {e}")


@router.message(MarketOrderStates.waiting_url)
async def process_market_url(message: Message, state: FSMContext):
    """Обработка ввода URL рынка."""
    url = message.text.strip()
    market_id, market_type = parse_market_url(url)
    
    if not market_id:
        builder = InlineKeyboardBuilder()
        builder.button(text="❌ Отменить", callback_data="cancel")
        await message.answer(
            "❌ Не удалось извлечь Market ID из URL. Попробуйте еще раз:",
            reply_markup=builder.as_markup()
        )
        return
    
    is_categorical = market_type == "multi"
    
    # Получаем данные пользователя и создаем клиент
    user = get_user(message.from_user.id)
    client = create_client(user)
    
    # Получаем информацию о рынке
    await message.answer("📊 Получение информации о рынке...")
    market = await get_market_info(client, market_id, is_categorical)
    
    if not market:
        await message.answer("❌ Не удалось получить информацию о рынке. Проверьте URL.")
        await state.clear()
        return
    
    # Если это категориальный рынок, нужно выбрать подрынок
    if is_categorical:
        submarkets = get_categorical_market_submarkets(market)
        
        if not submarkets:
            await message.answer("❌ Не удалось найти подрынки в категориальном рынке")
            await state.clear()
            return
        
        # Формируем список подрынков для выбора
        submarket_list = []
        for i, subm in enumerate(submarkets, 1):
            submarket_id = getattr(subm, 'market_id', getattr(subm, 'id', None))
            title = getattr(subm, 'market_title', getattr(subm, 'title', getattr(subm, 'name', f'Подрынок {i}')))
            submarket_list.append({
                'id': submarket_id,
                'title': title,
                'data': subm
            })
        
        # Сохраняем список подрынков и клиент в состояние
        await state.update_data(submarkets=submarket_list, client=client)
        
        # Создаем клавиатуру для выбора подрынка
        builder = InlineKeyboardBuilder()
        for i, subm in enumerate(submarket_list, 1):
            builder.button(text=f"{i}. {subm['title'][:30]}", callback_data=f"submarket_{i}")
        builder.button(text="❌ Отменить", callback_data="cancel")
        builder.adjust(1)
        
        await message.answer(
            f"📋 <b>Категориальный рынок</b>\n\n"
            f"Найдено подрынков: {len(submarket_list)}\n\n"
            f"Выберите подрынок:",
            parse_mode="HTML",
            reply_markup=builder.as_markup()
        )
        await state.set_state(MarketOrderStates.waiting_submarket)
        return
    
    # Для обычного рынка продолжаем как обычно
    # Получаем стаканы ордеров
    yes_token_id = getattr(market, 'yes_token_id', None)
    no_token_id = getattr(market, 'no_token_id', None)
    
    if not yes_token_id or not no_token_id:
        await message.answer("❌ Не удалось определить токены рынка")
        await state.clear()
        return
    
    # Сохраняем клиент в состояние
    await state.update_data(client=client)
    
    # Продолжаем обработку обычного рынка
    await process_market_data(message, state, market, market_id, client, yes_token_id, no_token_id)


async def process_market_data(message: Message, state: FSMContext, market, market_id: int, 
                              client: Client, yes_token_id: str, no_token_id: str):
    """Обрабатывает данные рынка и продолжает процесс размещения ордера."""
    yes_orderbook, no_orderbook = await get_orderbooks(client, yes_token_id, no_token_id)
    
    # Проверяем наличие ордеров в стаканах
    yes_has_orders = yes_orderbook and hasattr(yes_orderbook, 'bids') and hasattr(yes_orderbook, 'asks') and (len(yes_orderbook.bids) > 0 or len(yes_orderbook.asks) > 0)
    no_has_orders = no_orderbook and hasattr(no_orderbook, 'bids') and hasattr(no_orderbook, 'asks') and (len(no_orderbook.bids) > 0 or len(no_orderbook.asks) > 0)
    
    if not yes_has_orders and not no_has_orders:
        await message.answer(
            "⚠️ <b>Рынок неактивен</b>\n\n"
            "В стаканах ордеров нет заявок (bids и asks пустые).\n"
            "Возможные причины:\n"
            "• Рынок истек или закрыт\n"
            "• Рынок еще не начал торговаться\n"
            "• Нет ликвидности на рынке",
            parse_mode="HTML"
        )
        await state.clear()
        return
    
    # Рассчитываем спред и ликвидность
    yes_info = calculate_spread_and_liquidity(yes_orderbook, "YES")
    no_info = calculate_spread_and_liquidity(no_orderbook, "NO")
    
    # Сохраняем данные в состояние
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
    
    # Выводим информацию о рынке
    spread_text = ""
    
    # Информация для YES токена
    if yes_info['best_bid'] is not None or yes_info['best_ask'] is not None:
        yes_bid_text = f"Bid: {yes_info['best_bid'] * 100:.2f}%" if yes_info['best_bid'] is not None else "Bid: нет"
        yes_ask_text = f"Ask: {yes_info['best_ask'] * 100:.2f}%" if yes_info['best_ask'] is not None else "Ask: нет"
        spread_part = f", Спред {yes_info['spread'] * 100:.2f}% ({yes_info['spread_pct']:.2f}%)" if yes_info['spread'] else ""
        spread_text += f"\n✅ YES: {yes_bid_text} | {yes_ask_text}{spread_part}, Ликвидность {yes_info['total_liquidity']:.2f}"
    
    # Информация для NO токена
    if no_info['best_bid'] is not None or no_info['best_ask'] is not None:
        no_bid_text = f"Bid: {no_info['best_bid'] * 100:.2f}%" if no_info['best_bid'] is not None else "Bid: нет"
        no_ask_text = f"Ask: {no_info['best_ask'] * 100:.2f}%" if no_info['best_ask'] is not None else "Ask: нет"
        spread_part = f", Спред {no_info['spread'] * 100:.2f}% ({no_info['spread_pct']:.2f}%)" if no_info['spread'] else ""
        spread_text += f"\n❌ NO: {no_bid_text} | {no_ask_text}{spread_part}, Ликвидность {no_info['total_liquidity']:.2f}"
    
    # Создаем клавиатуру с кнопкой "Отменить"
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отменить", callback_data="cancel")
    
    await message.answer(
        f"✅ <b>Рынок найден:</b> {market.market_title}\n"
        f"📊 Market ID: {market_id}{spread_text}\n\n"
        f"💰 Введите сумму для фарминга (в USDT, например, 10):",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await state.set_state(MarketOrderStates.waiting_amount)


@router.callback_query(F.data.startswith("submarket_"), MarketOrderStates.waiting_submarket)
async def process_submarket(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора подрынка в категориальном рынке."""
    try:
        submarket_index = int(callback.data.split("_")[1]) - 1
        
        data = await state.get_data()
        submarkets = data.get('submarkets', [])
        
        if submarket_index < 0 or submarket_index >= len(submarkets):
            await callback.message.edit_text("❌ Неверный выбор подрынка")
            await state.clear()
            await callback.answer()
            return
        
        selected_submarket = submarkets[submarket_index]
        submarket_id = selected_submarket['id']
        
        if not submarket_id:
            await callback.message.edit_text("❌ Не удалось определить ID подрынка")
            await state.clear()
            await callback.answer()
            return
        
        # Получаем полную информацию о выбранном подрынке
        client = data['client']
        await callback.message.edit_text(f"📊 Получение информации о подрынке: {selected_submarket['title']}...")
        
        market = await get_market_info(client, submarket_id, is_categorical=False)
        
        if not market:
            await callback.message.edit_text("❌ Не удалось получить информацию о подрынке")
            await state.clear()
            await callback.answer()
            return
        
        # Получаем токены подрынка
        yes_token_id = getattr(market, 'yes_token_id', None)
        no_token_id = getattr(market, 'no_token_id', None)
        
        if not yes_token_id or not no_token_id:
            await callback.message.edit_text("❌ Не удалось определить токены подрынка")
            await state.clear()
            await callback.answer()
            return
        
        await callback.answer()
        
        # Продолжаем обработку как для обычного рынка
        await process_market_data(callback.message, state, market, submarket_id, client, yes_token_id, no_token_id)
    except (ValueError, IndexError, KeyError) as e:
        logger.error(f"Ошибка обработки выбора подрынка: {e}")
        await callback.message.edit_text("❌ Ошибка обработки выбора подрынка")
        await state.clear()
        await callback.answer()


@router.message(MarketOrderStates.waiting_amount)
async def process_amount(message: Message, state: FSMContext):
    """Обработка ввода суммы для фарминга."""
    try:
        amount = float(message.text.strip())
        
        if amount <= 0:
            builder = InlineKeyboardBuilder()
            builder.button(text="❌ Отменить", callback_data="cancel")
            await message.answer(
                "❌ Сумма должна быть положительным числом. Попробуйте еще раз:",
                reply_markup=builder.as_markup()
            )
            return
        
        data = await state.get_data()
        client = data['client']
        
        # Проверяем баланс
        has_balance, _ = await check_usdt_balance(client, amount)
        
        if not has_balance:
            builder = InlineKeyboardBuilder()
            builder.button(text="❌ Отменить", callback_data="cancel")
            await message.answer(
                f"❌ Недостаточно USDT баланса для размещения ордера на {amount} USDT.\n"
                "Введите другую сумму:",
                reply_markup=builder.as_markup()
            )
            return
        
        await state.update_data(amount=amount)
        
        # Создаем клавиатуру для выбора стороны
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ YES", callback_data="side_yes")
        builder.button(text="❌ NO", callback_data="side_no")
        builder.button(text="❌ Отменить", callback_data="cancel")
        builder.adjust(2)
        
        await message.answer(
            f"✅ USDT баланс достаточен для размещения BUY ордера на {amount} USDT\n\n"
            "📈 Выберите сторону:",
            reply_markup=builder.as_markup()
        )
        await state.set_state(MarketOrderStates.waiting_side)
    except ValueError:
        builder = InlineKeyboardBuilder()
        builder.button(text="❌ Отменить", callback_data="cancel")
        await message.answer(
            "❌ Неверный формат суммы. Введите число:",
            reply_markup=builder.as_markup()
        )


@router.callback_query(F.data.startswith("side_"), MarketOrderStates.waiting_side)
async def process_side(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора стороны (YES/NO)."""
    side = callback.data.split("_")[1].upper()
    
    data = await state.get_data()
    
    if side == "YES":
        token_id = data['yes_token_id']
        token_name = "YES"
        current_price = data['yes_info']['mid_price']
    else:
        token_id = data['no_token_id']
        token_name = "NO"
        current_price = data['no_info']['mid_price']
    
    if not current_price:
        await callback.message.answer("❌ Не удалось определить текущую цену для выбранного токена")
        await state.clear()
        await callback.answer()
        return
    
    # Рассчитываем максимальные значения тиков для BUY и SELL
    # API требует диапазон цены: 0.001 - 0.999 (включительно)
    tick_size = 0.001
    MIN_PRICE = 0.001
    MAX_PRICE = 0.999
    
    # Для BUY: чтобы цена не стала < MIN_PRICE (0.001)
    max_offset_buy = int((current_price - MIN_PRICE) / tick_size)
    
    # Для SELL: чтобы цена не стала > MAX_PRICE (0.999)
    max_offset_sell = int((MAX_PRICE - current_price) / tick_size)
    
    min_offset = 0
    
    await state.update_data(
        token_id=token_id,
        token_name=token_name,
        current_price=current_price,
        tick_size=tick_size,
        max_offset_buy=max_offset_buy,
        max_offset_sell=max_offset_sell
    )
    
    # Создаем клавиатуру с кнопкой "Отменить"
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отменить", callback_data="cancel")
    
    await callback.message.edit_text(
        f"✅ Выбрано: {token_name}\n\n"
        f"💵 Текущая цена: {current_price:.6f} ({current_price * 100:.2f}%)\n\n"
        f"⚙️ <b>Диапазон тиков:</b>\n"
        f"• Минимум: {min_offset} тиков\n"
        f"• Максимум для BUY: {max_offset_buy} тиков\n"
        f"• Максимум для SELL: {max_offset_sell} тиков\n\n"
        f"Введите количество тиков от текущей цены:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await callback.answer()
    await state.set_state(MarketOrderStates.waiting_offset_ticks)


@router.message(MarketOrderStates.waiting_offset_ticks)
async def process_offset_ticks(message: Message, state: FSMContext):
    """Обработка ввода количества тиков с валидацией."""
    try:
        offset_ticks = int(message.text.strip())
        
        data = await state.get_data()
        min_offset = 0
        max_offset_buy = data.get('max_offset_buy', 0)
        max_offset_sell = data.get('max_offset_sell', 0)
        current_price = data['current_price']
        tick_size = data.get('tick_size', 0.001)
        
        # Валидация: проверяем, что значение в допустимом диапазоне
        builder = InlineKeyboardBuilder()
        builder.button(text="❌ Отменить", callback_data="cancel")
        
        if offset_ticks < min_offset:
            await message.answer(
                f"❌ Количество тиков должно быть не меньше {min_offset}.\n"
                f"Введите значение от {min_offset} до {max(max_offset_buy, max_offset_sell)}:",
                reply_markup=builder.as_markup()
            )
            return
        
        # Проверяем максимальное значение (берем максимум из BUY и SELL)
        max_offset = max(max_offset_buy, max_offset_sell)
        if offset_ticks > max_offset:
            await message.answer(
                f"❌ Количество тиков слишком большое!\n\n"
                f"• Максимум для BUY: {max_offset_buy} тиков\n"
                f"• Максимум для SELL: {max_offset_sell} тиков\n\n"
                f"Введите значение от {min_offset} до {max_offset}:",
                reply_markup=builder.as_markup()
            )
            return
        
        await state.update_data(offset_ticks=offset_ticks)
        
        # Создаем клавиатуру для выбора направления
        builder = InlineKeyboardBuilder()
        
        # Проверяем, допустимо ли направление BUY с таким количеством тиков
        if offset_ticks <= max_offset_buy:
            builder.button(text="📈 BUY (покупка, ниже текущей цены)", callback_data="dir_buy")
        
        # Проверяем, допустимо ли направление SELL с таким количеством тиков
        if offset_ticks <= max_offset_sell:
            builder.button(text="📉 SELL (продажа, выше текущей цены)", callback_data="dir_sell")
        
        builder.button(text="❌ Отменить", callback_data="cancel")
        builder.adjust(1)
        
        # Если ни одно направление не доступно (не должно произойти после валидации)
        if not builder.buttons:
            await message.answer(
                f"❌ Ошибка: Количество тиков {offset_ticks} недопустимо для обоих направлений.\n"
                f"Введите значение от {min_offset} до {max_offset}:"
            )
            return
        
        await message.answer(
            f"✅ Количество тиков: {offset_ticks}\n\n"
            f"📊 Настройки:\n"
            f"• Текущая цена: {current_price:.6f}\n"
            f"• Размер тика: {tick_size}\n\n"
            f"Выберите направление ордера:",
            reply_markup=builder.as_markup()
        )
        await state.set_state(MarketOrderStates.waiting_direction)
    except ValueError:
        data = await state.get_data()
        max_offset_buy = data.get('max_offset_buy', 0)
        max_offset_sell = data.get('max_offset_sell', 0)
        max_offset = max(max_offset_buy, max_offset_sell)
        builder = InlineKeyboardBuilder()
        builder.button(text="❌ Отменить", callback_data="cancel")
        await message.answer(
            f"❌ Неверный формат. Введите целое число от 0 до {max_offset}:",
            reply_markup=builder.as_markup()
        )


@router.callback_query(F.data.startswith("dir_"), MarketOrderStates.waiting_direction)
async def process_direction(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора направления (BUY/SELL)."""
    direction = callback.data.split("_")[1].upper()
    
    data = await state.get_data()
    current_price = data['current_price']
    offset_ticks = data['offset_ticks']
    tick_size = data.get('tick_size', 0.001)
    token_name = data['token_name']
    max_offset_buy = data.get('max_offset_buy', 0)
    max_offset_sell = data.get('max_offset_sell', 0)
    
    # Дополнительная валидация: проверяем, что offset допустим для выбранного направления
    if direction == "BUY" and offset_ticks > max_offset_buy:
        await callback.message.answer(
            f"❌ Ошибка: Количество тиков {offset_ticks} слишком большое для BUY!\n"
            f"Максимум для BUY: {max_offset_buy} тиков"
        )
        await state.clear()
        await callback.answer()
        return
    
    if direction == "SELL" and offset_ticks > max_offset_sell:
        await callback.message.answer(
            f"❌ Ошибка: Количество тиков {offset_ticks} слишком большое для SELL!\n"
            f"Максимум для SELL: {max_offset_sell} тиков"
        )
        await state.clear()
        await callback.answer()
        return
    
    # Рассчитываем целевую цену
    target_price, is_valid = calculate_target_price(current_price, direction, offset_ticks, tick_size)
    
    if not is_valid or target_price <= 0:
        await callback.message.answer(
            f"❌ Ошибка: Рассчитанная цена ({target_price:.6f}) невалидна!\n"
            f"Offset {offset_ticks} тиков слишком большой для текущей цены {current_price:.6f}"
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
    
    # Формируем информацию для подтверждения
    market = data['market']
    market_id = data['market_id']
    amount = data['amount']
    
    info_text = data['yes_info'] if token_name == "YES" else data['no_info']
    spread_text = ""
    if info_text['spread']:
        spread_text = f"\n• Спред: {info_text['spread']:.6f} ({info_text['spread_pct']:.2f}%)\n• Ликвидность: {info_text['total_liquidity']:.2f}"
    
    confirm_text = (
        f"📋 <b>Подтверждение настроек</b>\n\n"
        f"📊 <b>Рынок:</b>\n"
        f"• Market ID: {market_id}\n"
        f"• Название: {market.market_title}\n"
        f"• Токен: {token_name}\n\n"
        f"💰 <b>Ордер:</b>\n"
        f"• Направление: {direction} {token_name}\n"
        f"• Текущая цена: {current_price:.6f}\n"
        f"• Целевая цена: {target_price:.6f}\n"
        f"• Отклонение: {offset_ticks} тиков ({abs(current_price - target_price):.6f})\n"
        f"• Сумма: {amount} USDT{spread_text}\n\n"
        f"⚠️ Ордер будет размещён на {offset_ticks} тиков от текущей цены и НЕ исполнится сразу."
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Разместить ордер", callback_data="confirm_yes")
    builder.button(text="❌ Отменить", callback_data="cancel")
    builder.adjust(2)
    
    await callback.message.edit_text(confirm_text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()
    await state.set_state(MarketOrderStates.waiting_confirm)


@router.callback_query(F.data == "cancel")
async def process_cancel(callback: CallbackQuery, state: FSMContext):
    """
    Универсальный обработчик кнопки 'Отменить' для всех состояний размещения ордера.
    Работает во всех состояниях MarketOrderStates.
    """
    try:
        # Пытаемся отредактировать сообщение (если это inline кнопка)
        await callback.message.edit_text("❌ Размещение ордера отменено")
    except Exception:
        # Если не удалось отредактировать, отправляем новое сообщение
        await callback.message.answer("❌ Размещение ордера отменено")
    
    await state.clear()
    await callback.answer("Отменено")


@router.callback_query(F.data.startswith("confirm_"), MarketOrderStates.waiting_confirm)
async def process_confirm(callback: CallbackQuery, state: FSMContext):
    """Обработка подтверждения размещения ордера."""
    confirm = callback.data.split("_")[1]
    
    if confirm != "yes":
        await callback.message.edit_text("❌ Размещение ордера отменено")
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
    
    await callback.message.edit_text("🔄 Размещение ордера...")
    
    success, order_id = await place_order(client, order_params)
    
    if success:
        await callback.message.edit_text(
            f"✅ <b>Ордер успешно размещён!</b>\n\n"
            f"📋 <b>Итоговая информация:</b>\n"
            f"• Направление: {data['direction']} {data['token_name']}\n"
            f"• Цена: {data['target_price']:.6f}\n"
            f"• Сумма: {data['amount']} USDT\n"
            f"• Offset: {data['offset_ticks']} тиков\n"
            f"• Order ID: {order_id}\n\n"
            f"⚠️ Ордер НЕ исполнится сразу, так как размещён на {data['offset_ticks']} тиков от текущей цены.",
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            f"❌ <b>Не удалось разместить ордер</b>\n\n"
            f"Проверьте баланс и параметры ордера.",
            parse_mode="HTML"
        )
    
    await state.clear()
    await callback.answer()


# ============================================================================
# Главная функция
# ============================================================================

async def main():
    """Главная функция запуска бота."""
    # Настраиваем прокси для всех API запросов (если указан в настройках)
    setup_proxy()
    
    # Инициализируем базу данных
    init_database()
    
    # Регистрируем роутер
    dp.include_router(router)
    
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
