"""
Модуль для работы с базой данных SQLite (асинхронная версия с aiosqlite).

Содержит функции для:
- Инициализации базы данных
- Работы с пользователями (сохранение, получение)
- Работы с ордерами (сохранение, получение)
- Экспорта данных
"""

import csv
import io
import logging
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

import aiosqlite
from service.aes import decrypt, encrypt

# Настройка логирования
logger = logging.getLogger(__name__)

# Путь к базе данных SQLite (в той же папке, что и скрипт)
DB_PATH = Path(__file__).parent / "users.db"


async def init_database():
    """Инициализирует базу данных SQLite."""
    async with aiosqlite.connect(DB_PATH) as conn:
        # Таблица пользователей (упрощенная - только telegram_id, username)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица аккаунтов Opinion
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS opinion_accounts (
                account_id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                wallet_address_cipher BLOB NOT NULL,
                wallet_nonce BLOB NOT NULL,
                private_key_cipher BLOB NOT NULL,
                private_key_nonce BLOB NOT NULL,
                api_key_cipher BLOB NOT NULL,
                api_key_nonce BLOB NOT NULL,
                proxy_cipher BLOB,
                proxy_nonce BLOB,
                proxy_status TEXT DEFAULT 'unknown',
                proxy_last_check TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            )
        """)

        # Создаем индекс для быстрого поиска аккаунтов по telegram_id
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_opinion_accounts_telegram_id ON opinion_accounts(telegram_id)
        """)

        # Таблица ордеров
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                order_id TEXT NOT NULL,
                market_id INTEGER NOT NULL,
                root_market_id INTEGER DEFAULT NULL,
                market_title TEXT,
                token_id TEXT NOT NULL,
                token_name TEXT NOT NULL,
                side TEXT NOT NULL,
                current_price REAL NOT NULL,
                target_price REAL NOT NULL,
                offset_ticks INTEGER NOT NULL,
                offset_cents REAL NOT NULL,
                amount REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                reposition_threshold_cents REAL DEFAULT 0.5,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES opinion_accounts(account_id)
            )
        """)

        # Миграция: добавляем поле root_market_id для существующих таблиц
        try:
            await conn.execute("""
                ALTER TABLE orders ADD COLUMN root_market_id INTEGER DEFAULT NULL
            """)
        except aiosqlite.OperationalError:
            # Колонка уже существует, игнорируем ошибку
            pass

        # Создаем индекс для быстрого поиска ордеров по account_id
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_account_id ON orders(account_id)
        """)

        # Создаем индекс для поиска по order_id
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id)
        """)

        # Таблица инвайтов
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invite TEXT NOT NULL UNIQUE,
                telegram_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                used_at TIMESTAMP,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            )
        """)

        # Таблица мониторинга кошельков
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS wallet_monitor (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL,
                label TEXT NOT NULL,
                tguserid INTEGER NOT NULL,
                lastruntime INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                lastcountorders INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (tguserid) REFERENCES users(telegram_id),
                UNIQUE(tguserid, address)
            )
        """)

        # Создаем индексы для быстрого поиска
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_invites_invite ON invites(invite)
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_invites_telegram_id ON invites(telegram_id)
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_wallet_monitor_user ON wallet_monitor(tguserid)
        """)

        # Добавляем поле reposition_threshold_cents если его нет (миграция)
        try:
            await conn.execute("""
                ALTER TABLE orders ADD COLUMN reposition_threshold_cents REAL DEFAULT 0.5
            """)
            await conn.commit()
            logger.info("Добавлено поле reposition_threshold_cents в таблицу orders")
        except aiosqlite.OperationalError as e:
            # Поле уже существует, игнорируем ошибку
            if "duplicate column" not in str(e).lower():
                logger.warning(
                    f"Ошибка при добавлении поля reposition_threshold_cents: {e}"
                )

        await conn.commit()
    logger.info("База данных инициализирована")

    # Выполняем миграцию статусов ордеров
    await migrate_order_statuses()


