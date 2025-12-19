"""
Скрипт для автоматической перестановки лимитных ордеров (market making) на Opinion.trade.

Алгоритм работы:
1. Запрашивает ссылку на рынок
2. Выдаёт информацию о спреде и ликвидности
3. Запрашивает сумму для фарминга
4. Проверяет достаточность средств (если недостаточно - переспрашивает)
5. Запрашивает сторону (YES/NO)
6. Указывает текущую цену
7. Спрашивает на сколько тиков стоять и как часто переставлять лимитку
8. Закидывает инфу о настройках и рынках, спрашивает подтверждение размещения
9. Размещает лимитку, которая не исполнится сразу

Цель: поставить лимитку, которая не исполнится сразу, и автоматически переставлять её.

Документация: https://docs.opinion.trade/developer-guide/opinion-clob-sdk/api-references/methods
"""

import os
import re
import sys
from urllib.parse import urlparse, parse_qs
from typing import Optional

from dotenv import load_dotenv
from opinion_clob_sdk import Client
from opinion_clob_sdk.sdk import InvalidParamError, OpenApiError
from opinion_clob_sdk.chain.py_order_utils.model.order import PlaceOrderDataInput
from opinion_clob_sdk.chain.py_order_utils.model.sides import OrderSide
from opinion_clob_sdk.chain.py_order_utils.model.order_type import LIMIT_ORDER
from opinion_clob_sdk.chain.exception import (
    BalanceNotEnough,
    NoPositionsToRedeem,
    InsufficientGasBalance
)

# Загружаем переменные окружения из .env файла
load_dotenv()


def parse_market_url(url: str) -> Optional[int]:
    """
    Парсит URL Opinion.trade и извлекает marketId (topicId).
    
    Args:
        url: URL страницы рынка на Opinion.trade
        
    Returns:
        int: marketId (topicId) или None, если не удалось извлечь
    """
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        
        if "topicId" in params:
            return int(params["topicId"][0])
        
        # Альтернативные способы извлечения
        match = re.search(r"topicId[=:](\d+)", url)
        if match:
            return int(match.group(1))
        
        return None
    except (ValueError, AttributeError):
        return None


def initialize_client() -> Client:
    """
    Инициализирует клиент Opinion CLOB SDK.
    
    Использует параметры из .env файла:
    - API_KEY: API ключ от Opinion Labs
    - RPC_URL: URL RPC ноды BNB Chain
    - PRIVATE_KEY: Приватный ключ из MetaMask
    - MULTI_SIG_ADDRESS: Адрес мультисиг кошелька
    - CONDITIONAL_TOKEN_ADDR: Адрес контракта условных токенов (опционально)
    - MULTISEND_ADDR: Адрес контракта мультисенда (опционально)
    
    Returns:
        Client: Инициализированный клиент
        
    Raises:
        ValueError: Если не хватает обязательных параметров
    """
    api_key = os.getenv('API_KEY')
    rpc_url = os.getenv('RPC_URL')
    private_key = os.getenv('PRIVATE_KEY')
    multi_sig_addr = os.getenv('MULTI_SIG_ADDRESS')
    conditional_tokens_addr = os.getenv('CONDITIONAL_TOKEN_ADDR', '0xAD1a38cEc043e70E83a3eC30443dB285ED10D774')
    multisend_addr = os.getenv('MULTISEND_ADDR', '0x998739BFdAAdde7C933B942a68053933098f9EDa')
    
    if not all([api_key, rpc_url, private_key, multi_sig_addr]):
        raise ValueError("Не все обязательные параметры найдены в .env файле")
    
    # Инициализируем клиент с кешированием для оптимизации
    client = Client(
        host='https://proxy.opinion.trade:8443',
        apikey=api_key,
        chain_id=56,  # BNB Chain mainnet
        rpc_url=rpc_url,
        private_key=private_key,
        multi_sig_addr=multi_sig_addr,
        conditional_tokens_addr=conditional_tokens_addr,
        multisend_addr=multisend_addr,
        market_cache_ttl=300,        # Кеш рынков на 5 минут
        quote_tokens_cache_ttl=3600, # Кеш USDT токенов на 1 час
        enable_trading_check_interval=3600  # Проверка статуса торговли каждый час
    )
    
    print("✅ Клиент успешно инициализирован!\n")
    return client


def get_market_info(client: Client, market_id: int):
    """
    Получает информацию о рынке.
    
    Шаг 3 алгоритма: Получаем информацию о рынке через SDK.
    
    Args:
        client: Инициализированный клиент Opinion SDK
        market_id: ID рынка
        
    Returns:
        dict: Данные о рынке или None в случае ошибки
    """
    print(f"📊 Получение информации о рынке #{market_id}...")
    
    try:
        # Получаем информацию о рынке с использованием кеша
        response = client.get_market(market_id=market_id, use_cache=True)

        if response.errno == 0:
            market = response.result.data
            print(f"✅ Рынок найден: {market.market_title}\n")
            return market
        else:
            print(f"❌ Ошибка получения рынка: {response.errmsg} (код: {response.errno})\n")
            return None
            
    except InvalidParamError as e:
        print(f"❌ Неверный параметр: {e}\n")
        return None
    except OpenApiError as e:
        print(f"❌ Ошибка API: {e}\n")
        return None
    except Exception as e:
        print(f"❌ Неожиданная ошибка: {e}\n")
        return None


