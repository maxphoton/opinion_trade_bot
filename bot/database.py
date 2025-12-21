"""
Модуль для работы с базой данных SQLite.

Содержит функции для:
- Инициализации базы данных
- Работы с пользователями (сохранение, получение)
- Работы с ордерами (сохранение, получение)
- Экспорта данных
"""

import csv
import io
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from aes import encrypt, decrypt

# Настройка логирования
logger = logging.getLogger(__name__)

# Путь к базе данных SQLite (в той же папке, что и скрипт)
DB_PATH = Path(__file__).parent / "users.db"


def init_database():
    """Инициализирует базу данных SQLite."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Таблица пользователей
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
    
    # Таблица ордеров
    cursor.execute("""
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
        )
    """)
    
    # Создаем индекс для быстрого поиска ордеров по telegram_id
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_orders_telegram_id ON orders(telegram_id)
    """)
    
    # Создаем индекс для поиска по order_id
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id)
    """)
    
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")


def get_user(telegram_id: int) -> Optional[dict]:
    """
    Получает данные пользователя из базы данных.
    
    Args:
        telegram_id: ID пользователя в Telegram
    
    Returns:
        dict: Словарь с данными пользователя или None, если пользователь не найден
    """
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
    """
    Сохраняет данные пользователя в базу данных с шифрованием.
    
    Args:
        telegram_id: ID пользователя в Telegram
        username: Имя пользователя (опционально)
        wallet_address: Адрес кошелька
        private_key: Приватный ключ
        api_key: API ключ
    """
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


def save_order(telegram_id: int, order_id: str, market_id: int, market_title: Optional[str],
               token_id: str, token_name: str, side: str, current_price: float,
               target_price: float, offset_ticks: int, offset_cents: float, amount: float,
               status: str = 'active'):
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
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO orders 
        (telegram_id, order_id, market_id, market_title, token_id, token_name, 
         side, current_price, target_price, offset_ticks, offset_cents, amount, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        telegram_id, order_id, market_id, market_title, token_id, token_name,
        side, current_price, target_price, offset_ticks, offset_cents, amount, status
    ))
    
    conn.commit()
    conn.close()
    logger.info(f"Ордер {order_id} сохранен в базу данных для пользователя {telegram_id}")


def get_user_orders(telegram_id: int, status: Optional[str] = None) -> list:
    """
    Получает список ордеров пользователя из базы данных.
    
    Args:
        telegram_id: ID пользователя в Telegram
        status: Фильтр по статусу (active/cancelled/filled). Если None, возвращает все ордера.
    
    Returns:
        list: Список словарей с данными ордеров
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    if status:
        cursor.execute("""
            SELECT * FROM orders 
            WHERE telegram_id = ? AND status = ?
            ORDER BY created_at DESC
        """, (telegram_id, status))
    else:
        cursor.execute("""
            SELECT * FROM orders 
            WHERE telegram_id = ?
            ORDER BY created_at DESC
        """, (telegram_id,))
    
    rows = cursor.fetchall()
    conn.close()
    
    # Получаем названия колонок
    columns = ['id', 'telegram_id', 'order_id', 'market_id', 'market_title', 
               'token_id', 'token_name', 'side', 'current_price', 'target_price',
               'offset_ticks', 'offset_cents', 'amount', 'status', 'created_at']
    
    orders = []
    for row in rows:
        order_dict = dict(zip(columns, row))
        orders.append(order_dict)
    
    return orders


def get_order_by_id(order_id: str) -> Optional[dict]:
    """
    Получает ордер по его ID из базы данных.
    
    Args:
        order_id: ID ордера на бирже
    
    Returns:
        dict: Словарь с данными ордера или None, если ордер не найден
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT * FROM orders 
        WHERE order_id = ?
    """, (order_id,))
    
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return None
    
    # Получаем названия колонок
    columns = ['id', 'telegram_id', 'order_id', 'market_id', 'market_title', 
               'token_id', 'token_name', 'side', 'current_price', 'target_price',
               'offset_ticks', 'offset_cents', 'amount', 'status', 'created_at']
    
    order_dict = dict(zip(columns, row))
    return order_dict


def update_order_status(order_id: str, status: str):
    """
    Обновляет статус ордера в базе данных.
    
    Args:
        order_id: ID ордера на бирже
        status: Новый статус (active/cancelled/filled)
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        UPDATE orders 
        SET status = ?
        WHERE order_id = ?
    """, (status, order_id))
    
    conn.commit()
    conn.close()
    logger.info(f"Статус ордера {order_id} обновлен на {status}")


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
                # Конвертируем BLOB в hex строку
                csv_row.append(value.hex())
            else:
                csv_row.append(value)
        writer.writerow(csv_row)
    
    return output.getvalue()