async def migrate_order_statuses():
    """
    Миграция статусов ордеров: обновляет старые статусы на новые.
    active -> pending
    filled -> finished
    cancelled -> canceled

    Также обновляет DEFAULT значение в схеме таблицы, если оно старое.
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        # Проверяем, существует ли таблица orders
        cursor = await conn.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='orders'
        """)
        table_exists = await cursor.fetchone()

        if not table_exists:
            # Таблица не существует, миграция не нужна
            return

        # Обновляем статусы в существующих записях
        cursor = await conn.execute("""
            UPDATE orders 
            SET status = CASE 
                WHEN status = 'active' THEN 'pending'
                WHEN status = 'filled' THEN 'finished'
                WHEN status = 'cancelled' THEN 'canceled'
                ELSE status
            END
            WHERE status IN ('active', 'filled', 'cancelled')
        """)
        rows_affected = cursor.rowcount
        await conn.commit()

        if rows_affected > 0:
            logger.info(
                f"Миграция статусов ордеров завершена: обновлено {rows_affected} записей"
            )
        else:
            logger.debug("Миграция статусов ордеров: нет записей для обновления")

        # Примечание: В SQLite нельзя изменить DEFAULT значение существующей колонки.
        # Однако DEFAULT не используется на практике, так как:
        # 1. В функции save_order() статус всегда передается явно (есть дефолт в Python: 'pending')
        # 2. В market_router.py статус передается явно: status='pending'
        # 3. В INSERT запросе статус всегда указан в VALUES
        # Поэтому даже если в схеме таблицы остался старый DEFAULT 'active', это не влияет на работу.


async def get_user(telegram_id: int) -> Optional[dict]:
    """
    Получает данные пользователя из базы данных.

    Args:
        telegram_id: ID пользователя в Telegram

    Returns:
        dict: Словарь с данными пользователя или None, если пользователь не найден
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT telegram_id, username, created_at FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        return None

    return {
        "telegram_id": row[0],
        "username": row[1],
        "created_at": row[2],
    }


async def save_user(
    telegram_id: int,
    username: Optional[str],
):
    """
    Сохраняет данные пользователя в базу данных.

    Args:
        telegram_id: ID пользователя в Telegram
        username: Имя пользователя (опционально)
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT OR REPLACE INTO users 
            (telegram_id, username)
            VALUES (?, ?)
        """,
            (telegram_id, username),
        )

        await conn.commit()
    logger.info(f"Пользователь {telegram_id} сохранен в базу данных")


async def save_order(
    account_id: int,
    order_id: str,
    market_id: int,
    market_title: Optional[str],
    token_id: str,
    token_name: str,
    side: str,
    current_price: float,
    target_price: float,
    offset_ticks: int,
    offset_cents: float,
    amount: float,
    status: str = "pending",
    reposition_threshold_cents: float = 0.5,
    root_market_id: Optional[int] = None,
):
    """
    Сохраняет информацию об ордере в базу данных.

    Args:
        account_id: ID аккаунта Opinion
        order_id: ID ордера на бирже
        market_id: ID рынка
        market_title: Название рынка
        token_id: ID токена (YES/NO)
        token_name: Название токена (YES/NO)
        side: Направление ордера (BUY/SELL)
        current_price: Текущая цена на момент размещения ордера
        target_price: Цена по которой размещен ордер
        offset_ticks: Отступ в тиках
        offset_cents: Отступ в центах
        amount: Сумма ордера в USDT
        status: Статус ордера (pending/finished/canceled)
        reposition_threshold_cents: Порог отклонения в центах для перестановки ордера
        root_market_id: ID корневого маркета (для categorical markets), None для binary markets
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO orders 
            (account_id, order_id, market_id, root_market_id, market_title, token_id, token_name, 
             side, current_price, target_price, offset_ticks, offset_cents, amount, status, reposition_threshold_cents)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                account_id,
                order_id,
                market_id,
                root_market_id,
                market_title,
                token_id,
                token_name,
                side,
                current_price,
                target_price,
                offset_ticks,
                offset_cents,
                amount,
                status,
                reposition_threshold_cents,
            ),
        )

        await conn.commit()
    logger.info(f"Ордер {order_id} сохранен в базу данных для аккаунта {account_id}")


async def get_account_orders(
    account_id: int, status: Optional[str] = None, market_id: Optional[int] = None
) -> list:
    """
    Получает список ордеров аккаунта из базы данных.

    Args:
        account_id: ID аккаунта Opinion
        status: Фильтр по статусу (pending/finished/canceled). Если None, возвращает все ордера.
        market_id: Фильтр по market_id. Если None, возвращает ордера для всех рынков.

    Returns:
        list: Список словарей с данными ордеров
    """
    # Явно указываем колонки в правильном порядке
    columns = [
        "id",
        "account_id",
        "order_id",
        "market_id",
        "root_market_id",
        "market_title",
        "token_id",
        "token_name",
        "side",
        "current_price",
        "target_price",
        "offset_ticks",
        "offset_cents",
        "amount",
        "status",
        "reposition_threshold_cents",
        "created_at",
    ]

    async with aiosqlite.connect(DB_PATH) as conn:
        # Формируем условия WHERE динамически
        conditions = ["account_id = ?"]
        params = [account_id]

        if status:
            conditions.append("status = ?")
            params.append(status)

        if market_id is not None:
            conditions.append("market_id = ?")
            params.append(market_id)

        where_clause = " AND ".join(conditions)

        async with conn.execute(
            f"""
            SELECT {", ".join(columns)} FROM orders 
            WHERE {where_clause}
            ORDER BY created_at DESC
            """,
            tuple(params),
        ) as cursor:
            rows = await cursor.fetchall()

    orders = []
    for row in rows:
        order_dict = dict(zip(columns, row))
        orders.append(order_dict)

    return orders


