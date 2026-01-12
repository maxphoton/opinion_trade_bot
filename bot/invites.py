"""
Модуль для работы с инвайтами.
Содержит функции для создания, проверки и использования инвайтов.
"""

import logging
import secrets
import string
from typing import Dict, List

import aiosqlite
from database import DB_PATH

logger = logging.getLogger(__name__)


async def create_invites(count: int) -> List[str]:
    """
    Создает указанное количество уникальных инвайтов.

    Args:
        count: Количество инвайтов для создания

    Returns:
        Список созданных инвайт-кодов
    """
    created_invites = []
    chars = string.ascii_uppercase + string.ascii_lowercase + string.digits

    async with aiosqlite.connect(DB_PATH) as conn:
        for _ in range(count):
            # Генерируем уникальный инвайт
            while True:
                invite_code = "".join(secrets.choice(chars) for _ in range(10))

                # Проверяем уникальность
                async with conn.execute(
                    "SELECT id FROM invites WHERE invite = ?", (invite_code,)
                ) as cursor:
                    existing = await cursor.fetchone()
                    if not existing:
                        break

            # Вставляем инвайт
            await conn.execute(
                "INSERT INTO invites (invite) VALUES (?)", (invite_code,)
            )
            created_invites.append(invite_code)

        await conn.commit()

    logger.info(f"Создано {count} новых инвайтов")
    return created_invites


async def get_unused_invites(count: int) -> List[Dict]:
    """
    Получает указанное количество неиспользованных инвайтов.
    Если недостаточно, создает новые до нужного количества.

    Args:
        count: Количество инвайтов для получения

    Returns:
        Список словарей с данными инвайтов: [{'id': int, 'invite': str, 'created_at': str}, ...]
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        # Получаем неиспользованные инвайты
        async with conn.execute(
            "SELECT id, invite, created_at FROM invites WHERE telegram_id IS NULL ORDER BY created_at ASC LIMIT ?",
            (count,),
        ) as cursor:
            rows = await cursor.fetchall()

        unused_count = len(rows)

        # Если недостаточно, создаем новые
        if unused_count < count:
            needed = count - unused_count
            new_invites = await create_invites(needed)
            # Получаем созданные инвайты из БД
            placeholders = ",".join(["?"] * len(new_invites))
            async with conn.execute(
                f"SELECT id, invite, created_at FROM invites WHERE invite IN ({placeholders})",
                new_invites,
            ) as cursor:
                new_rows = await cursor.fetchall()
            rows.extend(new_rows)

        # Формируем результат
        result = []
        for row in rows:
            result.append({"id": row[0], "invite": row[1], "created_at": row[2]})

        return result


async def is_invite_valid(invite_code: str) -> bool:
    """
    Проверяет, валиден ли инвайт (существует и не использован).

    Args:
        invite_code: Код инвайта для проверки

    Returns:
        True если инвайт валиден, False в противном случае
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT id FROM invites WHERE invite = ? AND telegram_id IS NULL",
            (invite_code,),
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None


async def use_invite(invite_code: str, telegram_id: int) -> bool:
    """
    Использует инвайт (записывает telegram_id к инвайту).
    Выполняется атомарно в транзакции для защиты от race condition.

    Args:
        invite_code: Код инвайта
        telegram_id: ID пользователя Telegram

    Returns:
        True если инвайт успешно использован, False если инвайт невалиден или уже использован
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        # Используем транзакцию для атомарности
        await conn.execute("BEGIN TRANSACTION")
        try:
            # Проверяем и обновляем в одной транзакции
            async with conn.execute(
                "SELECT id FROM invites WHERE invite = ? AND telegram_id IS NULL",
                (invite_code,),
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                invite_id = row[0]
                # Обновляем инвайт
                await conn.execute(
                    "UPDATE invites SET telegram_id = ?, used_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (telegram_id, invite_id),
                )
                await conn.commit()
                logger.info(
                    f"Инвайт {invite_code} использован пользователем {telegram_id}"
                )
                return True
            else:
                await conn.rollback()
                return False
        except Exception as e:
            await conn.rollback()
            logger.error(f"Ошибка при использовании инвайта {invite_code}: {e}")
            return False


async def get_invites_statistics() -> Dict:
    """
    Получает статистику по инвайтам.

    Returns:
        Словарь со статистикой: {'total': int, 'used': int, 'unused': int}
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        # Общее количество
        async with conn.execute("SELECT COUNT(*) FROM invites") as cursor:
            total = (await cursor.fetchone())[0]

        # Использованных
        async with conn.execute(
            "SELECT COUNT(*) FROM invites WHERE telegram_id IS NOT NULL"
        ) as cursor:
            used = (await cursor.fetchone())[0]

        # Неиспользованных
        unused = total - used

        return {"total": total, "used": used, "unused": unused}
