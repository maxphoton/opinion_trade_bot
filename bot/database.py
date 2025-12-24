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
import aiosqlite
from pathlib import Path
from typing import Optional

from aes import encrypt, decrypt

# Настройка логирования
logger = logging.getLogger(__name__)

# Путь к базе данных SQLite (в той же папке, что и скрипт)
DB_PATH = Path(__file__).parent / "users.db"


async def init_database():
    """Инициализирует базу данных SQLite."""
    async with aiosqlite.connect(DB_PATH) as conn:
        # Таблица пользователей
        await conn.execute("""
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
        
        # Таблица ордеров
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                order_id TEXT NOT NULL,
                market_id INTEGER NOT NULL,
                market_title TEXT,
                token_id TEXT NOT NULL,
                token_name TEXT NOT NULL,
                side TEXT NOT NULL,
                current_price REAL NOT NULL,
                target_price REAL NOT NULL,
                offset_ticks INTEGER NOT NULL,
                offset_cents REAL NOT NULL,
                amount REAL NOT NULL,
                status TEXT DEFAULT 'active',
                reposition_threshold_cents REAL DEFAULT 0.5,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            )
        """)
        
        # Создаем индекс для быстрого поиска ордеров по telegram_id
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_telegram_id ON orders(telegram_id)
        """)
        
        # Создаем индекс для поиска по order_id
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id)
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
                logger.warning(f"Ошибка при добавлении поля reposition_threshold_cents: {e}")
        
        await conn.commit()
    logger.info("База данных инициализирована")


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
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
    
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


async def save_user(telegram_id: int, username: Optional[str], wallet_address: str, 
              private_key: str, api_key: str):
    """
    Сохраняет данные пользователя в базу данных с шифрованием.
    
    Args:
        telegram_id: ID пользователя в Telegram
        username: Имя пользователя (опционально)
        wallet_address: Адрес кошелька
        private_key: Приватный ключ
        api_key: API ключ
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        # Шифруем данные
        wallet_cipher, wallet_nonce = encrypt(wallet_address)
        private_key_cipher, private_key_nonce = encrypt(private_key)
        api_key_cipher, api_key_nonce = encrypt(api_key)
        
        # Сохраняем или обновляем пользователя
        await conn.execute("""
            INSERT OR REPLACE INTO users 
            (telegram_id, username, wallet_address, wallet_nonce, 
             private_key_cipher, private_key_nonce, api_key_cipher, api_key_nonce)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            telegram_id, username, wallet_cipher, wallet_nonce,
            private_key_cipher, private_key_nonce, api_key_cipher, api_key_nonce
        ))
        
        await conn.commit()
    logger.info(f"Пользователь {telegram_id} сохранен в базу данных")


async def save_order(telegram_id: int, order_id: str, market_id: int, market_title: Optional[str],
               token_id: str, token_name: str, side: str, current_price: float,
               target_price: float, offset_ticks: int, offset_cents: float, amount: float,
               status: str = 'active', reposition_threshold_cents: float = 0.5):
    """
    Сохраняет информацию об ордере в базу данных.
    
    Args:
        telegram_id: ID пользователя в Telegram
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
        status: Статус ордера (active/cancelled/filled)
        reposition_threshold_cents: Порог отклонения в центах для перестановки ордера
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            INSERT INTO orders 
            (telegram_id, order_id, market_id, market_title, token_id, token_name, 
             side, current_price, target_price, offset_ticks, offset_cents, amount, status, reposition_threshold_cents)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            telegram_id, order_id, market_id, market_title, token_id, token_name,
            side, current_price, target_price, offset_ticks, offset_cents, amount, status, reposition_threshold_cents
        ))
        
        await conn.commit()
    logger.info(f"Ордер {order_id} сохранен в базу данных для пользователя {telegram_id}")


async def get_user_orders(telegram_id: int, status: Optional[str] = None) -> list:
    """
    Получает список ордеров пользователя из базы данных.
    
    Args:
        telegram_id: ID пользователя в Telegram
        status: Фильтр по статусу (active/cancelled/filled). Если None, возвращает все ордера.
    
    Returns:
        list: Список словарей с данными ордеров
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        if status:
            async with conn.execute("""
                SELECT * FROM orders 
                WHERE telegram_id = ? AND status = ?
                ORDER BY created_at DESC
            """, (telegram_id, status)) as cursor:
                rows = await cursor.fetchall()
        else:
            async with conn.execute("""
                SELECT * FROM orders 
                WHERE telegram_id = ?
                ORDER BY created_at DESC
            """, (telegram_id,)) as cursor:
                rows = await cursor.fetchall()
    
    # Получаем названия колонок
    columns = ['id', 'telegram_id', 'order_id', 'market_id', 'market_title', 
               'token_id', 'token_name', 'side', 'current_price', 'target_price',
               'offset_ticks', 'offset_cents', 'amount', 'status', 'created_at']
    
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
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("""
            SELECT * FROM orders 
            WHERE order_id = ?
        """, (order_id,)) as cursor:
            row = await cursor.fetchone()
    
    if not row:
        return None
    
    # Получаем названия колонок
    columns = ['id', 'telegram_id', 'order_id', 'market_id', 'market_title', 
               'token_id', 'token_name', 'side', 'current_price', 'target_price',
               'offset_ticks', 'offset_cents', 'amount', 'status', 'created_at']
    
    order_dict = dict(zip(columns, row))
    return order_dict


async def update_order_status(order_id: str, status: str):
    """
    Обновляет статус ордера в базе данных.
    
    Args:
        order_id: ID ордера на бирже
        status: Новый статус (active/cancelled/filled)
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            UPDATE orders 
            SET status = ?
            WHERE order_id = ?
        """, (status, order_id))
        
        await conn.commit()
    logger.info(f"Статус ордера {order_id} обновлен на {status}")


async def update_order_in_db(old_order_id: str, new_order_id: str, new_current_price: float, new_target_price: float):
    """
    Обновляет order_id и цену ордера в БД.
    
    Args:
        old_order_id: Старый ID ордера
        new_order_id: Новый ID ордера
        new_current_price: Новая текущая цена
        new_target_price: Новая целевая цена
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            UPDATE orders 
            SET order_id = ?, current_price = ?, target_price = ?
            WHERE order_id = ?
        """, (new_order_id, new_current_price, new_target_price, old_order_id))
        
        await conn.commit()
    logger.info(f"Обновлен ордер {old_order_id} -> {new_order_id} в БД")


async def get_all_users():
    """Получает список всех пользователей из БД."""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT telegram_id FROM users") as cursor:
            rows = await cursor.fetchall()
    return [row[0] for row in rows]


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
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for table_name in table_names:
                try:
                    csv_content = await export_table_to_csv(conn, table_name)
                    # Добавляем CSV файл в ZIP с именем таблицы
                    zip_file.writestr(f"{table_name}.csv", csv_content.encode('utf-8'))
                    logger.info(f"Экспортирована таблица {table_name}")
                except Exception as e:
                    logger.error(f"Ошибка при экспорте таблицы {table_name}: {e}")
                    # Добавляем файл с ошибкой
                    zip_file.writestr(f"{table_name}_error.txt", f"Error exporting table: {e}")
    
    zip_buffer.seek(0)
    return zip_buffer.read()