async def get_user_orders(telegram_id: int, status: Optional[str] = None) -> list:
    """
    Получает список ордеров пользователя из базы данных (через аккаунты).

    Args:
        telegram_id: ID пользователя в Telegram
        status: Фильтр по статусу (pending/finished/canceled). Если None, возвращает все ордера.

    Returns:
        list: Список словарей с данными ордеров
    """
    # Получаем все аккаунты пользователя
    accounts = await get_user_accounts(telegram_id)
    if not accounts:
        return []

    account_ids = [acc["account_id"] for acc in accounts]
    placeholders = ",".join(["?"] * len(account_ids))

    columns = [
        "id",
        "account_id",
        "order_id",
        "market_id",
        "root_market_id",
        "market_title",
        "token_id",
        "token_name",
        "side",
        "current_price",
        "target_price",
        "offset_ticks",
        "offset_cents",
        "amount",
        "status",
        "reposition_threshold_cents",
        "created_at",
    ]

    async with aiosqlite.connect(DB_PATH) as conn:
        if status:
            query = f"""
                SELECT {", ".join(columns)} FROM orders 
                WHERE account_id IN ({placeholders}) AND status = ?
                ORDER BY created_at DESC
            """
            params = (*account_ids, status)
        else:
            query = f"""
                SELECT {", ".join(columns)} FROM orders 
                WHERE account_id IN ({placeholders})
                ORDER BY created_at DESC
            """
            params = account_ids

        async with conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()

    orders = []
    for row in rows:
        order_dict = dict(zip(columns, row))
        orders.append(order_dict)

    return orders


async def get_order_by_id(order_id: str) -> Optional[dict]:
    """
    Получает ордер по его ID из базы данных.

    Args:
        order_id: ID ордера на бирже

    Returns:
        dict: Словарь с данными ордера или None, если ордер не найден
    """
    # Явно указываем колонки в правильном порядке
    columns = [
        "id",
        "account_id",
        "order_id",
        "market_id",
        "root_market_id",
        "market_title",
        "token_id",
        "token_name",
        "side",
        "current_price",
        "target_price",
        "offset_ticks",
        "offset_cents",
        "amount",
        "status",
        "reposition_threshold_cents",
        "created_at",
    ]

    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            f"""
            SELECT {", ".join(columns)} FROM orders 
            WHERE order_id = ?
        """,
            (order_id,),
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        return None

    order_dict = dict(zip(columns, row))
    return order_dict


async def update_order_status(order_id: str, status: str):
    """
    Обновляет статус ордера в базе данных.

    Args:
        order_id: ID ордера на бирже
        status: Новый статус (pending/finished/canceled)
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            UPDATE orders 
            SET status = ?
            WHERE order_id = ?
        """,
            (status, order_id),
        )

        await conn.commit()
    logger.info(f"Статус ордера {order_id} обновлен на {status}")


async def update_order_in_db(
    old_order_id: str,
    new_order_id: str,
    new_current_price: float,
    new_target_price: float,
):
    """
    Обновляет order_id и цену ордера в БД.

    Args:
        old_order_id: Старый ID ордера
        new_order_id: Новый ID ордера
        new_current_price: Новая текущая цена
        new_target_price: Новая целевая цена
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            UPDATE orders 
            SET order_id = ?, current_price = ?, target_price = ?
            WHERE order_id = ?
        """,
            (new_order_id, new_current_price, new_target_price, old_order_id),
        )

        await conn.commit()
    logger.info(f"Обновлен ордер {old_order_id} -> {new_order_id} в БД")