def get_orderbooks(client: Client, yes_token_id: str, no_token_id: str):
    """
    Получает стаканы ордеров для YES и NO токенов.
    
    Шаг 4 алгоритма: Получаем стаканы ордеров для обоих токенов.
    
    Args:
        client: Инициализированный клиент Opinion SDK
        yes_token_id: ID токена YES
        no_token_id: ID токена NO
        
    Returns:
        tuple: (yes_orderbook, no_orderbook) или (None, None) в случае ошибки
    """
    print("📖 Получение стаканов ордеров...")
    
    yes_orderbook = None
    no_orderbook = None
    
    # Получаем стакан для YES токена
    try:
        response = client.get_orderbook(token_id=yes_token_id)
        if response.errno == 0:
            # Структура ответа: response.result может быть напрямую объектом стакана
            yes_orderbook = response.result if hasattr(response.result, 'bids') else getattr(response.result, 'data', response.result)
            print(f"✅ Стакан для YES токена получен")
        else:
            print(f"⚠️ Не удалось получить стакан для YES: {response.errmsg}")
    except Exception as e:
        print(f"⚠️ Ошибка получения стакана для YES: {e}")
        import traceback
        traceback.print_exc()
    
    # Получаем стакан для NO токена
    try:
        response = client.get_orderbook(token_id=no_token_id)
        if response.errno == 0:
            # Структура ответа: response.result может быть напрямую объектом стакана
            no_orderbook = response.result if hasattr(response.result, 'bids') else getattr(response.result, 'data', response.result)
            print(f"✅ Стакан для NO токена получен")
        else:
            print(f"⚠️ Не удалось получить стакан для NO: {response.errmsg}")
    except Exception as e:
        print(f"⚠️ Ошибка получения стакана для NO: {e}")
        import traceback
        traceback.print_exc()
    
    print()
    return yes_orderbook, no_orderbook


def calculate_spread_and_liquidity(orderbook, token_name: str) -> dict:
    """
    Рассчитывает спред и ликвидность для токена.
    
    Args:
        orderbook: Стакан ордеров
        token_name: Название токена (YES/NO)
        
    Returns:
        dict: Информация о спреде и ликвидности
    """
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
    
    best_bid = float(bids[0].price) if bids else None
    best_ask = float(asks[0].price) if asks else None
    
    # Рассчитываем спред
    spread = None
    spread_pct = None
    mid_price = None
    
    if best_bid and best_ask:
        spread = best_ask - best_bid
        mid_price = (best_bid + best_ask) / 2
        spread_pct = (spread / mid_price * 100) if mid_price > 0 else 0
    
    # Рассчитываем ликвидность (сумма размеров в первых 5 уровнях)
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


def display_spread_and_liquidity(market, yes_orderbook, no_orderbook):
    """
    Выводит информацию о спреде и ликвидности для YES и NO токенов.
    
    Args:
        market: Данные о рынке
        yes_orderbook: Стакан для YES токена
        no_orderbook: Стакан для NO токена
    """
    print("=" * 80)
    print("📊 СПРЕД И ЛИКВИДНОСТЬ")
    print("=" * 80)
    
    # YES токен
    yes_info = calculate_spread_and_liquidity(yes_orderbook, "YES")
    print(f"\n✅ YES Token:")
    if yes_info['best_bid'] and yes_info['best_ask']:
        print(f"   Лучший Bid: {yes_info['best_bid']:.6f}")
        print(f"   Лучший Ask: {yes_info['best_ask']:.6f}")
        print(f"   Спред: {yes_info['spread']:.6f} ({yes_info['spread_pct']:.2f}%)")
        print(f"   Mid Price: {yes_info['mid_price']:.6f}")
        print(f"   Ликвидность Bid: {yes_info['bid_liquidity']:.2f}")
        print(f"   Ликвидность Ask: {yes_info['ask_liquidity']:.2f}")
        print(f"   Общая ликвидность: {yes_info['total_liquidity']:.2f}")
    else:
        print("   ⚠️ Недостаточно данных для расчёта спреда")
    
    # NO токен
    no_info = calculate_spread_and_liquidity(no_orderbook, "NO")
    print(f"\n❌ NO Token:")
    if no_info['best_bid'] and no_info['best_ask']:
        print(f"   Лучший Bid: {no_info['best_bid']:.6f}")
        print(f"   Лучший Ask: {no_info['best_ask']:.6f}")
        print(f"   Спред: {no_info['spread']:.6f} ({no_info['spread_pct']:.2f}%)")
        print(f"   Mid Price: {no_info['mid_price']:.6f}")
        print(f"   Ликвидность Bid: {no_info['bid_liquidity']:.2f}")
        print(f"   Ликвидность Ask: {no_info['ask_liquidity']:.2f}")
        print(f"   Общая ликвидность: {no_info['total_liquidity']:.2f}")
    else:
        print("   ⚠️ Недостаточно данных для расчёта спреда")
    
    print("\n" + "=" * 80 + "\n")
    
    return yes_info, no_info


def display_market_info(market, yes_orderbook, no_orderbook):
    """
    Выводит информацию о рынке и стаканах ордеров.
    
    Шаг 5 алгоритма: Выводим всю собранную информацию.
    
    Args:
        market: Данные о рынке
        yes_orderbook: Стакан для YES токена
        no_orderbook: Стакан для NO токена
    """
    print("=" * 80)
    print("📊 ИНФОРМАЦИЯ О РЫНКЕ")
    print("=" * 80)
    
    if market:
        print(f"\n🆔 Market ID: {market.market_id if hasattr(market, 'market_id') else 'N/A'}")
        print(f"📝 Название: {market.market_title}")
        print(f"📈 Статус: {market.status if hasattr(market, 'status') else 'N/A'}")
        print(f"💵 Quote Token: {market.quote_token if hasattr(market, 'quote_token') else 'N/A'}")
        
        # Выводим YES токен и его стакан
        if hasattr(market, 'yes_token_id') and market.yes_token_id:
            print(f"\n✅ YES Token ID: {market.yes_token_id}")
            if yes_orderbook:
                print(f"   Стакан:")
                if yes_orderbook.bids:
                    print(f"     🟢 Лучший Bid: {yes_orderbook.bids[0].price} | Размер: {yes_orderbook.bids[0].size}")
                if yes_orderbook.asks:
                    print(f"     🔴 Лучший Ask: {yes_orderbook.asks[0].price} | Размер: {yes_orderbook.asks[0].size}")
        
        # Выводим NO токен и его стакан
        if hasattr(market, 'no_token_id') and market.no_token_id:
            print(f"\n❌ NO Token ID: {market.no_token_id}")
            if no_orderbook:
                print(f"   Стакан:")
                if no_orderbook.bids:
                    print(f"     🟢 Лучший Bid: {no_orderbook.bids[0].price} | Размер: {no_orderbook.bids[0].size}")
                if no_orderbook.asks:
                    print(f"     🔴 Лучший Ask: {no_orderbook.asks[0].price} | Размер: {no_orderbook.asks[0].size}")
    
    print("\n" + "=" * 80 + "\n")


