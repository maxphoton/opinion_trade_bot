"""
WebSocket синхронизация ордеров для Opinion.trade.

Подписывается на канал market.last.trade для отслеживания изменений цен рынков
и автоматически переставляет ордера при изменении цены.

Использует канал market.last.trade вместо market.last.price для более надежного
отслеживания изменений цен, так как он отправляет сообщения при каждом матче трейда.
"""

import asyncio
import json
import logging
from typing import Dict, Optional, Set

import websockets
from aiogram import Bot
from opinion.sync_orders import async_sync_all_orders
from service.config import settings
from service.database import get_all_pending_orders_with_accounts, get_opinion_account

logger = logging.getLogger(__name__)

# Глобальный экземпляр WebSocket менеджера (устанавливается в main.py)
_websocket_sync_instance: Optional["WebSocketOrderSync"] = None


def get_websocket_sync() -> Optional["WebSocketOrderSync"]:
    """
    Возвращает глобальный экземпляр WebSocket менеджера.

    Returns:
        Экземпляр WebSocketOrderSync или None если не инициализирован
    """
    return _websocket_sync_instance


def set_websocket_sync(instance: "WebSocketOrderSync"):
    """
    Устанавливает глобальный экземпляр WebSocket менеджера.

    Args:
        instance: Экземпляр WebSocketOrderSync
    """
    global _websocket_sync_instance
    _websocket_sync_instance = instance


# WebSocket URL
WS_URL = "wss://ws.opinion.trade"

# Heartbeat interval (30 seconds as per documentation)
HEARTBEAT_INTERVAL = 30.0

# Debounce delay (2-3 seconds to group frequent updates)
DEBOUNCE_DELAY = 3