async def get_all_users():
    """Получает список всех пользователей из БД."""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT telegram_id FROM users") as cursor:
            rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def save_opinion_account(
    telegram_id: int,
    wallet_address: str,
    private_key: str,
    api_key: str,
    proxy_str: str,
    proxy_status: str,
) -> int:
    """
    Сохраняет аккаунт Opinion в базу данных с шифрованием.

    Args:
        telegram_id: ID пользователя в Telegram
        wallet_address: Адрес кошелька
        private_key: Приватный ключ
        api_key: API ключ
        proxy_str: Прокси в формате ip:port:login:password (обязательно)
        proxy_status: Статус прокси ('working', 'failed', 'unknown')

    Returns:
        int: ID созданного аккаунта
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        # Шифруем данные
        wallet_cipher, wallet_nonce = encrypt(wallet_address)
        private_key_cipher, private_key_nonce = encrypt(private_key)
        api_key_cipher, api_key_nonce = encrypt(api_key)
        proxy_cipher, proxy_nonce = encrypt(proxy_str)

        # Сохраняем аккаунт
        cursor = await conn.execute(
            """
            INSERT INTO opinion_accounts 
            (telegram_id, wallet_address_cipher, wallet_nonce, 
             private_key_cipher, private_key_nonce, api_key_cipher, api_key_nonce,
             proxy_cipher, proxy_nonce, proxy_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                telegram_id,
                wallet_cipher,
                wallet_nonce,
                private_key_cipher,
                private_key_nonce,
                api_key_cipher,
                api_key_nonce,
                proxy_cipher,
                proxy_nonce,
                proxy_status,
            ),
        )

        account_id = cursor.lastrowid
        await conn.commit()
    logger.info(f"Аккаунт Opinion {account_id} сохранен для пользователя {telegram_id}")
    return account_id


async def get_opinion_account(account_id: int) -> Optional[dict]:
    """
    Получает данные аккаунта Opinion из базы данных.

    Args:
        account_id: ID аккаунта Opinion

    Returns:
        dict: Словарь с данными аккаунта или None, если аккаунт не найден
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            """
            SELECT account_id, telegram_id, wallet_address_cipher, wallet_nonce,
                   private_key_cipher, private_key_nonce, api_key_cipher, api_key_nonce,
                   proxy_cipher, proxy_nonce, proxy_status, proxy_last_check, created_at
            FROM opinion_accounts WHERE account_id = ?
            """,
            (account_id,),
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        return None

    # Расшифровываем данные
    try:
        wallet_address = decrypt(row[2], row[3])
        private_key = decrypt(row[4], row[5])
        api_key = decrypt(row[6], row[7])
        proxy_str = decrypt(row[8], row[9])

        return {
            "account_id": row[0],
            "telegram_id": row[1],
            "wallet_address": wallet_address,
            "private_key": private_key,
            "api_key": api_key,
            "proxy_str": proxy_str,
            "proxy_status": row[10],
            "proxy_last_check": row[11],
            "created_at": row[12],
        }
    except Exception as e:
        logger.error(f"Ошибка расшифровки данных аккаунта {account_id}: {e}")
        return None


async def get_user_accounts(telegram_id: int) -> list:
    """
    Получает список всех аккаунтов Opinion пользователя.

    Args:
        telegram_id: ID пользователя в Telegram

    Returns:
        list: Список словарей с данными аккаунтов
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            """
            SELECT account_id, telegram_id, wallet_address_cipher, wallet_nonce,
                   private_key_cipher, private_key_nonce, api_key_cipher, api_key_nonce,
                   proxy_cipher, proxy_nonce, proxy_status, proxy_last_check, created_at
            FROM opinion_accounts WHERE telegram_id = ?
            ORDER BY created_at ASC
            """,
            (telegram_id,),
        ) as cursor:
            rows = await cursor.fetchall()

    accounts = []
    for row in rows:
        try:
            wallet_address = decrypt(row[2], row[3])
            private_key = decrypt(row[4], row[5])
            api_key = decrypt(row[6], row[7])
            proxy_str = decrypt(row[8], row[9])

            accounts.append(
                {
                    "account_id": row[0],
                    "telegram_id": row[1],
                    "wallet_address": wallet_address,
                    "private_key": private_key,
                    "api_key": api_key,
                    "proxy_str": proxy_str,
                    "proxy_status": row[10],
                    "proxy_last_check": row[11],
                    "created_at": row[12],
                }
            )
        except Exception as e:
            logger.error(f"Ошибка расшифровки данных аккаунта {row[0]}: {e}")
            continue

    return accounts