def get_order_input(market) -> Optional[dict]:
    """
    Запрашивает у пользователя параметры ордера.
    
    Шаги 6-8 алгоритма: Спрашиваем тип, цену и размер ордера.
    
    Args:
        market: Данные о рынке (для получения token_id)
        
    Returns:
        dict: Параметры ордера или None, если пользователь отменил
    """
    print("📝 Ввод параметров ордера")
    print("-" * 80)
    
    # Определяем доступные токены
    yes_token_id = getattr(market, 'yes_token_id', None)
    no_token_id = getattr(market, 'no_token_id', None)
    
    if not yes_token_id or not no_token_id:
        print("❌ Не удалось определить токены рынка")
        return None
    
    # Шаг 6: Спрашиваем направление (BUY/SELL) и токен (YES/NO)
    print("\n1. Выберите направление и токен:")
    print("   1) BUY YES")
    print("   2) SELL YES")
    print("   3) BUY NO")
    print("   4) SELL NO")
    
    choice = input("\nВаш выбор (1-4): ").strip()
    
    if choice == "1":
        side = OrderSide.BUY
        token_id = yes_token_id
        token_name = "YES"
    elif choice == "2":
        side = OrderSide.SELL
        token_id = yes_token_id
        token_name = "YES"
    elif choice == "3":
        side = OrderSide.BUY
        token_id = no_token_id
        token_name = "NO"
    elif choice == "4":
        side = OrderSide.SELL
        token_id = no_token_id
        token_name = "NO"
    else:
        print("❌ Неверный выбор")
        return None
    
    # Правильно выводим направление (BUY/SELL вместо 0/1)
    side_str = "BUY" if side == OrderSide.BUY else "SELL"
    print(f"\n✅ Выбрано: {side_str} {token_name}")
    
    # Шаг 7: Спрашиваем цену ордера
    # Цена токена на prediction markets - это вероятность исхода события
    # Диапазон: от 0.0 (0% вероятность) до 1.0 (100% вероятность)
    # Например: 0.55 = 55% вероятность, 0.92 = 92% вероятность
    try:
        price = input("\n2. Введите цену ордера (0.0 - 1.0, например, 0.55): ").strip()
        price = float(price)
        
        # Строгая проверка: цена должна быть в диапазоне от 0 до 1
        if price < 0 or price > 1:
            print("❌ Цена должна быть в диапазоне от 0.0 до 1.0 (0% - 100% вероятность)")
            print(f"   Вы ввели: {price}")
            return None
        
        if price == 0:
            print("⚠️  Внимание: цена 0.0 означает 0% вероятность. Ордер может не исполниться.")
        elif price == 1:
            print("⚠️  Внимание: цена 1.0 означает 100% вероятность. Ордер может не исполниться.")
    except ValueError:
        print("❌ Неверный формат цены. Введите число от 0.0 до 1.0")
        return None
    
    # Шаг 8: Спрашиваем размер ордера
    try:
        amount = input("\n3. Введите размер ордера в USDT (например, 10): ").strip()
        amount = float(amount)
        if amount <= 0:
            print("❌ Размер должен быть положительным числом")
            return None
    except ValueError:
        print("❌ Неверный формат размера")
        return None
    
    # Определяем marketId
    market_id = getattr(market, 'market_id', None) or getattr(market, 'topic_id', None)
    if not market_id:
        print("❌ Не удалось определить marketId")
        return None
    
    return {
        'market_id': market_id,
        'token_id': token_id,
        'side': side,
        'price': str(price),
        'amount': amount,
        'token_name': token_name
    }


def check_usdt_balance(client: Client, required_amount: float) -> tuple[bool, dict]:
    """
    Проверяет достаточность USDT баланса перед размещением BUY ордера.
    
    Args:
        client: Инициализированный клиент Opinion SDK
        required_amount: Требуемая сумма в USDT
        
    Returns:
        tuple: (достаточно_ли, данные_баланса)
    """
    try:
        response = client.get_my_balances()
        
        if response.errno != 0:
            print(f"⚠️ Не удалось проверить баланс: {response.errmsg}")
            return False, {}
        
        # Получаем данные баланса (структура может отличаться)
        balance_data = response.result if not hasattr(response.result, 'data') else response.result.data
        
        # Ищем доступный USDT баланс
        available = 0.0
        if hasattr(balance_data, 'balances') and balance_data.balances:
            for balance in balance_data.balances:
                available += float(getattr(balance, 'available_balance', 0))
        elif hasattr(balance_data, 'available_balance'):
            available = float(balance_data.available_balance)
        elif hasattr(balance_data, 'available'):
            available = float(balance_data.available)
        
        print(f"💰 Доступный USDT баланс: {available} | Требуется: {required_amount}")
        
        if available < required_amount:
            print(f"❌ Недостаточно USDT! Доступно: {available}, требуется: {required_amount}")
            return False, balance_data
        
        return True, balance_data
        
    except Exception as e:
        print(f"⚠️ Ошибка проверки баланса: {e}")
        return False, {}