class WebSocketOrderSync:
    """Менеджер WebSocket синхронизации ордеров."""

    def __init__(self, bot: Bot):
        """
        Инициализирует WebSocket менеджер.

        Args:
            bot: Экземпляр aiogram Bot для отправки уведомлений
        """
        self.bot = bot
        self.ws_url = WS_URL
        self.admin_api_key: Optional[str] = None
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.subscriptions: Set[int] = (
            set()
        )  # market_id -> множество подписанных маркетов
        self.pending_updates: Dict[
            int, asyncio.Task
        ] = {}  # market_id -> задача дебаунса
        self.reconnect_delay = 1.0
        self.max_reconnect_delay = 60.0
        self.heartbeat_task: Optional[asyncio.Task] = None
        self.running = False
        self._lock = asyncio.Lock()

    async def _get_admin_api_key(self) -> Optional[str]:
        """
        Получает админский API ключ из настроек или первого аккаунта из БД.

        Returns:
            API ключ или None если не найден
        """
        # Сначала проверяем настройки
        if settings.websocket_api_key:
            return settings.websocket_api_key

        # Если не указан, берем первый аккаунт из БД
        orders_with_accounts = await get_all_pending_orders_with_accounts()
        if orders_with_accounts:
            account = orders_with_accounts[0]["account"]
            account_data = await get_opinion_account(account["account_id"])
            if account_data:
                return account_data.get("api_key")

        logger.warning("Не найден API ключ для WebSocket соединения")
        return None

    async def start(self):
        """Запускает WebSocket менеджер."""
        if self.running:
            logger.warning("WebSocket менеджер уже запущен")
            return

        self.running = True
        logger.info("Запуск WebSocket менеджера синхронизации ордеров")

        # Получаем API ключ
        self.admin_api_key = await self._get_admin_api_key()
        if not self.admin_api_key:
            logger.error(
                "Не удалось получить API ключ для WebSocket. Остановка менеджера."
            )
            self.running = False
            return

        # Загружаем активные подписки при старте
        await self._load_active_subscriptions()

        # Запускаем основной цикл
        asyncio.create_task(self._run())

    async def stop(self):
        """Останавливает WebSocket менеджер."""
        logger.info("Остановка WebSocket менеджера")
        self.running = False

        # Отменяем heartbeat
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            try:
                await self.heartbeat_task
            except asyncio.CancelledError:
                pass

        # Отменяем все pending updates
        for task in self.pending_updates.values():
            task.cancel()
        self.pending_updates.clear()

        # Закрываем соединение
        if self.ws:
            await self.ws.close()

    async def subscribe_to_market(
        self, market_id: int, root_market_id: Optional[int] = None
    ):
        """
        Подписывается на маркет (если еще не подписаны).

        Args:
            market_id: ID маркета (submarket для categorical, обычный market для binary)
            root_market_id: ID корневого маркета (для categorical markets), None для binary
        """
        # Определяем значение для подписки: root_market_id для categorical, market_id для binary
        subscription_key = root_market_id if root_market_id is not None else market_id

        async with self._lock:
            if subscription_key in self.subscriptions:
                logger.debug(
                    f"Уже подписаны на маркет {subscription_key} (market_id: {market_id}, root: {root_market_id})"
                )
                return

            self.subscriptions.add(subscription_key)

        # Отправляем подписку, если соединение активно
        if self.ws:
            await self._send_subscribe(market_id, root_market_id)
        else:
            logger.info(
                f"Подписка на маркет {subscription_key} будет отправлена при подключении"
            )

    async def unsubscribe_from_market(
        self, market_id: int, root_market_id: Optional[int] = None
    ):
        """
        Отписывается от маркета.

        Args:
            market_id: ID маркета (submarket для categorical, обычный market для binary)
            root_market_id: ID корневого маркета (для categorical markets), None для binary
        """
        # Определяем значение для отписки: root_market_id для categorical, market_id для binary
        subscription_key = root_market_id if root_market_id is not None else market_id

        async with self._lock:
            if subscription_key not in self.subscriptions:
                return
            self.subscriptions.discard(subscription_key)

        # Отправляем отписку, если соединение активно
        if self.ws:
            await self._send_unsubscribe(market_id, root_market_id)

    async def _load_active_subscriptions(self):
        """Загружает все активные подписки из БД при старте."""
        orders_with_accounts = await get_all_pending_orders_with_accounts()
        subscription_keys = set()

        for item in orders_with_accounts:
            order = item["order"]
            market_id = order.get("market_id")
            root_market_id = order.get("root_market_id")
            # Для categorical markets используем root_market_id, для binary - market_id
            subscription_key = (
                root_market_id if root_market_id is not None else market_id
            )
            if subscription_key:
                subscription_keys.add(subscription_key)

        async with self._lock:
            self.subscriptions.update(subscription_keys)

        logger.info(
            f"Загружено {len(subscription_keys)} активных маркетов для подписки"
        )

    async def _run(self):
        """Основной цикл WebSocket соединения с переподключением."""
        while self.running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"WebSocket соединение закрыто: {e}")
                if self.running:
                    logger.info(
                        f"Переподключение через {self.reconnect_delay:.1f} секунд..."
                    )
                    await asyncio.sleep(self.reconnect_delay)
                    self.reconnect_delay = min(
                        self.reconnect_delay * 2, self.max_reconnect_delay
                    )
            except Exception as e:
                logger.error(f"Ошибка в WebSocket соединении: {e}", exc_info=True)
                if self.running:
                    logger.info(
                        f"Переподключение через {self.reconnect_delay:.1f} секунд..."
                    )
                    await asyncio.sleep(self.reconnect_delay)
                    # Экспоненциальная задержка с максимумом
                    self.reconnect_delay = min(
                        self.reconnect_delay * 2, self.max_reconnect_delay
                    )

    async def _connect_and_listen(self):
        """Подключается к WebSocket и слушает сообщения."""
        url = f"{self.ws_url}?apikey={self.admin_api_key}"
        logger.info(
            f"Подключение к WebSocket: {url.replace(self.admin_api_key, '***')}"
        )

        try:
            async with websockets.connect(
                url, ping_interval=None, ping_timeout=None
            ) as ws:
                self.ws = ws
                self.reconnect_delay = (
                    1.0  # Сбрасываем задержку при успешном подключении
                )
                logger.info("WebSocket соединение установлено")

                # Запускаем heartbeat
                self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                # Подписываемся на все активные маркеты
                await self._resubscribe_all()

                # Слушаем сообщения
                async for message in ws:
                    if not self.running:
                        break
                    try:
                        await self._handle_message(message)
                    except Exception as e:
                        logger.error(
                            f"Ошибка при обработке сообщения: {e}", exc_info=True
                        )
        except websockets.exceptions.InvalidStatusCode as e:
            logger.error(f"Ошибка подключения к WebSocket (неверный статус): {e}")
            raise
        except websockets.exceptions.ConnectionClosedError as e:
            logger.warning(f"WebSocket соединение закрыто с ошибкой: {e}")
            raise
        except Exception as e:
            logger.error(f"Неожиданная ошибка WebSocket соединения: {e}", exc_info=True)
            raise
        finally:
            # Очищаем соединение и heartbeat при разрыве
            self.ws = None
            if self.heartbeat_task:
                self.heartbeat_task.cancel()
                try:
                    await self.heartbeat_task
                except asyncio.CancelledError:
                    pass
                self.heartbeat_task = None

    async def _resubscribe_all(self):
        """Переподписывается на все активные маркеты."""
        # Загружаем все pending ордера для получения market_id и root_market_id
        orders_with_accounts = await get_all_pending_orders_with_accounts()
        subscriptions_map = {}  # subscription_key -> (market_id, root_market_id)

        for item in orders_with_accounts:
            order = item["order"]
            market_id = order.get("market_id")
            root_market_id = order.get("root_market_id")
            subscription_key = (
                root_market_id if root_market_id is not None else market_id
            )
            if subscription_key:
                subscriptions_map[subscription_key] = (market_id, root_market_id)

        logger.info(f"Переподписка на {len(subscriptions_map)} уникальных маркетов")

        # Обновляем subscriptions с новыми ключами
        async with self._lock:
            self.subscriptions.update(subscriptions_map.keys())

        for subscription_key, (market_id, root_market_id) in subscriptions_map.items():
            try:
                await self._send_subscribe(market_id, root_market_id)
                await asyncio.sleep(0.1)  # Небольшая задержка между подписками
            except Exception as e:
                logger.error(f"Ошибка при подписке на маркет {subscription_key}: {e}")

    async def _send_subscribe(
        self, market_id: int, root_market_id: Optional[int] = None
    ):
        """
        Отправляет сообщение подписки на маркет.

        Args:
            market_id: ID маркета (submarket для categorical, обычный market для binary)
            root_market_id: ID корневого маркета (для categorical markets), None для binary
        """
        if not self.ws:
            return

        try:
            # Для categorical markets используем rootMarketId, для binary - marketId
            if root_market_id is not None:
                message = {
                    "action": "SUBSCRIBE",
                    "channel": "market.last.trade",
                    "rootMarketId": root_market_id,
                }
                logger.debug(
                    f"Подписка на categorical маркет rootMarketId={root_market_id} отправлена"
                )
            else:
                message = {
                    "action": "SUBSCRIBE",
                    "channel": "market.last.trade",
                    "marketId": market_id,
                }
                logger.debug(
                    f"Подписка на binary маркет marketId={market_id} отправлена"
                )

            await self.ws.send(json.dumps(message))
        except (
            websockets.exceptions.ConnectionClosed,
            websockets.exceptions.ConnectionClosedOK,
        ):
            logger.warning(
                f"Соединение закрыто при отправке подписки на маркет {market_id}"
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке подписки на маркет {market_id}: {e}")

    async def _send_unsubscribe(
        self, market_id: int, root_market_id: Optional[int] = None
    ):
        """
        Отправляет сообщение отписки от маркета.

        Args:
            market_id: ID маркета (submarket для categorical, обычный market для binary)
            root_market_id: ID корневого маркета (для categorical markets), None для binary
        """
        if not self.ws:
            return

        try:
            # Для categorical markets используем rootMarketId, для binary - marketId
            if root_market_id is not None:
                message = {
                    "action": "UNSUBSCRIBE",
                    "channel": "market.last.trade",
                    "rootMarketId": root_market_id,
                }
            else:
                message = {
                    "action": "UNSUBSCRIBE",
                    "channel": "market.last.trade",
                    "marketId": market_id,
                }
            await self.ws.send(json.dumps(message))
            logger.debug(f"Отписка от маркета {market_id} отправлена")
        except (
            websockets.exceptions.ConnectionClosed,
            websockets.exceptions.ConnectionClosedOK,
        ):
            logger.warning(
                f"Соединение закрыто при отправке отписки от маркета {market_id}"
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке отписки от маркета {market_id}: {e}")

    async def _heartbeat_loop(self):
        """Отправляет heartbeat каждые 30 секунд."""
        while self.running:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self.ws:
                    try:
                        await self.ws.send(json.dumps({"action": "HEARTBEAT"}))
                        logger.debug("Heartbeat отправлен")
                    except (
                        websockets.exceptions.ConnectionClosed,
                        websockets.exceptions.ConnectionClosedOK,
                    ):
                        # Соединение закрыто - это нормально
                        logger.debug("Heartbeat не отправлен: соединение закрыто")
                        break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ошибка при отправке heartbeat: {e}")

    async def _handle_message(self, message: str):
        """Обрабатывает входящее сообщение от WebSocket."""
        try:
            data = json.loads(message)
            msg_type = data.get("msgType")

            if msg_type == "market.last.trade":
                await self._handle_price_update(data)
            else:
                logger.debug(f"Получено сообщение типа {msg_type}")

        except json.JSONDecodeError as e:
            logger.error(f"Ошибка парсинга JSON сообщения: {e}")
        except Exception as e:
            logger.error(f"Ошибка при обработке сообщения: {e}")

    async def _handle_price_update(self, message: Dict):
        """
        Обрабатывает обновление цены из канала market.last.trade с дебаунсом.

        Args:
            message: Сообщение с обновлением цены (содержит side, shares, amount и другие поля)
        """
        market_id = message.get("marketId")
        if not market_id:
            logger.warning("Получено обновление цены без marketId")
            return

        # Отменяем предыдущую задачу дебаунса для этого маркета
        if market_id in self.pending_updates:
            task = self.pending_updates[market_id]
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Создаем новую задачу с задержкой
        task = asyncio.create_task(
            self._process_market_update_debounced(market_id, message)
        )
        self.pending_updates[market_id] = task

    async def _process_market_update_debounced(self, market_id: int, message: Dict):
        """
        Обрабатывает обновление маркета с дебаунсом.

        Args:
            market_id: ID маркета
            message: Сообщение с обновлением цены
        """
        # Ждем дебаунс задержку
        await asyncio.sleep(DEBOUNCE_DELAY)

        # Проверяем, что задача не была отменена
        if market_id not in self.pending_updates:
            return

        # Удаляем задачу из pending
        self.pending_updates.pop(market_id, None)

        # Обрабатываем обновление
        await self._process_market_update(market_id, message)

    async def _process_market_update(self, market_id: int, message: Dict):
        """
        Обрабатывает обновление маркета: загружает ордера и проверяет необходимость перестановки.

        Args:
            market_id: ID маркета
            message: Сообщение с обновлением цены из market.last.trade
                    (содержит tokenId, outcomeSide, price, side, shares, amount)
        """
        logger.info(f"Обработка обновления для маркета {market_id}")

        # Используем async_sync_all_orders для синхронизации ордеров по этому маркету
        # Это использует батчинг и всю логику обработки из sync_orders.py
        await async_sync_all_orders(self.bot, market_id=market_id)