async def update_proxy_status(
    account_id: int, status: str, last_check: Optional[str] = None
):
    """
    Обновляет статус прокси для аккаунта.

    Args:
        account_id: ID аккаунта Opinion
        status: Статус прокси ('working', 'failed', 'unknown')
        last_check: Время последней проверки (опционально, по умолчанию CURRENT_TIMESTAMP)
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        if last_check:
            await conn.execute(
                """
                UPDATE opinion_accounts 
                SET proxy_status = ?, proxy_last_check = ?
                WHERE account_id = ?
                """,
                (status, last_check, account_id),
            )
        else:
            await conn.execute(
                """
                UPDATE opinion_accounts 
                SET proxy_status = ?, proxy_last_check = CURRENT_TIMESTAMP
                WHERE account_id = ?
                """,
                (status, account_id),
            )

        await conn.commit()
    logger.info(f"Статус прокси для аккаунта {account_id} обновлен на {status}")


async def get_all_pending_orders_with_accounts(market_id: Optional[int] = None) -> list:
    """
    Получает все pending ордера с JOIN к аккаунтам для синхронизации.

    Args:
        market_id: Фильтр по market_id. Если None, возвращает ордера для всех рынков.

    Returns:
        list: Список словарей с данными ордеров и аккаунтов
    """
    columns = [
        "o.id",
        "o.account_id",
        "o.order_id",
        "o.market_id",
        "o.root_market_id",
        "o.market_title",
        "o.token_id",
        "o.token_name",
        "o.side",
        "o.current_price",
        "o.target_price",
        "o.offset_ticks",
        "o.offset_cents",
        "o.amount",
        "o.status",
        "o.reposition_threshold_cents",
        "o.created_at",
        "a.telegram_id",
        "a.wallet_address_cipher",
        "a.wallet_nonce",
        "a.private_key_cipher",
        "a.private_key_nonce",
        "a.api_key_cipher",
        "a.api_key_nonce",
        "a.proxy_cipher",
        "a.proxy_nonce",
        "a.proxy_status",
    ]

    # Формируем условия WHERE динамически
    conditions = ["o.status = 'pending'"]
    params = []

    if market_id is not None:
        conditions.append("o.market_id = ?")
        params.append(market_id)

    where_clause = " AND ".join(conditions)

    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            f"""
            SELECT {", ".join(columns)}
            FROM orders o
            INNER JOIN opinion_accounts a ON o.account_id = a.account_id
            WHERE {where_clause}
            ORDER BY a.account_id, o.created_at ASC
            """,
            tuple(params) if params else (),
        ) as cursor:
            rows = await cursor.fetchall()

    result = []
    for row in rows:
        try:
            # Расшифровываем данные аккаунта
            wallet_address = decrypt(row[18], row[19])
            private_key = decrypt(row[20], row[21])
            api_key = decrypt(row[22], row[23])
            proxy_str = decrypt(row[24], row[25])

            result.append(
                {
                    "order": {
                        "id": row[0],
                        "account_id": row[1],
                        "order_id": row[2],
                        "market_id": row[3],
                        "root_market_id": row[4],
                        "market_title": row[5],
                        "token_id": row[6],
                        "token_name": row[7],
                        "side": row[8],
                        "current_price": row[9],
                        "target_price": row[10],
                        "offset_ticks": row[11],
                        "offset_cents": row[12],
                        "amount": row[13],
                        "status": row[14],
                        "reposition_threshold_cents": row[15],
                        "created_at": row[16],
                    },
                    "account": {
                        "account_id": row[1],
                        "telegram_id": row[17],
                        "wallet_address": wallet_address,
                        "private_key": private_key,
                        "api_key": api_key,
                        "proxy_str": proxy_str,
                        "proxy_status": row[25],
                    },
                }
            )
        except Exception as e:
            logger.error(f"Ошибка расшифровки данных для ордера {row[2]}: {e}")
            continue

    return result


async def delete_opinion_account(account_id: int) -> bool:
    """
    Удаляет аккаунт Opinion (только если нет активных ордеров).

    Args:
        account_id: ID аккаунта Opinion

    Returns:
        bool: True если аккаунт был удален, False если аккаунт не найден или есть активные ордера
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        # Проверяем, существует ли аккаунт
        async with conn.execute(
            "SELECT account_id FROM opinion_accounts WHERE account_id = ?",
            (account_id,),
        ) as cursor:
            account_exists = await cursor.fetchone()

        if not account_exists:
            logger.warning(f"Попытка удалить несуществующий аккаунт {account_id}")
            return False

        # Проверяем, есть ли активные ордера
        async with conn.execute(
            "SELECT COUNT(*) FROM orders WHERE account_id = ? AND status = 'pending'",
            (account_id,),
        ) as cursor:
            active_orders_count = (await cursor.fetchone())[0]

        if active_orders_count > 0:
            logger.warning(
                f"Нельзя удалить аккаунт {account_id}: есть {active_orders_count} активных ордеров"
            )
            return False

        # Удаляем все ордера аккаунта
        async with conn.execute(
            "DELETE FROM orders WHERE account_id = ?", (account_id,)
        ) as cursor:
            orders_deleted = cursor.rowcount

        # Удаляем аккаунт
        await conn.execute(
            "DELETE FROM opinion_accounts WHERE account_id = ?", (account_id,)
        )

        await conn.commit()

        logger.info(
            f"Аккаунт {account_id} удален из БД (удалено {orders_deleted} ордеров)"
        )
        return True