def check_token_balance(client: Client, token_id: str, required_amount_usdt: float, price: float) -> tuple[bool, float]:
    """
    Проверяет достаточность баланса токенов перед размещением SELL ордера.
    
    Для SELL ордера нужно иметь токены, а не USDT.
    Рассчитываем, сколько токенов нужно для продажи на указанную сумму USDT.
    
    Args:
        client: Инициализированный клиент Opinion SDK
        token_id: ID токена, который нужно продать
        required_amount_usdt: Требуемая сумма в USDT (сколько хотите получить)
        price: Цена продажи токена
        
    Returns:
        tuple: (достаточно_ли, доступное_количество_токенов)
    """
    try:
        # Получаем позиции (токены, которые у нас есть)
        positions = client.get_my_positions(limit=100)
        
        # Обрабатываем разные форматы ответа
        if hasattr(positions, 'errno') and positions.errno != 0:
            print(f"⚠️ Не удалось проверить позиции: {positions.errmsg}")
            return False, 0.0
        
        pos_list = []
        if hasattr(positions, 'result'):
            pos_list = positions.result.list if hasattr(positions.result, 'list') else []
        elif isinstance(positions, list):
            pos_list = positions
        
        # Ищем позицию по нужному токену
        available_tokens = 0.0
        for pos in pos_list:
            pos_token_id = getattr(pos, 'token_id', None) or str(getattr(pos, 'tokenId', ''))
            if str(pos_token_id) == str(token_id):
                # Нашли нужный токен, получаем количество
                available_tokens = float(getattr(pos, 'size', getattr(pos, 'amount', 0)))
                break
        
        # Рассчитываем, сколько токенов нужно для продажи на required_amount_usdt
        # required_tokens = required_amount_usdt / price
        required_tokens = required_amount_usdt / price if price > 0 else 0
        
        print(f"💰 Доступно токенов: {available_tokens:.6f}")
        print(f"💰 Требуется токенов для продажи на {required_amount_usdt} USDT: {required_tokens:.6f} (по цене {price})")
        
        if available_tokens < required_tokens:
            print(f"❌ Недостаточно токенов! Доступно: {available_tokens:.6f}, требуется: {required_tokens:.6f}")
            print(f"   💡 Для SELL ордера нужно сначала купить токены через BUY ордер")
            return False, available_tokens
        
        return True, available_tokens
        
    except Exception as e:
        print(f"⚠️ Ошибка проверки баланса токенов: {e}")
        import traceback
        traceback.print_exc()
        return False, 0.0


def place_order(client: Client, order_params: dict) -> bool:
    """
    Размещает ордер на рынке.
    
    Шаг 9 алгоритма: Делаем ордер через SDK.
    
    Args:
        client: Инициализированный клиент Opinion SDK
        order_params: Параметры ордера
        
    Returns:
        bool: True если ордер успешно размещён, False в противном случае
    """
    side_str = "BUY" if order_params['side'] == OrderSide.BUY else "SELL"
    print(f"\n🔄 Размещение ордера: {side_str} {order_params['token_name']} @ {order_params['price']} (размер: {order_params['amount']})...")
    
    # Проверяем баланс перед размещением
    # Для BUY нужен USDT баланс, для SELL нужны токены
    print("\n🔍 Проверка баланса...")
    
    if order_params['side'] == OrderSide.BUY:
        # Для BUY проверяем USDT баланс
        has_balance, balance_data = check_usdt_balance(client, order_params['amount'])
        if not has_balance:
            print("❌ Размещение ордера отменено из-за недостаточного USDT баланса\n")
            return False
    else:
        # Для SELL проверяем баланс токенов
        price = float(order_params['price'])
        has_tokens, available_tokens = check_token_balance(
            client, 
            order_params['token_id'], 
            order_params['amount'], 
            price
        )
        if not has_tokens:
            print("❌ Размещение ордера отменено из-за недостаточного баланса токенов\n")
            print("💡 Для размещения SELL ордера нужно сначала купить токены через BUY ордер")
            return False
    
    try:
        # Включаем торговлю (требуется один раз перед размещением ордеров)
        print("🔓 Включение торговли...")
        client.enable_trading()

        # Формируем данные ордера
        print("📝 Формирование данных ордера...")
        
        # Округляем цену до 6 знаков после запятой (требование API)
        price = float(order_params['price'])
        price_rounded = round(price, 6)
        
        if price != price_rounded:
            print(f"   ⚠️  Цена округлена с {price} до {price_rounded} (максимум 6 знаков после запятой)")
        
        order_data = PlaceOrderDataInput(
            marketId=order_params['market_id'],
            tokenId=order_params['token_id'],
            side=order_params['side'],
            orderType=LIMIT_ORDER,
            price=str(price_rounded),  # Используем округлённую цену
            makerAmountInQuoteToken=order_params['amount']
        )
        
        print(f"📤 Отправка ордера на сервер...")
        print(f"   Market ID: {order_params['market_id']}")
        print(f"   Token ID: {order_params['token_id']}")
        print(f"   Side: {side_str}")
        print(f"   Price: {order_params['price']}")
        print(f"   Amount: {order_params['amount']}")
        
        # Размещаем ордер с проверкой одобрения
        result = client.place_order(order_data, check_approval=True)
        
        # Отладочный вывод полного ответа
        print(f"\n📋 Полный ответ API:")
        print(f"   errno: {result.errno}")
        print(f"   errmsg: {result.errmsg if hasattr(result, 'errmsg') else 'N/A'}")
        if hasattr(result, 'result'):
            print(f"   result type: {type(result.result)}")
            if hasattr(result.result, '__dict__'):
                print(f"   result attributes: {list(result.result.__dict__.keys())}")
        
        if result.errno == 0:
            # Пытаемся извлечь order_id из разных возможных структур
            order_id = 'N/A'
            if hasattr(result, 'result'):
                # Проверяем order_data (видно из отладки, что есть атрибут 'order_data')
                if hasattr(result.result, 'order_data'):
                    order_data = result.result.order_data
                    # order_data может быть объектом с order_id
                    if hasattr(order_data, 'order_id'):
                        order_id = order_data.order_id
                    elif hasattr(order_data, 'id'):
                        order_id = order_data.id
                    elif isinstance(order_data, dict):
                        order_id = order_data.get('order_id') or order_data.get('id', 'N/A')
                    else:
                        # Отладочный вывод структуры order_data
                        print(f"   🔍 order_data type: {type(order_data)}")
                        if hasattr(order_data, '__dict__'):
                            print(f"   🔍 order_data attributes: {list(order_data.__dict__.keys())}")
                            # Пробуем найти order_id в атрибутах
                            for attr in dir(order_data):
                                if 'order' in attr.lower() and 'id' in attr.lower():
                                    try:
                                        order_id = getattr(order_data, attr)
                                        break
                                    except:
                                        pass
                elif hasattr(result.result, 'data'):
                    if hasattr(result.result.data, 'order_id'):
                        order_id = result.result.data.order_id
                elif hasattr(result.result, 'order_id'):
                    order_id = result.result.order_id
                elif isinstance(result.result, dict):
                    order_id = result.result.get('order_id', 'N/A')
            
            print(f"\n✅ Ордер успешно размещён! Order ID: {order_id}\n")
            return True
        else:
            print(f"\n❌ Ошибка размещения ордера:")
            print(f"   Код ошибки: {result.errno}")
            print(f"   Сообщение: {result.errmsg if hasattr(result, 'errmsg') else 'N/A'}")
            print(f"   Полный ответ: {result}\n")
            return False
            
    except BalanceNotEnough as e:
        print(f"\n❌ Недостаточно баланса: {e}\n")
        import traceback
        traceback.print_exc()
        return False
    except InvalidParamError as e:
        print(f"\n❌ Неверный параметр: {e}\n")
        import traceback
        traceback.print_exc()
        return False
    except OpenApiError as e:
        print(f"\n❌ Ошибка API: {e}\n")
        import traceback
        traceback.print_exc()
        return False
    except Exception as e:
        print(f"\n❌ Неожиданная ошибка: {e}\n")
        import traceback
        traceback.print_exc()
        return False