async def delete_user(telegram_id: int) -> bool:
    """
    Удаляет пользователя, все его аккаунты, ордера и очищает использованные инвайты из базы данных.

    Args:
        telegram_id: ID пользователя в Telegram

    Returns:
        bool: True если пользователь был удален, False если пользователь не найден
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        # Проверяем, существует ли пользователь
        async with conn.execute(
            "SELECT telegram_id FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            user_exists = await cursor.fetchone()

        if not user_exists:
            logger.warning(
                f"Попытка удалить несуществующего пользователя {telegram_id}"
            )
            return False

        # Получаем все аккаунты пользователя
        accounts = await get_user_accounts(telegram_id)
        account_ids = [acc["account_id"] for acc in accounts]

        # Удаляем все ордера всех аккаунтов пользователя
        orders_deleted = 0
        if account_ids:
            placeholders = ",".join(["?"] * len(account_ids))
            async with conn.execute(
                f"DELETE FROM orders WHERE account_id IN ({placeholders})", account_ids
            ) as cursor:
                orders_deleted = cursor.rowcount

        # Удаляем все аккаунты пользователя
        accounts_deleted = 0
        if account_ids:
            async with conn.execute(
                "DELETE FROM opinion_accounts WHERE telegram_id = ?", (telegram_id,)
            ) as cursor:
                accounts_deleted = cursor.rowcount

        # Очищаем использованные инвайты пользователя (чтобы они снова стали доступны)
        async with conn.execute(
            "UPDATE invites SET telegram_id = NULL, used_at = NULL WHERE telegram_id = ?",
            (telegram_id,),
        ) as cursor:
            invites_cleared = cursor.rowcount

        # Удаляем пользователя
        await conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))

        await conn.commit()

        logger.info(
            f"Пользователь {telegram_id} удален из БД (удалено {accounts_deleted} аккаунтов, {orders_deleted} ордеров, очищено {invites_cleared} инвайтов)"
        )
        return True


async def check_wallet_address_exists(
    wallet_address: str, telegram_id: Optional[int] = None
) -> bool:
    """
    Проверяет, существует ли уже аккаунт с таким wallet_address.

    Args:
        wallet_address: Адрес кошелька для проверки
        telegram_id: ID пользователя (не используется, оставлен для обратной совместимости)

    Returns:
        bool: True если wallet_address уже существует, False если уникален
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT wallet_address_cipher, wallet_nonce FROM opinion_accounts"
        ) as cursor:
            rows = await cursor.fetchall()

    for row in rows:
        try:
            existing_wallet = decrypt(row[0], row[1])
            if existing_wallet == wallet_address:
                return True
        except Exception as e:
            logger.warning(
                f"Ошибка при расшифровке wallet_address для проверки уникальности: {e}"
            )
            continue

    return False


async def check_private_key_exists(
    private_key: str, telegram_id: Optional[int] = None
) -> bool:
    """
    Проверяет, существует ли уже аккаунт с таким private_key.

    Args:
        private_key: Приватный ключ для проверки
        telegram_id: ID пользователя (не используется, оставлен для обратной совместимости)

    Returns:
        bool: True если private_key уже существует, False если уникален
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT private_key_cipher, private_key_nonce FROM opinion_accounts"
        ) as cursor:
            rows = await cursor.fetchall()

    for row in rows:
        try:
            existing_private_key = decrypt(row[0], row[1])
            if existing_private_key == private_key:
                return True
        except Exception as e:
            logger.warning(
                f"Ошибка при расшифровке private_key для проверки уникальности: {e}"
            )
            continue

    return False


async def check_api_key_exists(api_key: str, telegram_id: Optional[int] = None) -> bool:
    """
    Проверяет, существует ли уже аккаунт с таким api_key.

    Args:
        api_key: API ключ для проверки
        telegram_id: ID пользователя (не используется, оставлен для обратной совместимости)

    Returns:
        bool: True если api_key уже существует, False если уникален
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT api_key_cipher, api_key_nonce FROM opinion_accounts"
        ) as cursor:
            rows = await cursor.fetchall()

    for row in rows:
        try:
            existing_api_key = decrypt(row[0], row[1])
            if existing_api_key == api_key:
                return True
        except Exception as e:
            logger.warning(
                f"Ошибка при расшифровке api_key для проверки уникальности: {e}"
            )
            continue

    return False


async def check_proxy_exists(proxy_str: str) -> bool:
    """
    Проверяет, существует ли уже аккаунт с таким прокси.

    Args:
        proxy_str: Строка прокси в формате ip:port:login:password

    Returns:
        bool: True если прокси уже используется, False если уникален
    """
    if not proxy_str:
        return False

    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT proxy_cipher, proxy_nonce FROM opinion_accounts WHERE proxy_cipher IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()

    for row in rows:
        try:
            existing_proxy = decrypt(row[0], row[1])
            if existing_proxy == proxy_str:
                return True
        except Exception as e:
            logger.warning(
                f"Ошибка при расшифровке proxy для проверки уникальности: {e}"
            )
            continue

    return False


async def export_table_to_csv(conn: aiosqlite.Connection, table_name: str) -> str:
    """
    Экспортирует одну таблицу в CSV формат.

    Args:
        conn: Соединение с базой данных
        table_name: Название таблицы

    Returns:
        str: CSV содержимое в виде строки
    """
    async with conn.execute(f"SELECT * FROM {table_name}") as cursor:
        rows = await cursor.fetchall()
        # Получаем названия колонок
        column_names = [description[0] for description in cursor.description]

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
                # Конвертируем BLOB в hex строку
                csv_row.append(value.hex())
            else:
                csv_row.append(value)
        writer.writerow(csv_row)

    return output.getvalue()


async def export_users_to_csv() -> str:
    """
    Экспортирует таблицу users в CSV формат.

    Returns:
        str: CSV содержимое в виде строки
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        return await export_table_to_csv(conn, "users")


async def add_wallet_monitor(address: str, label: str, tguserid: int) -> bool:
    """
    Добавляет запись мониторинга кошелька.

    Returns:
        bool: True если запись добавлена, False если уже существует.
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        try:
            await conn.execute(
                """
                INSERT INTO wallet_monitor (address, label, tguserid)
                VALUES (?, ?, ?)
                """,
                (address, label, tguserid),
            )
            await conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_wallet_monitor(address: str, tguserid: int) -> bool:
    """
    Удаляет запись мониторинга кошелька.

    Returns:
        bool: True если запись удалена, False если не найдена.
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            """
            DELETE FROM wallet_monitor
            WHERE address = ? AND tguserid = ?
            """,
            (address, tguserid),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def get_wallet_monitor_by_user(address: str, tguserid: int) -> Optional[dict]:
    """Возвращает запись мониторинга по адресу и пользователю."""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            """
            SELECT id, address, label, tguserid, lastruntime, lastcountorders, created_at
            FROM wallet_monitor
            WHERE address = ? AND tguserid = ?
            """,
            (address, tguserid),
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        return None

    return {
        "id": row[0],
        "address": row[1],
        "label": row[2],
        "tguserid": row[3],
        "lastruntime": row[4],
        "lastcountorders": row[5],
        "created_at": row[6],
    }


async def get_wallet_monitors() -> List[Dict]:
    """Возвращает список всех записей мониторинга кошельков."""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            """
            SELECT id, address, label, tguserid, lastruntime, lastcountorders, created_at
            FROM wallet_monitor
            ORDER BY id ASC
            """
        ) as cursor:
            rows = await cursor.fetchall()

    monitors: List[Dict] = []
    for row in rows:
        monitors.append(
            {
                "id": row[0],
                "address": row[1],
                "label": row[2],
                "tguserid": row[3],
                "lastruntime": row[4],
                "lastcountorders": row[5],
                "created_at": row[6],
            }
        )
    return monitors


async def get_wallet_monitors_by_user(tguserid: int) -> List[Dict]:
    """Возвращает список записей мониторинга кошельков для пользователя."""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            """
            SELECT id, address, label, tguserid, lastruntime, lastcountorders, created_at
            FROM wallet_monitor
            WHERE tguserid = ?
            ORDER BY id ASC
            """,
            (tguserid,),
        ) as cursor:
            rows = await cursor.fetchall()

    monitors: List[Dict] = []
    for row in rows:
        monitors.append(
            {
                "id": row[0],
                "address": row[1],
                "label": row[2],
                "tguserid": row[3],
                "lastruntime": row[4],
                "lastcountorders": row[5],
                "created_at": row[6],
            }
        )
    return monitors


async def update_wallet_monitor_runtime(monitor_id: int, lastruntime: int) -> None:
    """Обновляет время последней обработки в Unix timestamp."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            UPDATE wallet_monitor
            SET lastruntime = ?
            WHERE id = ?
            """,
            (lastruntime, monitor_id),
        )
        await conn.commit()


async def update_wallet_monitor_orders_count(
    monitor_id: int, lastcountorders: int
) -> None:
    """Обновляет количество позиций (lastcountorders)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            UPDATE wallet_monitor
            SET lastcountorders = ?
            WHERE id = ?
            """,
            (lastcountorders, monitor_id),
        )
        await conn.commit()


async def export_all_tables_to_zip() -> bytes:
    """
    Экспортирует все таблицы из базы данных в ZIP архив с CSV файлами.

    Returns:
        bytes: ZIP архив в виде байтов
    """
    # Создаем ZIP архив в памяти
    zip_buffer = io.BytesIO()

    async with aiosqlite.connect(DB_PATH) as conn:
        # Получаем список всех таблиц
        async with conn.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
        """) as cursor:
            tables = await cursor.fetchall()
            table_names = [row[0] for row in tables]

        # Экспортируем каждую таблицу в CSV и добавляем в ZIP
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for table_name in table_names:
                try:
                    csv_content = await export_table_to_csv(conn, table_name)
                    # Добавляем CSV файл в ZIP с именем таблицы
                    zip_file.writestr(f"{table_name}.csv", csv_content.encode("utf-8"))
                    logger.info(f"Экспортирована таблица {table_name}")
                except Exception as e:
                    logger.error(f"Ошибка при экспорте таблицы {table_name}: {e}")
                    # Добавляем файл с ошибкой
                    zip_file.writestr(
                        f"{table_name}_error.txt", f"Error exporting table: {e}"
                    )

    zip_buffer.seek(0)
    return zip_buffer.read()