def display_order_info(client: Client):
    """
    Выводит информацию о всех открытых ордерах.
    
    Args:
        client: Инициализированный клиент Opinion SDK
    """
    print("=" * 80)
    print("📋 ОТКРЫТЫЕ ОРДЕРА")
    print("=" * 80)
    
    try:
        # Пробуем разные варианты параметра status
        # Согласно ошибке, API ожидает числовой статус, а не строку "open"
        try:
            response = client.get_my_orders(status=1, limit=50)  # 1 = open
        except:
            # Если не работает с числом, пробуем без параметра status
            try:
                response = client.get_my_orders(limit=50)
            except Exception as e:
                print(f"❌ Ошибка получения ордеров: {e}\n")
                return

        # Отладочный вывод структуры ответа
        print(f"\n🔍 Отладка структуры ордеров:")
        print(f"   response type: {type(response)}")
        if hasattr(response, '__dict__'):
            print(f"   response attributes: {list(response.__dict__.keys())}")
        if hasattr(response, 'errno'):
            print(f"   errno: {response.errno}")
            if response.errno != 0:
                print(f"   errmsg: {getattr(response, 'errmsg', 'N/A')}")

        if hasattr(response, 'errno') and response.errno == 0:
            # Обрабатываем разные форматы ответа
            orders = []
            if hasattr(response, 'result'):
                if hasattr(response.result, 'list'):
                    orders = response.result.list
                elif hasattr(response.result, 'data'):
                    if hasattr(response.result.data, 'list'):
                        orders = response.result.data.list
                    elif isinstance(response.result.data, list):
                        orders = response.result.data
                elif isinstance(response.result, list):
                    orders = response.result
            elif isinstance(response, list):
                orders = response
            
            if not orders:
                print("\n📭 Нет открытых ордеров\n")
            else:
                print(f"\n✅ Найдено открытых ордеров: {len(orders)}\n")
                for i, order in enumerate(orders, 1):
                    print(f"  Ордер #{i}:")
                    # Пробуем разные варианты атрибутов
                    order_id = getattr(order, 'order_id', getattr(order, 'id', getattr(order, 'orderId', 'N/A')))
                    side = getattr(order, 'side', getattr(order, 'order_side', 'N/A'))
                    price = getattr(order, 'price', getattr(order, 'order_price', 'N/A'))
                    size = getattr(order, 'size', getattr(order, 'amount', getattr(order, 'quantity', 'N/A')))
                    status = getattr(order, 'status', getattr(order, 'order_status', 'N/A'))
                    
                    print(f"    Order ID: {order_id}")
                    print(f"    Направление: {side} | Цена: {price} | Размер: {size}")
                    if status != 'N/A':
                        print(f"    Статус: {status}")
                    print()
        else:
            errmsg = getattr(response, 'errmsg', 'N/A') if hasattr(response, 'errmsg') else 'Unknown error'
            print(f"\n❌ Ошибка получения ордеров: {errmsg}\n")
            
    except Exception as e:
        print(f"\n❌ Ошибка получения ордеров: {e}\n")
        import traceback
        traceback.print_exc()


def display_balance(client: Client):
    """
    Выводит информацию о балансе.
    
    Шаг 14 алгоритма: Выводим информацию о балансе.
    
    Args:
        client: Инициализированный клиент Opinion SDK
    """
    print("=" * 80)
    print("💰 БАЛАНС")
    print("=" * 80)
    
    try:
        response = client.get_my_balances()

        if response.errno == 0:
            # Структура ответа может быть разной - пробуем разные варианты
            balance_data = response.result if not hasattr(response.result, 'data') else response.result.data
            
            # Отладочный вывод структуры
            print(f"\n🔍 Отладка структуры баланса:")
            print(f"   response.result type: {type(response.result)}")
            if hasattr(response.result, '__dict__'):
                print(f"   response.result attributes: {list(response.result.__dict__.keys())}")
            
            # Обрабатываем разные форматы ответа
            if hasattr(balance_data, 'balances'):
                balances = balance_data.balances
                for balance in balances:
                    quote_token = getattr(balance, 'quote_token', 'N/A')
                    available = getattr(balance, 'available_balance', 0)
                    frozen = getattr(balance, 'frozen_balance', 0)
                    total = getattr(balance, 'total_balance', 0)
                    
                    print(f"\n💵 Token: {quote_token}")
                    print(f"   Доступно: {available}")
                    print(f"   Заморожено: {frozen}")
                    print(f"   Всего: {total}")
            elif hasattr(balance_data, 'available_balance') or hasattr(balance_data, 'available'):
                # Прямой объект баланса
                quote_token = getattr(balance_data, 'quote_token', 'N/A')
                available = getattr(balance_data, 'available_balance', getattr(balance_data, 'available', 0))
                frozen = getattr(balance_data, 'frozen_balance', getattr(balance_data, 'frozen', 0))
                total = getattr(balance_data, 'total_balance', getattr(balance_data, 'total', 0))
                
                print(f"\n💵 Token: {quote_token}")
                print(f"   Доступно: {available}")
                print(f"   Заморожено: {frozen}")
                print(f"   Всего: {total}")
            else:
                print(f"\n📊 Данные баланса (сырой формат): {balance_data}")
                print(f"   Тип: {type(balance_data)}")
                if hasattr(balance_data, '__dict__'):
                    print(f"   Атрибуты: {list(balance_data.__dict__.keys())}")
        else:
            print(f"❌ Ошибка получения баланса: {response.errmsg}\n")
            
    except Exception as e:
        print(f"❌ Ошибка: {e}\n")
        import traceback
        traceback.print_exc()