async def get_database_statistics() -> dict:
    """
    Получает статистику по базе данных.

    Returns:
        dict: Словарь со статистикой:
        {
            'users': {
                'total': int,
                'with_orders': int,
                'with_active_orders': int
            },
            'orders': {
                'total': int,
                'unique_markets': int,
                'by_status': {
                    'FILLED': {'count': int, 'amount': float},
                    'OPEN': {'count': int, 'amount': float},
                    'CANCELLED': {'count': int, 'amount': float},
                    'EXPIRED': {'count': int, 'amount': float},
                    'INVALIDATED': {'count': int, 'amount': float}
                },
                'total_amount': float,
                'average_amount': float
            }
        }
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        # Статистика по пользователям
        async with conn.execute("SELECT COUNT(*) FROM users") as cursor:
            total_users = (await cursor.fetchone())[0]

        async with conn.execute(
            """
            SELECT COUNT(DISTINCT a.telegram_id) 
            FROM orders o
            INNER JOIN opinion_accounts a ON o.account_id = a.account_id
            """
        ) as cursor:
            users_with_orders = (await cursor.fetchone())[0]

        async with conn.execute(
            """
            SELECT COUNT(DISTINCT a.telegram_id) 
            FROM orders o
            INNER JOIN opinion_accounts a ON o.account_id = a.account_id
            WHERE o.status IN ('pending', 'OPEN')
            """
        ) as cursor:
            users_with_active_orders = (await cursor.fetchone())[0]

        # Статистика по ордерам
        async with conn.execute("SELECT COUNT(*) FROM orders") as cursor:
            total_orders = (await cursor.fetchone())[0]

        async with conn.execute(
            "SELECT COUNT(DISTINCT market_id) FROM orders"
        ) as cursor:
            unique_markets = (await cursor.fetchone())[0]

        # Статистика по статусам (маппим старые статусы на новые)
        # pending -> OPEN, finished -> FILLED, canceled -> CANCELLED
        status_mapping = {
            "pending": "OPEN",
            "finished": "FILLED",
            "canceled": "CANCELLED",
            "OPEN": "OPEN",
            "FILLED": "FILLED",
            "CANCELLED": "CANCELLED",
        }

        orders_by_status = {
            "FILLED": {"count": 0, "amount": 0.0},
            "OPEN": {"count": 0, "amount": 0.0},
            "CANCELLED": {"count": 0, "amount": 0.0},
        }

        # Получаем все ордера со статусами и суммами
        async with conn.execute("SELECT status, amount FROM orders") as cursor:
            rows = await cursor.fetchall()

        total_amount = 0.0
        for status, amount in rows:
            # Нормализуем статус (приводим к нижнему регистру для маппинга)
            status_str = str(status).lower() if status else ""
            # Маппим статус
            mapped_status = status_mapping.get(status_str, "CANCELLED")
            if mapped_status not in orders_by_status:
                # Неизвестный статус - считаем как отмененный
                mapped_status = "CANCELLED"

            orders_by_status[mapped_status]["count"] += 1
            orders_by_status[mapped_status]["amount"] += float(amount or 0)
            total_amount += float(amount or 0)

        # Средняя сумма ордера
        average_amount = total_amount / total_orders if total_orders > 0 else 0.0

        return {
            "users": {
                "total": total_users,
                "with_orders": users_with_orders,
                "with_active_orders": users_with_active_orders,
            },
            "orders": {
                "total": total_orders,
                "unique_markets": unique_markets,
                "by_status": orders_by_status,
                "total_amount": total_amount,
                "average_amount": average_amount,
            },
        }