def display_positions(client: Client):
    """
    Выводит информацию о позициях.
    
    Args:
        client: Инициализированный клиент Opinion SDK
    """
    try:
        positions = client.get_my_positions(limit=20)

        # Отладочный вывод структуры ответа
        print(f"\n🔍 Отладка структуры позиций:")
        print(f"   positions type: {type(positions)}")
        if hasattr(positions, '__dict__'):
            print(f"   positions attributes: {list(positions.__dict__.keys())}")
        if hasattr(positions, 'errno'):
            print(f"   errno: {positions.errno}")
            if positions.errno != 0:
                print(f"   errmsg: {getattr(positions, 'errmsg', 'N/A')}")
        
        if hasattr(positions, 'errno') and positions.errno != 0:
            print(f"\n❌ Ошибка получения позиций: {getattr(positions, 'errmsg', 'N/A')}\n")
            return
        
        # Обрабатываем разные форматы ответа
        pos_list = []
        if hasattr(positions, 'result'):
            if hasattr(positions.result, 'list'):
                pos_list = positions.result.list
            elif hasattr(positions.result, 'data'):
                if hasattr(positions.result.data, 'list'):
                    pos_list = positions.result.data.list
                elif isinstance(positions.result.data, list):
                    pos_list = positions.result.data
            elif isinstance(positions.result, list):
                pos_list = positions.result
        elif isinstance(positions, list):
            pos_list = positions
        
        if not pos_list:
            print("\n📭 Нет открытых позиций\n")
        else:
            print(f"\n✅ Найдено позиций: {len(pos_list)}\n")
            for i, pos in enumerate(pos_list, 1):
                print(f"  Позиция #{i}:")
                # Пробуем разные варианты атрибутов
                market_id = getattr(pos, 'market_id', getattr(pos, 'marketId', 'N/A'))
                token_id = getattr(pos, 'token_id', getattr(pos, 'tokenId', 'N/A'))
                size = getattr(pos, 'size', getattr(pos, 'amount', getattr(pos, 'quantity', 'N/A')))
                price = getattr(pos, 'price', getattr(pos, 'avg_price', 'N/A'))
                
                print(f"    Market ID: {market_id}")
                if token_id != 'N/A':
                    print(f"    Token ID: {str(token_id)[:30]}...")
                print(f"    Размер: {size}")
                if price != 'N/A':
                    print(f"    Средняя цена: {price}")
                print()
                
    except Exception as e:
        print(f"\n❌ Ошибка получения позиций: {e}\n")
        import traceback
        traceback.print_exc()


def display_trade_history(client: Client, market_id: int):
    """
    Выводит историю сделок.
    
    Шаг 16 алгоритма: Выводим информацию о истории ордеров.
    
    Args:
        client: Инициализированный клиент Opinion SDK
        market_id: ID рынка для фильтрации истории
    """
    print("=" * 80)
    print("📜 ИСТОРИЯ СДЕЛОК")
    print("=" * 80)
    
    try:
        trades = client.get_my_trades(market_id=market_id)
        
        if hasattr(trades, 'errno') and trades.errno != 0:
            print(f"❌ Ошибка получения истории: {trades.errmsg}\n")
            return
        
        # Обрабатываем разные форматы ответа
        if hasattr(trades, 'result'):
            trade_list = trades.result.list if hasattr(trades.result, 'list') else []
        elif isinstance(trades, list):
            trade_list = trades
        else:
            trade_list = []
        
        if not trade_list:
            print(f"\n📭 Нет сделок по рынку #{market_id}\n")
        else:
            print(f"\nНайдено сделок: {len(trade_list)}\n")
            for trade in trade_list:
                trade_id = getattr(trade, 'trade_id', 'N/A')
                side = getattr(trade, 'side', 'N/A')
                price = getattr(trade, 'price', 'N/A')
                size = getattr(trade, 'size', getattr(trade, 'amount', 'N/A'))
                print(f"  Trade ID: {trade_id} | {side} @ {price} | Размер: {size}")
                print()
                
    except Exception as e:
        print(f"❌ Ошибка: {e}\n")


def get_farming_amount(client: Client) -> float:
    """
    Запрашивает сумму для фарминга с проверкой баланса.
    
    Если баланса недостаточно, переспрашивает до тех пор, пока не будет достаточно средств.
    
    Args:
        client: Инициализированный клиент Opinion SDK
        
    Returns:
        float: Сумма для фарминга
    """
    while True:
        try:
            amount_str = input("\n💰 Введите сумму для фарминга (в USDT, например, 10): ").strip()
            amount = float(amount_str)
            
            if amount <= 0:
                print("❌ Сумма должна быть положительным числом")
                continue
            
            # Проверяем USDT баланс (для BUY ордеров)
            # Для SELL ордеров баланс токенов будет проверяться позже
            print(f"\n🔍 Проверка USDT баланса для суммы {amount}...")
            has_balance, balance_data = check_usdt_balance(client, amount)
            
            if has_balance:
                print(f"✅ USDT баланс достаточен для размещения BUY ордера на {amount}")
                print(f"   ⚠️  Примечание: Для SELL ордеров нужны токены, а не USDT")
                return amount
            else:
                print(f"❌ Недостаточно USDT. Попробуйте ввести меньшую сумму.")
                retry = input("   Ввести другую сумму? (y/n): ").strip().lower()
                if retry != 'y':
                    return None
                    
        except ValueError:
            print("❌ Неверный формат суммы. Введите число.")
        except KeyboardInterrupt:
            return None


def get_side_choice() -> tuple[Optional[str], Optional[str]]:
    """
    Запрашивает выбор стороны (YES/NO).
    
    Returns:
        tuple: (side_str, token_name) или (None, None) если отменено
    """
    print("\n📈 Выберите сторону:")
    print("   1) YES")
    print("   2) NO")
    
    choice = input("\nВаш выбор (1-2): ").strip()
    
    if choice == "1":
        return "YES", "YES"
    elif choice == "2":
        return "NO", "NO"
    else:
        print("❌ Неверный выбор")
        return None, None


def calculate_target_price(current_price: float, side: str, offset_ticks: int, tick_size: float = 0.001) -> tuple[float, bool]:
    """
    Рассчитывает целевую цену для лимитного ордера.
    
    Для BUY: target = current_price - offset_ticks * tick_size (ниже текущей цены)
    Для SELL: target = current_price + offset_ticks * tick_size (выше текущей цены)
    
    Args:
        current_price: Текущая цена (mid price)
        side: Направление ("BUY" или "SELL")
        offset_ticks: Количество тиков от текущей цены
        tick_size: Размер тика (по умолчанию 0.001)
        
    Returns:
        tuple: (целевая_цена, валидна_ли_цена)
        - целевая_цена: Целевая цена для ордера (округлённая до 3 знаков после запятой, требование API)
        - валидна_ли_цена: True если цена > 0, False если цена стала 0 или отрицательной
    """
    MIN_PRICE = 0.000001  # Минимальная допустимая цена (больше 0)
    
    if side == "BUY":
        target = current_price - offset_ticks * tick_size
    else:  # SELL
        target = current_price + offset_ticks * tick_size
    
    # Ограничиваем диапазоном MIN_PRICE-1
    target = max(MIN_PRICE, min(1.0, target))
    
    # Проверяем, что цена валидна (больше 0)
    is_valid = target > 0
    
    # Округляем до 3 знаков после запятой (требование API: максимум 3 знака)
    target = round(target, 3)
    
    # Если после округления цена стала 0, устанавливаем минимальную
    if target == 0.0:
        target = round(MIN_PRICE, 3)  # Минимум тоже округляем до 3 знаков
        is_valid = True
    
    return target, is_valid


def main():
    """
    Главная функция скрипта - реализует алгоритм автоматической перестановки лимитных ордеров.
    """
    print("\n" + "=" * 80)
    print("🚀 OPINION.TRADE - АВТОМАТИЧЕСКАЯ ПЕРЕСТАНОВКА ЛИМИТНЫХ ОРДЕРОВ")
    print("=" * 80 + "\n")
    
    try:
        # Инициализируем клиент
        client = initialize_client()
        
        # Шаг 1: Запрашиваем ссылку на рынок
        print("📋 Шаг 1: Ввод ссылки на рынок")
        print("-" * 80)
        
        if len(sys.argv) > 1:
            url = sys.argv[1]
        else:
            url = input("Введите ссылку на рынок Opinion.trade: ").strip()
        
        if not url:
            print("❌ URL не указан")
            sys.exit(1)
        
        # Извлекаем marketId из ссылки
        print(f"\n🔗 URL: {url}")
        market_id = parse_market_url(url)
        
        if not market_id:
            print("❌ Не удалось извлечь marketId из URL")
            print("💡 Убедитесь, что URL содержит параметр topicId")
            sys.exit(1)
        
        print(f"✅ Извлечён Market ID: {market_id}\n")
        
        # Получаем информацию о рынке
        market = get_market_info(client, market_id)
        
        if not market:
            print("❌ Не удалось получить информацию о рынке")
            sys.exit(1)
        
        # Получаем стаканы ордеров
        yes_token_id = getattr(market, 'yes_token_id', None)
        no_token_id = getattr(market, 'no_token_id', None)
        
        if not yes_token_id or not no_token_id:
            print("❌ Не удалось определить токены рынка")
            sys.exit(1)
        
        yes_orderbook, no_orderbook = get_orderbooks(client, yes_token_id, no_token_id)
        
        # Шаг 2: Выводим информацию о спреде и ликвидности
        print("\n📊 Шаг 2: Анализ спреда и ликвидности")
        print("-" * 80)
        yes_info, no_info = display_spread_and_liquidity(market, yes_orderbook, no_orderbook)
        
        # Шаг 3: Запрашиваем сумму для фарминга
        print("💰 Шаг 3: Ввод суммы для фарминга")
        print("-" * 80)
        farming_amount = get_farming_amount(client)
        
        if not farming_amount:
            print("❌ Отменено пользователем")
            sys.exit(0)
        
        # Шаг 4: Запрашиваем сторону (YES/NO)
        print("\n📈 Шаг 4: Выбор стороны")
        print("-" * 80)
        side_str, token_name = get_side_choice()
        
        if not side_str:
            print("❌ Отменено пользователем")
            sys.exit(0)
        
        # Определяем токен и текущую цену
        if token_name == "YES":
            token_id = yes_token_id
            current_price = yes_info['mid_price']
            orderbook = yes_orderbook
        else:
            token_id = no_token_id
            current_price = no_info['mid_price']
            orderbook = no_orderbook
        
        if not current_price:
            print(f"❌ Не удалось определить текущую цену для {token_name} токена")
            sys.exit(1)
        
        # Шаг 5: Указываем текущую цену
        print(f"\n💵 Шаг 5: Текущая цена {token_name} токена")
        print("-" * 80)
        print(f"   Текущая цена (Mid Price): {current_price:.6f}")
        print(f"   Это означает вероятность: {current_price * 100:.2f}%")
        
        # Шаг 6: Спрашиваем количество тиков и частоту перестановки
        print(f"\n⚙️  Шаг 6: Настройка параметров перестановки")
        print("-" * 80)
        
        try:
            offset_ticks_str = input("Введите количество тиков от текущей цены (например, 5): ").strip()
            offset_ticks = int(offset_ticks_str)
            
            if offset_ticks < 0:
                print("❌ Количество тиков должно быть неотрицательным")
                sys.exit(1)
            
            # Используем стандартный tick_size для prediction markets (обычно 0.001)
            tick_size = 0.001
            
            # Рассчитываем целевую цену
            # Для BUY ставим ниже текущей цены, для SELL - выше
            # Но сначала нужно определить направление
            print(f"\n📊 Информация о настройках:")
            print(f"   Текущая цена: {current_price:.6f}")
            print(f"   Количество тиков: {offset_ticks}")
            print(f"   Размер тика: {tick_size}")
            
            # Спрашиваем направление (BUY или SELL)
            print(f"\n   Выберите направление ордера:")
            print(f"   1) BUY {token_name} (покупка, цена будет ниже текущей)")
            print(f"   2) SELL {token_name} (продажа, цена будет выше текущей)")
            
            direction_choice = input("   Ваш выбор (1-2): ").strip()
            
            if direction_choice == "1":
                order_side = OrderSide.BUY
                side_display = "BUY"
                target_price, is_valid = calculate_target_price(current_price, "BUY", offset_ticks, tick_size)
            elif direction_choice == "2":
                order_side = OrderSide.SELL
                side_display = "SELL"
                target_price, is_valid = calculate_target_price(current_price, "SELL", offset_ticks, tick_size)
            else:
                print("❌ Неверный выбор")
                sys.exit(1)
            
            # Проверяем валидность цены
            if not is_valid or target_price <= 0:
                print(f"\n   ❌ ОШИБКА: Рассчитанная цена ({target_price:.6f}) невалидна!")
                print(f"   Offset {offset_ticks} тиков слишком большой для текущей цены {current_price:.6f}")
                print(f"   Максимальный offset для BUY: {int(current_price / tick_size)} тиков")
                print(f"   Максимальный offset для SELL: {int((1.0 - current_price) / tick_size)} тиков")
                sys.exit(1)
            
            # Проверяем, не стала ли цена минимальной (значит offset слишком большой)
            if target_price <= 0.000001:
                print(f"\n   ⚠️  ВНИМАНИЕ: Offset {offset_ticks} тиков слишком большой!")
                print(f"   Целевая цена установлена на минимум: {target_price:.6f}")
                print(f"   Рекомендуется уменьшить offset до {int(current_price / tick_size)} тиков для BUY")
            
            print(f"\n   ✅ Целевая цена ордера: {target_price:.6f}")
            print(f"   Отклонение от текущей цены: {abs(current_price - target_price):.6f} ({abs(current_price - target_price) / current_price * 100:.2f}%)")
            
            if offset_ticks > 0:
                print(f"\n   ⚠️  ВНИМАНИЕ: Ордер будет размещён на {offset_ticks} тиков от текущей цены.")
                print(f"   Это означает, что ордер НЕ исполнится сразу при текущей цене.")
                print(f"   Ордер будет ждать, пока цена не сдвинется к вашей целевой цене.")
            else:
                print(f"\n   ⚠️  ВНИМАНИЕ: offset = 0 означает размещение ордера по текущей цене.")
                print(f"   Ордер может исполниться сразу!")
            
            # Спрашиваем частоту перестановки (пока не реализуем автоматическую перестановку)
            print(f"\n   📝 Примечание: Автоматическая перестановка ордеров будет реализована в основной версии бота.")
            print(f"   Сейчас будет размещён только один лимитный ордер.")
            
        except ValueError:
            print("❌ Неверный формат количества тиков")
            sys.exit(1)
        
        # Шаг 7: Показываем все настройки и запрашиваем подтверждение
        print("\n" + "=" * 80)
        print("📋 Шаг 7: Подтверждение настроек")
        print("=" * 80)
        print(f"\n📊 Рынок:")
        print(f"   Market ID: {market_id}")
        print(f"   Название: {market.market_title}")
        print(f"   Токен: {token_name} ({token_id[:20]}...)")
        
        print(f"\n💰 Ордер:")
        print(f"   Направление: {side_display} {token_name}")
        print(f"   Текущая цена: {current_price:.6f}")
        print(f"   Целевая цена: {target_price:.6f}")
        print(f"   Отклонение: {offset_ticks} тиков ({abs(current_price - target_price):.6f})")
        print(f"   Сумма: {farming_amount} USDT")
        
        print(f"\n📈 Спред и ликвидность:")
        if token_name == "YES":
            info = yes_info
        else:
            info = no_info
        
        if info['spread']:
            print(f"   Спред: {info['spread']:.6f} ({info['spread_pct']:.2f}%)")
            print(f"   Ликвидность: {info['total_liquidity']:.2f}")
        
        print("\n" + "=" * 80)
        confirm = input("\n✅ Разместить ордер с этими настройками? (y/n): ").strip().lower()
        
        if confirm != 'y':
            print("❌ Отменено пользователем")
            # Выводим информацию о позициях даже при отмене
            print("\n" + "=" * 80)
            print("📊 ТЕКУЩИЕ ПОЗИЦИИ")
            print("=" * 80)
            display_positions(client)
            sys.exit(0)
        
        # Шаг 8: Размещаем ордер
        print("\n🔄 Шаг 8: Размещение ордера")
        print("-" * 80)
        
        order_params = {
            'market_id': market_id,
            'token_id': token_id,
            'side': order_side,
            'price': str(target_price),
            'amount': farming_amount,
            'token_name': token_name
        }
        
        if place_order(client, order_params):
            print("\n" + "=" * 80)
            print("✅ Ордер успешно размещён!")
            print("=" * 80)
            print(f"\n📋 Итоговая информация:")
            print(f"   Направление: {side_display} {token_name}")
            print(f"   Цена: {target_price:.6f}")
            print(f"   Сумма: {farming_amount} USDT")
            print(f"   Offset: {offset_ticks} тиков")
            if offset_ticks > 0:
                print(f"\n   ⚠️  Ордер НЕ исполнится сразу, так как размещён на {offset_ticks} тиков от текущей цены.")
            print()
            
            # Выводим открытые ордера
            display_order_info(client)
            
            # Выводим информацию о позициях после размещения ордера
            print("=" * 80)
            print("📊 ТЕКУЩИЕ ПОЗИЦИИ")
            print("=" * 80)
            display_positions(client)
        else:
            print("\n❌ Не удалось разместить ордер")
            # Выводим информацию о позициях даже при ошибке
            print("\n" + "=" * 80)
            print("📊 ТЕКУЩИЕ ПОЗИЦИИ")
            print("=" * 80)
            display_positions(client)
            sys.exit(1)
        
    except ValueError as e:
        print(f"\n❌ Ошибка конфигурации: {e}")
        print("\n💡 Убедитесь, что в .env файле указаны все необходимые параметры")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n⚠️ Прервано пользователем")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
