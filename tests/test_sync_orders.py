"""
Тесты для bot/opinion/sync_orders.py

Покрывает различные кейсы:
- Когда изменение цены достаточно для перестановки ордера
- Когда изменение недостаточно
- Проверка уведомлений о смещении цены
- Проверка правильности списков для отмены/размещения
- Уведомления об ошибках отмены ордеров (send_cancellation_error_notification)
- Уведомления об ошибках размещения ордеров (send_order_placement_error_notification)
- Проверка статуса ордера через API (finished/canceled)
- Уведомления об исполнении ордеров (send_order_filled_notification)
- Группировка ордеров по аккаунтам в async_sync_all_orders
- Обработка аккаунтов с failed proxy
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opinion.opinion_api_wrapper import ORDER_STATUS_CANCELED, ORDER_STATUS_FINISHED

# Импортируем функции для тестирования
# conftest.py настроит sys.path для работы с относительными импортами
from opinion.sync_orders import (
    async_sync_all_orders,
    calculate_new_target_price,
    process_account_orders,
    send_cancellation_error_notification,
    send_order_filled_notification,
    send_order_placement_error_notification,
)
from service.config import TICK_SIZE

# Мокируем OrderSide для тестов
# Создаем MagicMock объекты, которые будут использоваться для сравнения
MockOrderSide = MagicMock()
MockOrderSide.BUY = MagicMock()
MockOrderSide.SELL = MagicMock()


class TestCalculateNewTargetPrice:
    """Тесты для функции calculate_new_target_price"""

    @pytest.mark.parametrize(
        "current_price,offset_ticks,side,expected",
        [
            (0.5, 10, "BUY", 0.5 - 10 * TICK_SIZE),
            (0.5, 10, "SELL", 0.5 + 10 * TICK_SIZE),
            (0.3, 5, "BUY", 0.3 - 5 * TICK_SIZE),
            (0.7, 15, "SELL", 0.7 + 15 * TICK_SIZE),
        ],
    )
    def test_calculate_target_price(self, current_price, offset_ticks, side, expected):
        """Тест расчета целевой цены для разных параметров"""
        result = calculate_new_target_price(current_price, side, offset_ticks)
        assert result == pytest.approx(expected, abs=0.0001)

    @pytest.mark.parametrize(
        "current_price,offset_ticks,side",
        [
            (0.01, 100, "BUY"),  # Большой отступ для BUY
            (0.001, 1, "BUY"),  # Граничный случай
            (0.0, 1, "BUY"),  # Минимальная цена
        ],
    )
    def test_price_limits_min(self, current_price, offset_ticks, side):
        """Тест ограничения минимальной цены (0.001)"""
        result = calculate_new_target_price(current_price, side, offset_ticks)
        assert result >= 0.001

    @pytest.mark.parametrize(
        "current_price,offset_ticks,side",
        [
            (0.99, 100, "SELL"),  # Большой отступ для SELL
            (0.999, 1, "SELL"),  # Граничный случай
            (1.0, 1, "SELL"),  # Максимальная цена
        ],
    )
    def test_price_limits_max(self, current_price, offset_ticks, side):
        """Тест ограничения максимальной цены (0.999)"""
        result = calculate_new_target_price(current_price, side, offset_ticks)
        assert result <= 0.999


class TestProcessAccountOrders:
    """Тесты для функции process_account_orders"""

    @pytest.fixture
    def mock_account_data(self):
        """Мок данных аккаунта"""
        return {
            "account_id": 1,
            "telegram_id": 12345,
            "wallet_address": "0x123",
            "private_key": "key",
            "api_key": "api_key",
            "proxy_str": "proxy",
            "proxy_status": "active",
        }

    @pytest.fixture
    def mock_client(self):
        """Мок клиента Opinion SDK"""
        client = MagicMock()
        return client

    @pytest.mark.asyncio
    async def test_no_orders(self, mock_account_data, mock_client):
        """Тест: у аккаунта нет активных ордеров"""
        with (
            patch(
                "opinion.sync_orders.get_account_orders", new_callable=AsyncMock
            ) as mock_get_orders,
            patch("opinion.sync_orders.create_client") as mock_create_client,
        ):
            mock_get_orders.return_value = []
            mock_create_client.return_value = mock_client

            (
                orders_to_cancel,
                orders_to_place,
                notifications,
            ) = await process_account_orders(1, mock_account_data)

            assert orders_to_cancel == []
            assert orders_to_place == []
            assert notifications == []

    @pytest.mark.asyncio
    async def test_reposition_sufficient_change(self, mock_account_data, mock_client):
        """Тест: изменение достаточно для перестановки ордера"""
        db_order = {
            "order_id": "order_123",
            "market_id": 100,
            "token_id": "token_yes",
            "token_name": "YES",
            "side": "BUY",
            "current_price": 0.500,
            "target_price": 0.490,
            "offset_ticks": 10,
            "amount": 100.0,
            "reposition_threshold_cents": 0.5,
            "status": "pending",
        }

        with (
            patch(
                "opinion.sync_orders.get_account_orders", new_callable=AsyncMock
            ) as mock_get_orders,
            patch("opinion.sync_orders.create_client") as mock_create_client,
            patch("opinion.sync_orders.get_current_market_price") as mock_get_price,
            patch(
                "opinion.sync_orders.get_order_by_id", new_callable=AsyncMock
            ) as mock_get_order_by_id,
        ):
            mock_get_orders.return_value = [db_order]
            mock_create_client.return_value = mock_client
            # Новая цена достаточно изменилась, чтобы целевая цена изменилась >= threshold
            mock_get_price.return_value = 0.510
            mock_get_order_by_id.return_value = None

            (
                orders_to_cancel,
                orders_to_place,
                notifications,
            ) = await process_account_orders(1, mock_account_data)

            # Проверяем поведение: ордер должен быть добавлен в списки для перестановки
            assert len(orders_to_cancel) == 1
            assert orders_to_cancel[0] == "order_123"
            assert len(orders_to_place) == 1
            assert len(orders_to_cancel) == len(
                orders_to_place
            )  # Списки должны быть согласованы

            # Проверяем, что новый ордер связан со старым
            new_order = orders_to_place[0]
            assert new_order["old_order_id"] == "order_123"
            # Проверяем, что новая цена рассчитана корректно (BUY: current - offset)
            expected_new_price = calculate_new_target_price(0.510, "BUY", 10)
            assert new_order["price"] == pytest.approx(expected_new_price, abs=0.0001)

            # Проверяем, что уведомление создано только когда ордер будет переставлен
            assert len(notifications) == 1
            notification = notifications[0]
            assert notification["order_id"] == "order_123"
            assert notification["will_reposition"] is True
            # Проверяем бизнес-логику: изменение >= порога
            assert (
                notification["target_price_change_cents"]
                >= db_order["reposition_threshold_cents"]
            )

    @pytest.mark.asyncio
    async def test_reposition_insufficient_change(self, mock_account_data, mock_client):
        """Тест: изменение недостаточно для перестановки ордера"""
        db_order = {
            "order_id": "order_456",
            "market_id": 100,
            "token_id": "token_no",
            "token_name": "NO",
            "side": "SELL",
            "current_price": 0.500,
            "target_price": 0.510,
            "offset_ticks": 10,
            "amount": 100.0,
            "reposition_threshold_cents": 0.5,
            "status": "pending",
        }

        with (
            patch(
                "opinion.sync_orders.get_account_orders", new_callable=AsyncMock
            ) as mock_get_orders,
            patch("opinion.sync_orders.create_client") as mock_create_client,
            patch("opinion.sync_orders.get_current_market_price") as mock_get_price,
            patch(
                "opinion.sync_orders.get_order_by_id", new_callable=AsyncMock
            ) as mock_get_order_by_id,
        ):
            mock_get_orders.return_value = [db_order]
            mock_create_client.return_value = mock_client
            # Новая цена изменилась недостаточно, чтобы целевая цена изменилась >= threshold
            mock_get_price.return_value = 0.503
            mock_get_order_by_id.return_value = None

            (
                orders_to_cancel,
                orders_to_place,
                notifications,
            ) = await process_account_orders(1, mock_account_data)

            # Проверяем поведение: ордер НЕ должен быть добавлен в списки
            assert len(orders_to_cancel) == 0
            assert len(orders_to_place) == 0
            # Уведомление не создается, когда изменение недостаточно
            assert len(notifications) == 0

    @pytest.mark.asyncio
    async def test_order_status_finished(self, mock_account_data, mock_client):
        """Тест: ордер стал finished, обновляется БД и отправляется уведомление"""
        db_order = {
            "order_id": "order_finished",
            "market_id": 100,
            "token_id": "token_yes",
            "token_name": "YES",
            "side": "BUY",
            "current_price": 0.500,
            "target_price": 0.490,
            "offset_ticks": 10,
            "amount": 100.0,
            "reposition_threshold_cents": 0.5,
            "status": "pending",
        }

        # Мокируем API ордер со статусом finished
        api_order = MagicMock()
        api_order.status = ORDER_STATUS_FINISHED
        api_order.order_id = "order_finished"
        api_order.market_id = 100
        api_order.market_title = "Test Market"
        api_order.root_market_id = 200
        api_order.root_market_title = "Root Market"
        api_order.price = "0.490"
        api_order.side_enum = "Buy"
        api_order.outcome = "YES"
        api_order.filled_amount = "100.0"
        api_order.order_amount = "100.0"

        with (
            patch(
                "opinion.sync_orders.get_account_orders", new_callable=AsyncMock
            ) as mock_get_orders,
            patch("opinion.sync_orders.create_client") as mock_create_client,
            patch(
                "opinion.sync_orders.get_order_by_id", new_callable=AsyncMock
            ) as mock_get_order_by_id,
            patch(
                "opinion.sync_orders.update_order_status", new_callable=AsyncMock
            ) as mock_update_status,
            patch(
                "opinion.sync_orders.send_order_filled_notification",
                new_callable=AsyncMock,
            ) as mock_send_notification,
        ):
            mock_get_orders.return_value = [db_order]
            mock_create_client.return_value = mock_client
            mock_get_order_by_id.return_value = api_order

            mock_bot = AsyncMock()
            (
                orders_to_cancel,
                orders_to_place,
                notifications,
            ) = await process_account_orders(1, mock_account_data, bot=mock_bot)

            # Ордер не должен быть добавлен в списки для отмены/размещения
            assert len(orders_to_cancel) == 0
            assert len(orders_to_place) == 0
            assert len(notifications) == 0

            # Проверяем, что статус обновлен в БД
            mock_update_status.assert_called_once_with("order_finished", "finished")

            # Проверяем, что отправлено уведомление об исполнении
            mock_send_notification.assert_called_once_with(mock_bot, 12345, api_order)

    @pytest.mark.asyncio
    async def test_order_status_canceled(self, mock_account_data, mock_client):
        """Тест: ордер стал canceled, обновляется БД без уведомления"""
        db_order = {
            "order_id": "order_canceled",
            "market_id": 100,
            "token_id": "token_yes",
            "token_name": "YES",
            "side": "BUY",
            "current_price": 0.500,
            "target_price": 0.490,
            "offset_ticks": 10,
            "amount": 100.0,
            "reposition_threshold_cents": 0.5,
            "status": "pending",
        }

        # Мокируем API ордер со статусом canceled
        api_order = MagicMock()
        api_order.status = ORDER_STATUS_CANCELED

        with (
            patch(
                "opinion.sync_orders.get_account_orders", new_callable=AsyncMock
            ) as mock_get_orders,
            patch("opinion.sync_orders.create_client") as mock_create_client,
            patch(
                "opinion.sync_orders.get_order_by_id", new_callable=AsyncMock
            ) as mock_get_order_by_id,
            patch(
                "opinion.sync_orders.update_order_status", new_callable=AsyncMock
            ) as mock_update_status,
            patch(
                "opinion.sync_orders.send_order_filled_notification",
                new_callable=AsyncMock,
            ) as mock_send_notification,
        ):
            mock_get_orders.return_value = [db_order]
            mock_create_client.return_value = mock_client
            mock_get_order_by_id.return_value = api_order

            mock_bot = AsyncMock()
            (
                orders_to_cancel,
                orders_to_place,
                notifications,
            ) = await process_account_orders(1, mock_account_data, bot=mock_bot)

            # Ордер не должен быть добавлен в списки
            assert len(orders_to_cancel) == 0
            assert len(orders_to_place) == 0
            assert len(notifications) == 0

            # Проверяем, что статус обновлен в БД
            mock_update_status.assert_called_once_with("order_canceled", "canceled")

            # Проверяем, что уведомление НЕ отправлено (для canceled не отправляется)
            mock_send_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_order_status_check_timeout(self, mock_account_data, mock_client):
        """Тест: таймаут при проверке статуса, продолжаем обработку"""
        db_order = {
            "order_id": "order_timeout",
            "market_id": 100,
            "token_id": "token_yes",
            "token_name": "YES",
            "side": "BUY",
            "current_price": 0.500,
            "target_price": 0.490,
            "offset_ticks": 10,
            "amount": 100.0,
            "reposition_threshold_cents": 0.5,
            "status": "pending",
        }

        with (
            patch(
                "opinion.sync_orders.get_account_orders", new_callable=AsyncMock
            ) as mock_get_orders,
            patch("opinion.sync_orders.create_client") as mock_create_client,
            patch("opinion.sync_orders.get_current_market_price") as mock_get_price,
            patch(
                "opinion.sync_orders.get_order_by_id", new_callable=AsyncMock
            ) as mock_get_order_by_id,
        ):
            mock_get_orders.return_value = [db_order]
            mock_create_client.return_value = mock_client
            mock_get_price.return_value = 0.510
            # Мокируем таймаут (возвращает None, но это обрабатывается)
            mock_get_order_by_id.side_effect = Exception("504 Gateway Time-out")

            # Обработка должна продолжиться несмотря на таймаут
            (
                orders_to_cancel,
                orders_to_place,
                notifications,
            ) = await process_account_orders(1, mock_account_data)

            # Ордер должен быть обработан (graceful degradation)
            assert len(orders_to_cancel) == 1
            assert len(orders_to_place) == 1

    @pytest.mark.asyncio
    async def test_no_price_change(self, mock_account_data, mock_client):
        """Тест: цена не изменилась - ордер не должен переставляться"""
        db_order = {
            "order_id": "order_789",
            "market_id": 100,
            "token_id": "token_yes",
            "token_name": "YES",
            "side": "BUY",
            "current_price": 0.500,
            "target_price": 0.490,
            "offset_ticks": 10,
            "amount": 100.0,
            "reposition_threshold_cents": 0.5,
            "status": "pending",
        }

        with (
            patch(
                "opinion.sync_orders.get_account_orders", new_callable=AsyncMock
            ) as mock_get_orders,
            patch("opinion.sync_orders.create_client") as mock_create_client,
            patch("opinion.sync_orders.get_current_market_price") as mock_get_price,
            patch(
                "opinion.sync_orders.get_order_by_id", new_callable=AsyncMock
            ) as mock_get_order_by_id,
        ):
            mock_get_orders.return_value = [db_order]
            mock_create_client.return_value = mock_client
            mock_get_price.return_value = 0.500  # Цена не изменилась
            mock_get_order_by_id.return_value = None

            (
                orders_to_cancel,
                orders_to_place,
                notifications,
            ) = await process_account_orders(1, mock_account_data)

            # Проверяем поведение: при отсутствии изменения цены ордер не переставляется
            assert len(orders_to_cancel) == 0
            assert len(orders_to_place) == 0
            assert len(notifications) == 0

    @pytest.mark.asyncio
    async def test_multiple_orders_mixed(self, mock_account_data, mock_client):
        """Тест: несколько ордеров, часть переставляется, часть нет"""
        db_orders = [
            {
                "order_id": "order_1",
                "market_id": 100,
                "token_id": "token_yes",
                "token_name": "YES",
                "side": "BUY",
                "current_price": 0.500,
                "target_price": 0.490,
                "offset_ticks": 10,
                "amount": 100.0,
                "reposition_threshold_cents": 0.5,
                "status": "pending",
            },
            {
                "order_id": "order_2",
                "market_id": 100,
                "token_id": "token_no",
                "token_name": "NO",
                "side": "SELL",
                "current_price": 0.500,
                "target_price": 0.510,
                "offset_ticks": 10,
                "amount": 100.0,
                "reposition_threshold_cents": 0.5,
                "status": "pending",
            },
        ]

        with (
            patch(
                "opinion.sync_orders.get_account_orders", new_callable=AsyncMock
            ) as mock_get_orders,
            patch("opinion.sync_orders.create_client") as mock_create_client,
            patch("opinion.sync_orders.get_current_market_price") as mock_get_price,
            patch(
                "opinion.sync_orders.get_order_by_id", new_callable=AsyncMock
            ) as mock_get_order_by_id,
        ):
            mock_get_orders.return_value = db_orders
            mock_create_client.return_value = mock_client
            mock_get_order_by_id.return_value = None

            # Первый ордер: изменение достаточно для перестановки
            # Второй ордер: изменение недостаточно
            def get_price_side_effect(client, token_id, side):
                if token_id == "token_yes" and side == "BUY":
                    return 0.510  # Достаточное изменение
                elif token_id == "token_no" and side == "SELL":
                    return 0.503  # Недостаточное изменение
                return None

            mock_get_price.side_effect = get_price_side_effect

            (
                orders_to_cancel,
                orders_to_place,
                notifications,
            ) = await process_account_orders(1, mock_account_data)

            # Проверяем поведение: только ордер с достаточным изменением переставляется
            assert len(orders_to_cancel) == 1
            assert orders_to_cancel[0] == "order_1"
            assert len(orders_to_place) == 1
            assert len(orders_to_cancel) == len(orders_to_place)

            # Уведомление создается только для ордера, который будет переставлен
            assert len(notifications) == 1
            assert notifications[0]["order_id"] == "order_1"
            assert notifications[0]["will_reposition"] is True

    @pytest.mark.asyncio
    async def test_notification_only_when_repositioning(
        self, mock_account_data, mock_client
    ):
        """Тест: уведомление создается только когда ордер будет переставлен"""
        db_order = {
            "order_id": "order_notify",
            "market_id": 100,
            "token_id": "token_yes",
            "token_name": "YES",
            "side": "BUY",
            "current_price": 0.500,
            "target_price": 0.490,
            "offset_ticks": 10,
            "amount": 100.0,
            "reposition_threshold_cents": 1.0,  # Высокий порог
            "status": "pending",
        }

        with (
            patch(
                "opinion.sync_orders.get_account_orders", new_callable=AsyncMock
            ) as mock_get_orders,
            patch("opinion.sync_orders.create_client") as mock_create_client,
            patch("opinion.sync_orders.get_current_market_price") as mock_get_price,
            patch(
                "opinion.sync_orders.get_order_by_id", new_callable=AsyncMock
            ) as mock_get_order_by_id,
        ):
            mock_get_orders.return_value = [db_order]
            mock_create_client.return_value = mock_client
            mock_get_price.return_value = (
                0.501  # Небольшое изменение, недостаточное для порога
            )
            mock_get_order_by_id.return_value = None

            (
                orders_to_cancel,
                orders_to_place,
                notifications,
            ) = await process_account_orders(1, mock_account_data)

            # Проверяем поведение: при недостаточном изменении ордер не переставляется
            assert len(orders_to_cancel) == 0
            assert len(orders_to_place) == 0
            # Уведомление не создается, когда изменение недостаточно
            assert len(notifications) == 0

    @pytest.mark.asyncio
    async def test_notification_contains_key_data(self, mock_account_data, mock_client):
        """Тест: уведомление содержит ключевые данные для пользователя"""
        db_order = {
            "order_id": "order_struct",
            "market_id": 200,
            "token_id": "token_test",
            "token_name": "YES",
            "side": "BUY",
            "current_price": 0.500,
            "target_price": 0.490,
            "offset_ticks": 10,
            "amount": 100.0,
            "reposition_threshold_cents": 0.5,
            "status": "pending",
        }

        with (
            patch(
                "opinion.sync_orders.get_account_orders", new_callable=AsyncMock
            ) as mock_get_orders,
            patch("opinion.sync_orders.create_client") as mock_create_client,
            patch("opinion.sync_orders.get_current_market_price") as mock_get_price,
            patch(
                "opinion.sync_orders.get_order_by_id", new_callable=AsyncMock
            ) as mock_get_order_by_id,
        ):
            mock_get_orders.return_value = [db_order]
            mock_create_client.return_value = mock_client
            mock_get_price.return_value = 0.510
            mock_get_order_by_id.return_value = None

            _, _, notifications = await process_account_orders(1, mock_account_data)

            # Проверяем, что уведомление создано только когда ордер будет переставлен
            assert len(notifications) == 1
            notification = notifications[0]

            # Проверяем ключевые бизнес-данные, а не структуру
            assert notification["order_id"] == "order_struct"
            assert notification["will_reposition"] is True
            # Проверяем, что изменение целевой цены достаточно для перестановки
            assert (
                notification["target_price_change_cents"]
                >= notification["reposition_threshold_cents"]
            )
            # Проверяем, что цены изменились
            assert (
                notification["old_current_price"] != notification["new_current_price"]
            )
            assert notification["old_target_price"] != notification["new_target_price"]

    @pytest.mark.asyncio
    async def test_client_creation_error(self, mock_account_data):
        """Тест: ошибка создания клиента"""
        with (
            patch(
                "opinion.sync_orders.get_account_orders", new_callable=AsyncMock
            ) as mock_get_orders,
            patch("opinion.sync_orders.create_client") as mock_create_client,
        ):
            mock_get_orders.return_value = [
                {
                    "order_id": "order_123",
                    "market_id": 100,
                    "token_id": "token_yes",
                    "token_name": "YES",
                    "side": "BUY",
                    "current_price": 0.500,
                    "target_price": 0.490,
                    "offset_ticks": 10,
                    "amount": 100.0,
                    "reposition_threshold_cents": 0.5,
                    "status": "pending",
                }
            ]
            mock_create_client.side_effect = Exception("Client creation failed")

            (
                orders_to_cancel,
                orders_to_place,
                notifications,
            ) = await process_account_orders(1, mock_account_data)

            # При ошибке создания клиента должны вернуться пустые списки
            assert orders_to_cancel == []
            assert orders_to_place == []
            assert notifications == []

    @pytest.mark.asyncio
    async def test_filter_by_market_id(self, mock_account_data, mock_client):
        """Тест: фильтрация ордеров по market_id"""
        db_orders = [
            {
                "order_id": "order_1",
                "market_id": 100,
                "token_id": "token_yes",
                "token_name": "YES",
                "side": "BUY",
                "current_price": 0.500,
                "target_price": 0.490,
                "offset_ticks": 10,
                "amount": 100.0,
                "reposition_threshold_cents": 0.5,
                "status": "pending",
            },
            {
                "order_id": "order_2",
                "market_id": 200,
                "token_id": "token_no",
                "token_name": "NO",
                "side": "SELL",
                "current_price": 0.500,
                "target_price": 0.510,
                "offset_ticks": 10,
                "amount": 100.0,
                "reposition_threshold_cents": 0.5,
                "status": "pending",
            },
        ]

        with (
            patch(
                "opinion.sync_orders.get_account_orders", new_callable=AsyncMock
            ) as mock_get_orders,
            patch("opinion.sync_orders.create_client") as mock_create_client,
            patch("opinion.sync_orders.get_current_market_price") as mock_get_price,
            patch(
                "opinion.sync_orders.get_order_by_id", new_callable=AsyncMock
            ) as mock_get_order_by_id,
        ):
            # Когда market_id=100, должны вернуться только ордера для market_id=100
            mock_get_orders.return_value = [db_orders[0]]  # Только первый ордер
            mock_create_client.return_value = mock_client
            mock_get_price.return_value = 0.510
            mock_get_order_by_id.return_value = None

            (
                orders_to_cancel,
                orders_to_place,
                notifications,
            ) = await process_account_orders(1, mock_account_data, market_id=100)

            # Проверяем, что get_account_orders вызван с правильным market_id
            mock_get_orders.assert_called_once_with(1, status="pending", market_id=100)

            # Должен быть обработан только ордер для market_id=100
            assert len(orders_to_cancel) == 1
            assert orders_to_cancel[0] == "order_1"
            assert len(orders_to_place) == 1

    @pytest.mark.asyncio
    async def test_no_market_id_filter(self, mock_account_data, mock_client):
        """Тест: без фильтра по market_id обрабатываются все ордера"""
        db_orders = [
            {
                "order_id": "order_1",
                "market_id": 100,
                "token_id": "token_yes",
                "token_name": "YES",
                "side": "BUY",
                "current_price": 0.500,
                "target_price": 0.490,
                "offset_ticks": 10,
                "amount": 100.0,
                "reposition_threshold_cents": 0.5,
                "status": "pending",
            },
            {
                "order_id": "order_2",
                "market_id": 200,
                "token_id": "token_no",
                "token_name": "NO",
                "side": "SELL",
                "current_price": 0.500,
                "target_price": 0.510,
                "offset_ticks": 10,
                "amount": 100.0,
                "reposition_threshold_cents": 0.5,
                "status": "pending",
            },
        ]

        with (
            patch(
                "opinion.sync_orders.get_account_orders", new_callable=AsyncMock
            ) as mock_get_orders,
            patch("opinion.sync_orders.create_client") as mock_create_client,
            patch("opinion.sync_orders.get_current_market_price") as mock_get_price,
            patch(
                "opinion.sync_orders.get_order_by_id", new_callable=AsyncMock
            ) as mock_get_order_by_id,
        ):
            # Когда market_id=None, должны вернуться все ордера
            mock_get_orders.return_value = db_orders
            mock_create_client.return_value = mock_client
            mock_get_price.return_value = 0.510
            mock_get_order_by_id.return_value = None

            (
                orders_to_cancel,
                orders_to_place,
                notifications,
            ) = await process_account_orders(1, mock_account_data, market_id=None)

            # Проверяем, что get_account_orders вызван с market_id=None
            mock_get_orders.assert_called_once_with(1, status="pending", market_id=None)

            # Должны быть обработаны оба ордера
            assert len(orders_to_cancel) == 2
            assert "order_1" in orders_to_cancel
            assert "order_2" in orders_to_cancel


class TestAsyncSyncAllOrders:
    """Тесты для функции async_sync_all_orders"""

    @pytest.mark.asyncio
    async def test_no_pending_orders(self):
        """Тест: нет pending ордеров"""
        with patch(
            "opinion.sync_orders.get_all_pending_orders_with_accounts",
            new_callable=AsyncMock,
        ) as mock_get_orders:
            mock_get_orders.return_value = []

            mock_bot = AsyncMock()
            await async_sync_all_orders(mock_bot)

            # Функция должна завершиться без ошибок
            mock_get_orders.assert_called_once_with(market_id=None)

    @pytest.mark.asyncio
    async def test_group_orders_by_account(self):
        """Тест: группировка ордеров по аккаунтам"""
        orders_with_accounts = [
            {
                "order": {
                    "order_id": "order_1",
                    "account_id": 1,
                    "market_id": 100,
                    "token_id": "token_yes",
                    "token_name": "YES",
                    "side": "BUY",
                    "current_price": 0.500,
                    "target_price": 0.490,
                    "offset_ticks": 10,
                    "amount": 100.0,
                    "reposition_threshold_cents": 0.5,
                    "status": "pending",
                },
                "account": {
                    "account_id": 1,
                    "telegram_id": 12345,
                    "wallet_address": "0x123",
                    "private_key": "key",
                    "api_key": "api_key",
                    "proxy_str": "proxy",
                    "proxy_status": "active",
                },
            },
            {
                "order": {
                    "order_id": "order_2",
                    "account_id": 1,
                    "market_id": 200,
                    "token_id": "token_no",
                    "token_name": "NO",
                    "side": "SELL",
                    "current_price": 0.500,
                    "target_price": 0.510,
                    "offset_ticks": 10,
                    "amount": 100.0,
                    "reposition_threshold_cents": 0.5,
                    "status": "pending",
                },
                "account": {
                    "account_id": 1,
                    "telegram_id": 12345,
                    "wallet_address": "0x123",
                    "private_key": "key",
                    "api_key": "api_key",
                    "proxy_str": "proxy",
                    "proxy_status": "active",
                },
            },
        ]

        with (
            patch(
                "opinion.sync_orders.get_all_pending_orders_with_accounts",
                new_callable=AsyncMock,
            ) as mock_get_orders,
            patch(
                "opinion.sync_orders.process_account_orders", new_callable=AsyncMock
            ) as mock_process,
        ):
            mock_get_orders.return_value = orders_with_accounts
            mock_process.return_value = ([], [], [])  # Нет ордеров для перестановки

            mock_bot = AsyncMock()
            await async_sync_all_orders(mock_bot)

            # Проверяем, что get_all_pending_orders_with_accounts вызван с market_id=None
            mock_get_orders.assert_called_once_with(market_id=None)

            # Проверяем, что process_account_orders вызван один раз для аккаунта 1
            assert mock_process.call_count == 1
            call_args = mock_process.call_args
            assert call_args[0][0] == 1  # account_id
            assert call_args[0][1]["account_id"] == 1  # account_data
            # Проверяем, что market_id передан в process_account_orders
            assert call_args.kwargs.get("market_id") is None

    @pytest.mark.asyncio
    async def test_skip_failed_proxy(self):
        """Тест: пропуск аккаунтов с failed proxy"""
        orders_with_accounts = [
            {
                "order": {
                    "order_id": "order_1",
                    "account_id": 1,
                    "market_id": 100,
                    "token_id": "token_yes",
                    "token_name": "YES",
                    "side": "BUY",
                    "current_price": 0.500,
                    "target_price": 0.490,
                    "offset_ticks": 10,
                    "amount": 100.0,
                    "reposition_threshold_cents": 0.5,
                    "status": "pending",
                },
                "account": {
                    "account_id": 1,
                    "telegram_id": 12345,
                    "wallet_address": "0x123",
                    "private_key": "key",
                    "api_key": "api_key",
                    "proxy_str": "proxy",
                    "proxy_status": "failed",  # Failed proxy
                },
            },
        ]

        with (
            patch(
                "opinion.sync_orders.get_all_pending_orders_with_accounts",
                new_callable=AsyncMock,
            ) as mock_get_orders,
            patch(
                "opinion.sync_orders.process_account_orders", new_callable=AsyncMock
            ) as mock_process,
        ):
            mock_get_orders.return_value = orders_with_accounts

            mock_bot = AsyncMock()
            await async_sync_all_orders(mock_bot)

            # Проверяем, что get_all_pending_orders_with_accounts вызван с market_id=None
            mock_get_orders.assert_called_once_with(market_id=None)

            # Аккаунт с failed proxy должен быть пропущен
            mock_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_accounts(self):
        """Тест: обработка нескольких аккаунтов"""
        orders_with_accounts = [
            {
                "order": {
                    "order_id": "order_1",
                    "account_id": 1,
                    "market_id": 100,
                    "token_id": "token_yes",
                    "token_name": "YES",
                    "side": "BUY",
                    "current_price": 0.500,
                    "target_price": 0.490,
                    "offset_ticks": 10,
                    "amount": 100.0,
                    "reposition_threshold_cents": 0.5,
                    "status": "pending",
                },
                "account": {
                    "account_id": 1,
                    "telegram_id": 12345,
                    "wallet_address": "0x123",
                    "private_key": "key",
                    "api_key": "api_key",
                    "proxy_str": "proxy",
                    "proxy_status": "active",
                },
            },
            {
                "order": {
                    "order_id": "order_2",
                    "account_id": 2,
                    "market_id": 200,
                    "token_id": "token_no",
                    "token_name": "NO",
                    "side": "SELL",
                    "current_price": 0.500,
                    "target_price": 0.510,
                    "offset_ticks": 10,
                    "amount": 100.0,
                    "reposition_threshold_cents": 0.5,
                    "status": "pending",
                },
                "account": {
                    "account_id": 2,
                    "telegram_id": 67890,
                    "wallet_address": "0x456",
                    "private_key": "key2",
                    "api_key": "api_key2",
                    "proxy_str": "proxy2",
                    "proxy_status": "active",
                },
            },
        ]

        with (
            patch(
                "opinion.sync_orders.get_all_pending_orders_with_accounts",
                new_callable=AsyncMock,
            ) as mock_get_orders,
            patch(
                "opinion.sync_orders.process_account_orders", new_callable=AsyncMock
            ) as mock_process,
        ):
            mock_get_orders.return_value = orders_with_accounts
            mock_process.return_value = ([], [], [])

            mock_bot = AsyncMock()
            await async_sync_all_orders(mock_bot)

            # Проверяем, что get_all_pending_orders_with_accounts вызван с market_id=None
            mock_get_orders.assert_called_once_with(market_id=None)

            # Должно быть 2 вызова для двух аккаунтов
            assert mock_process.call_count == 2
            # Проверяем, что market_id передан в process_account_orders
            for call in mock_process.call_args_list:
                assert call.kwargs.get("market_id") is None

    @pytest.mark.asyncio
    async def test_full_sync_cycle_success(self):
        """Тест: полный цикл синхронизации - успешная отмена и размещение"""
        orders_with_accounts = [
            {
                "order": {
                    "order_id": "order_1",
                    "account_id": 1,
                    "market_id": 100,
                    "token_id": "token_yes",
                    "token_name": "YES",
                    "side": "BUY",
                    "current_price": 0.500,
                    "target_price": 0.490,
                    "offset_ticks": 10,
                    "amount": 100.0,
                    "reposition_threshold_cents": 0.5,
                    "status": "pending",
                },
                "account": {
                    "account_id": 1,
                    "telegram_id": 12345,
                    "wallet_address": "0x123",
                    "private_key": "key",
                    "api_key": "api_key",
                    "proxy_str": "proxy",
                    "proxy_status": "active",
                },
            },
        ]

        # Мокируем результаты process_account_orders
        orders_to_cancel = ["order_1"]
        orders_to_place = [
            {
                "old_order_id": "order_1",
                "market_id": 100,
                "token_id": "token_yes",
                "token_name": "YES",
                "side": MockOrderSide.BUY,
                "price": 0.500,
                "amount": 100.0,
                "current_price_at_creation": 0.510,
                "target_price": 0.500,
                "account_id": 1,
                "telegram_id": 12345,
            }
        ]
        price_change_notifications = [
            {
                "order_id": "order_1",
                "market_id": 100,
                "token_name": "YES",
                "side": "BUY",
                "old_current_price": 0.500,
                "new_current_price": 0.510,
                "old_target_price": 0.490,
                "new_target_price": 0.500,
                "price_change": 0.010,
                "target_price_change": 0.010,
                "target_price_change_cents": 1.0,
                "reposition_threshold_cents": 0.5,
                "offset_ticks": 10,
                "will_reposition": True,
            }
        ]

        # Мокируем результаты отмены (успешная)
        cancel_result = MagicMock()
        cancel_result.errno = 0
        cancel_results = [{"success": True, "result": cancel_result}]

        # Мокируем результаты размещения (успешная)
        place_result_data = MagicMock()
        place_result_data.errno = 0
        place_result_data.result = MagicMock()
        place_result_data.result.order_data = MagicMock()
        place_result_data.result.order_data.order_id = "new_order_1"
        place_results = [{"success": True, "result": place_result_data}]

        with (
            patch(
                "opinion.sync_orders.get_all_pending_orders_with_accounts",
                new_callable=AsyncMock,
            ) as mock_get_orders,
            patch(
                "opinion.sync_orders.process_account_orders", new_callable=AsyncMock
            ) as mock_process,
            patch("opinion.sync_orders.create_client") as mock_create_client,
            patch("opinion.sync_orders.cancel_orders_batch") as mock_cancel,
            patch("opinion.sync_orders.place_orders_batch") as mock_place,
            patch(
                "opinion.sync_orders.update_order_in_db", new_callable=AsyncMock
            ) as mock_update_db,
            patch(
                "opinion.sync_orders.send_price_change_notification",
                new_callable=AsyncMock,
            ) as mock_send_price,
            patch(
                "opinion.sync_orders.send_order_updated_notification",
                new_callable=AsyncMock,
            ) as mock_send_updated,
            patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread,
        ):
            mock_get_orders.return_value = orders_with_accounts
            mock_process.return_value = (
                orders_to_cancel,
                orders_to_place,
                price_change_notifications,
            )
            mock_client = MagicMock()
            mock_create_client.return_value = mock_client
            mock_cancel.return_value = cancel_results
            mock_place.return_value = place_results

            # Мокируем asyncio.to_thread для синхронных функций
            # asyncio.to_thread - это async функция, поэтому используем side_effect с async функцией
            async def to_thread_side_effect(func, *args):
                return func(*args)

            mock_to_thread.side_effect = to_thread_side_effect

            mock_bot = AsyncMock()
            await async_sync_all_orders(mock_bot)

            # Проверяем, что get_all_pending_orders_with_accounts вызван с market_id=None
            mock_get_orders.assert_called_once_with(market_id=None)

            # Проверяем, что market_id передан в process_account_orders
            mock_process.assert_called_once()
            assert mock_process.call_args.kwargs.get("market_id") is None

            # Проверяем, что уведомления отправлены
            assert mock_send_price.call_count == 1

            # Проверяем, что отмена вызвана
            mock_cancel.assert_called_once_with(mock_client, orders_to_cancel)

            # Проверяем, что размещение вызвано
            mock_place.assert_called_once()

            # Проверяем, что БД обновлена
            mock_update_db.assert_called_once_with(
                "order_1", "new_order_1", 0.510, 0.500
            )

            # Проверяем, что отправлено уведомление об успешном обновлении
            mock_send_updated.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_cancellation_failure(self):
        """Тест: ошибка при отмене ордера - размещение не выполняется"""
        orders_with_accounts = [
            {
                "order": {
                    "order_id": "order_1",
                    "account_id": 1,
                    "market_id": 100,
                    "token_id": "token_yes",
                    "token_name": "YES",
                    "side": "BUY",
                    "current_price": 0.500,
                    "target_price": 0.490,
                    "offset_ticks": 10,
                    "amount": 100.0,
                    "reposition_threshold_cents": 0.5,
                    "status": "pending",
                },
                "account": {
                    "account_id": 1,
                    "telegram_id": 12345,
                    "wallet_address": "0x123",
                    "private_key": "key",
                    "api_key": "api_key",
                    "proxy_str": "proxy",
                    "proxy_status": "active",
                },
            },
        ]

        orders_to_cancel = ["order_1"]
        orders_to_place = [
            {
                "old_order_id": "order_1",
                "market_id": 100,
                "token_id": "token_yes",
                "token_name": "YES",
                "side": MockOrderSide.BUY,
                "price": 0.500,
                "amount": 100.0,
                "current_price_at_creation": 0.510,
                "target_price": 0.500,
                "account_id": 1,
                "telegram_id": 12345,
            }
        ]

        # Мокируем результаты отмены (неуспешная)
        cancel_result = MagicMock()
        cancel_result.errno = 10207  # Ошибка
        cancel_result.errmsg = "Order not found"
        cancel_results = [{"success": True, "result": cancel_result}]

        with (
            patch(
                "opinion.sync_orders.get_all_pending_orders_with_accounts",
                new_callable=AsyncMock,
            ) as mock_get_orders,
            patch(
                "opinion.sync_orders.process_account_orders", new_callable=AsyncMock
            ) as mock_process,
            patch("opinion.sync_orders.create_client") as mock_create_client,
            patch("opinion.sync_orders.cancel_orders_batch") as mock_cancel,
            patch("opinion.sync_orders.place_orders_batch") as mock_place,
            patch(
                "opinion.sync_orders.send_cancellation_error_notification",
                new_callable=AsyncMock,
            ) as mock_send_cancel_error,
            patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread,
        ):
            mock_get_orders.return_value = orders_with_accounts
            mock_process.return_value = (orders_to_cancel, orders_to_place, [])
            mock_client = MagicMock()
            mock_create_client.return_value = mock_client
            mock_cancel.return_value = cancel_results

            # Мокируем asyncio.to_thread так, чтобы он вызывал функцию напрямую
            async def to_thread_side_effect(func, *args):
                return func(*args)

            mock_to_thread.side_effect = to_thread_side_effect

            mock_bot = AsyncMock()
            await async_sync_all_orders(mock_bot)

            # Проверяем, что get_all_pending_orders_with_accounts вызван с market_id=None
            mock_get_orders.assert_called_once_with(market_id=None)

            # Проверяем, что отмена вызвана
            mock_cancel.assert_called_once()

            # Проверяем, что размещение НЕ вызвано (из-за ошибки отмены)
            mock_place.assert_not_called()

            # Проверяем, что отправлено уведомление об ошибке отмены
            mock_send_cancel_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_placement_failure(self):
        """Тест: ошибка при размещении ордера - отправляется уведомление об ошибке"""
        orders_with_accounts = [
            {
                "order": {
                    "order_id": "order_1",
                    "account_id": 1,
                    "market_id": 100,
                    "token_id": "token_yes",
                    "token_name": "YES",
                    "side": "BUY",
                    "current_price": 0.500,
                    "target_price": 0.490,
                    "offset_ticks": 10,
                    "amount": 100.0,
                    "reposition_threshold_cents": 0.5,
                    "status": "pending",
                },
                "account": {
                    "account_id": 1,
                    "telegram_id": 12345,
                    "wallet_address": "0x123",
                    "private_key": "key",
                    "api_key": "api_key",
                    "proxy_str": "proxy",
                    "proxy_status": "active",
                },
            },
        ]

        orders_to_cancel = ["order_1"]
        orders_to_place = [
            {
                "old_order_id": "order_1",
                "market_id": 100,
                "token_id": "token_yes",
                "token_name": "YES",
                "side": MockOrderSide.BUY,
                "price": 0.500,
                "amount": 100.0,
                "current_price_at_creation": 0.510,
                "target_price": 0.500,
                "account_id": 1,
                "telegram_id": 12345,
            }
        ]

        # Мокируем результаты отмены (успешная)
        cancel_result = MagicMock()
        cancel_result.errno = 0
        cancel_results = [{"success": True, "result": cancel_result}]

        # Мокируем результаты размещения (неуспешная)
        place_result_data = MagicMock()
        place_result_data.errno = 10207  # Ошибка
        place_result_data.errmsg = "Insufficient balance"
        place_results = [{"success": True, "result": place_result_data}]

        with (
            patch(
                "opinion.sync_orders.get_all_pending_orders_with_accounts",
                new_callable=AsyncMock,
            ) as mock_get_orders,
            patch(
                "opinion.sync_orders.process_account_orders", new_callable=AsyncMock
            ) as mock_process,
            patch("opinion.sync_orders.create_client") as mock_create_client,
            patch("opinion.sync_orders.cancel_orders_batch") as mock_cancel,
            patch("opinion.sync_orders.place_orders_batch") as mock_place,
            patch(
                "opinion.sync_orders.update_order_in_db", new_callable=AsyncMock
            ) as mock_update_db,
            patch(
                "opinion.sync_orders.send_order_placement_error_notification",
                new_callable=AsyncMock,
            ) as mock_send_placement_error,
            patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread,
        ):
            mock_get_orders.return_value = orders_with_accounts
            mock_process.return_value = (orders_to_cancel, orders_to_place, [])
            mock_client = MagicMock()
            mock_create_client.return_value = mock_client
            mock_cancel.return_value = cancel_results
            mock_place.return_value = place_results

            # Мокируем asyncio.to_thread так, чтобы он вызывал функцию напрямую
            async def to_thread_side_effect(func, *args):
                return func(*args)

            mock_to_thread.side_effect = to_thread_side_effect

            mock_bot = AsyncMock()
            await async_sync_all_orders(mock_bot)

            # Проверяем, что get_all_pending_orders_with_accounts вызван с market_id=None
            mock_get_orders.assert_called_once_with(market_id=None)

            # Проверяем, что размещение вызвано
            mock_place.assert_called_once()

            # Проверяем, что БД НЕ обновлена (из-за ошибки размещения)
            mock_update_db.assert_not_called()

            # Проверяем, что отправлено уведомление об ошибке размещения
            mock_send_placement_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_with_market_id_filter(self):
        """Тест: синхронизация с фильтром по market_id"""
        orders_with_accounts = [
            {
                "order": {
                    "order_id": "order_1",
                    "account_id": 1,
                    "market_id": 100,
                    "token_id": "token_yes",
                    "token_name": "YES",
                    "side": "BUY",
                    "current_price": 0.500,
                    "target_price": 0.490,
                    "offset_ticks": 10,
                    "amount": 100.0,
                    "reposition_threshold_cents": 0.5,
                    "status": "pending",
                },
                "account": {
                    "account_id": 1,
                    "telegram_id": 12345,
                    "wallet_address": "0x123",
                    "private_key": "key",
                    "api_key": "api_key",
                    "proxy_str": "proxy",
                    "proxy_status": "active",
                },
            },
            {
                "order": {
                    "order_id": "order_2",
                    "account_id": 1,
                    "market_id": 200,
                    "token_id": "token_no",
                    "token_name": "NO",
                    "side": "SELL",
                    "current_price": 0.500,
                    "target_price": 0.510,
                    "offset_ticks": 10,
                    "amount": 100.0,
                    "reposition_threshold_cents": 0.5,
                    "status": "pending",
                },
                "account": {
                    "account_id": 1,
                    "telegram_id": 12345,
                    "wallet_address": "0x123",
                    "private_key": "key",
                    "api_key": "api_key",
                    "proxy_str": "proxy",
                    "proxy_status": "active",
                },
            },
        ]

        with (
            patch(
                "opinion.sync_orders.get_all_pending_orders_with_accounts",
                new_callable=AsyncMock,
            ) as mock_get_orders,
            patch(
                "opinion.sync_orders.process_account_orders", new_callable=AsyncMock
            ) as mock_process,
        ):
            # Когда указан market_id=100, должны вернуться только ордера для market_id=100
            mock_get_orders.return_value = [
                orders_with_accounts[0]
            ]  # Только первый ордер
            mock_process.return_value = ([], [], [])

            mock_bot = AsyncMock()
            await async_sync_all_orders(mock_bot, market_id=100)

            # Проверяем, что get_all_pending_orders_with_accounts вызван с правильным market_id
            mock_get_orders.assert_called_once_with(market_id=100)

            # Проверяем, что process_account_orders вызван с правильным market_id
            mock_process.assert_called_once()
            assert mock_process.call_args.kwargs.get("market_id") == 100

    @pytest.mark.asyncio
    async def test_sync_without_market_id(self):
        """Тест: синхронизация без фильтра по market_id (обрабатываются все ордера)"""
        orders_with_accounts = [
            {
                "order": {
                    "order_id": "order_1",
                    "account_id": 1,
                    "market_id": 100,
                    "token_id": "token_yes",
                    "token_name": "YES",
                    "side": "BUY",
                    "current_price": 0.500,
                    "target_price": 0.490,
                    "offset_ticks": 10,
                    "amount": 100.0,
                    "reposition_threshold_cents": 0.5,
                    "status": "pending",
                },
                "account": {
                    "account_id": 1,
                    "telegram_id": 12345,
                    "wallet_address": "0x123",
                    "private_key": "key",
                    "api_key": "api_key",
                    "proxy_str": "proxy",
                    "proxy_status": "active",
                },
            },
        ]

        with (
            patch(
                "opinion.sync_orders.get_all_pending_orders_with_accounts",
                new_callable=AsyncMock,
            ) as mock_get_orders,
            patch(
                "opinion.sync_orders.process_account_orders", new_callable=AsyncMock
            ) as mock_process,
        ):
            mock_get_orders.return_value = orders_with_accounts
            mock_process.return_value = ([], [], [])

            mock_bot = AsyncMock()
            # Вызываем без market_id (по умолчанию None)
            await async_sync_all_orders(mock_bot)

            # Проверяем, что get_all_pending_orders_with_accounts вызван с market_id=None
            mock_get_orders.assert_called_once_with(market_id=None)

            # Проверяем, что process_account_orders вызван с market_id=None
            mock_process.assert_called_once()
            assert mock_process.call_args.kwargs.get("market_id") is None


class TestCancellationErrorNotification:
    """Тесты для функции send_cancellation_error_notification"""

    @pytest.mark.asyncio
    async def test_send_notification_single_order(self):
        """Тест: отправка уведомления об ошибке отмены одного ордера"""
        mock_bot = AsyncMock()
        telegram_id = 12345

        failed_orders = [
            {
                "order_id": "order_123",
                "market_id": 100,
                "token_name": "YES",
                "side": "BUY",
                "errno": 10207,
                "errmsg": "Order not found",
            }
        ]

        await send_cancellation_error_notification(mock_bot, telegram_id, failed_orders)

        # Проверяем поведение: сообщение должно быть отправлено правильному пользователю
        assert mock_bot.send_message.called
        call_args = mock_bot.send_message.call_args
        assert call_args.kwargs["chat_id"] == telegram_id

        # Проверяем, что ключевая информация присутствует (без проверки форматирования)
        message = call_args.kwargs["text"]
        assert "order_123" in message  # ID ордера
        assert (
            "10207" in message or "Order not found" in message
        )  # Информация об ошибке

    @pytest.mark.asyncio
    async def test_send_notification_multiple_orders(self):
        """Тест: отправка уведомления об ошибке отмены нескольких ордеров"""
        mock_bot = AsyncMock()
        telegram_id = 12345

        failed_orders = [
            {
                "order_id": "order_1",
                "market_id": 100,
                "token_name": "YES",
                "side": "BUY",
                "errno": 10207,
                "errmsg": "Order not found",
            },
            {
                "order_id": "order_2",
                "market_id": 200,
                "token_name": "NO",
                "side": "SELL",
                "errno": 10208,
                "errmsg": "Insufficient balance",
            },
        ]

        await send_cancellation_error_notification(mock_bot, telegram_id, failed_orders)

        assert mock_bot.send_message.called
        call_args = mock_bot.send_message.call_args
        message = call_args.kwargs["text"]

        # Проверяем поведение: все ордера должны быть упомянуты в сообщении
        assert "order_1" in message
        assert "order_2" in message

    @pytest.mark.asyncio
    async def test_empty_failed_orders_list(self):
        """Тест: пустой список неудачных отмен (не должно отправляться сообщение)"""
        mock_bot = AsyncMock()
        telegram_id = 12345

        await send_cancellation_error_notification(mock_bot, telegram_id, [])

        # Проверяем, что send_message НЕ был вызван
        assert not mock_bot.send_message.called

    @pytest.mark.asyncio
    async def test_missing_fields_in_failed_order(self):
        """Тест: обработка отсутствующих полей в failed_orders"""
        mock_bot = AsyncMock()
        telegram_id = 12345

        # Ордер с неполными данными
        failed_orders = [
            {
                "order_id": "order_123",
                # Отсутствуют некоторые поля
            }
        ]

        await send_cancellation_error_notification(mock_bot, telegram_id, failed_orders)

        # Проверяем поведение: функция должна обработать неполные данные и отправить сообщение
        assert mock_bot.send_message.called
        call_args = mock_bot.send_message.call_args
        message = call_args.kwargs["text"]
        assert "order_123" in message  # Основная информация должна быть

    @pytest.mark.asyncio
    async def test_send_notification_error_handling(self):
        """Тест: обработка ошибки при отправке уведомления"""
        mock_bot = AsyncMock()
        mock_bot.send_message.side_effect = Exception("Telegram API error")
        telegram_id = 12345

        failed_orders = [
            {
                "order_id": "order_123",
                "market_id": 100,
                "token_name": "YES",
                "side": "BUY",
                "errno": 10207,
                "errmsg": "Order not found",
            }
        ]

        # Функция должна обработать ошибку и не упасть
        await send_cancellation_error_notification(mock_bot, telegram_id, failed_orders)

        # Проверяем, что send_message был вызван (ошибка обработана внутри функции)
        assert mock_bot.send_message.called


class TestOrderPlacementErrorNotification:
    """Тесты для функции send_order_placement_error_notification"""

    @pytest.mark.asyncio
    async def test_send_notification_buy_order(self):
        """Тест: отправка уведомления об ошибке размещения BUY ордера"""
        mock_bot = AsyncMock()
        telegram_id = 12345

        # Мокируем OrderSide в sync_orders модуле
        with patch("opinion.sync_orders.OrderSide", MockOrderSide):
            order_params = {
                "market_id": 100,
                "token_name": "YES",
                "side": MockOrderSide.BUY,
                "current_price_at_creation": 0.500,
                "target_price": 0.490,
                "amount": 100.0,
            }
            old_order_id = "order_123"
            errno = 10207
            errmsg = "Insufficient balance"

            await send_order_placement_error_notification(
                mock_bot, telegram_id, order_params, old_order_id, errno, errmsg
            )

            # Проверяем поведение: сообщение отправлено правильному пользователю
            assert mock_bot.send_message.called
            call_args = mock_bot.send_message.call_args
            assert call_args.kwargs["chat_id"] == telegram_id

            # Проверяем, что ключевая информация присутствует
            message = call_args.kwargs["text"]
            assert "order_123" in message  # ID отмененного ордера
            assert str(errno) in message or errmsg in message  # Информация об ошибке

    @pytest.mark.asyncio
    async def test_send_notification_sell_order(self):
        """Тест: отправка уведомления об ошибке размещения SELL ордера"""
        mock_bot = AsyncMock()
        telegram_id = 12345

        # Мокируем OrderSide для этого теста
        with patch("opinion.sync_orders.OrderSide", MockOrderSide):
            order_params = {
                "market_id": 200,
                "token_name": "NO",
                "side": MockOrderSide.SELL,
                "current_price_at_creation": 0.600,
                "target_price": 0.610,
                "amount": 50.0,
            }
            old_order_id = "order_456"
            errno = 10208
            errmsg = "Market closed"

            await send_order_placement_error_notification(
                mock_bot, telegram_id, order_params, old_order_id, errno, errmsg
            )

            # Проверяем поведение: сообщение отправлено с ключевой информацией
            assert mock_bot.send_message.called
            call_args = mock_bot.send_message.call_args
            message = call_args.kwargs["text"]
            assert "order_456" in message  # ID отмененного ордера
            assert str(errno) in message or errmsg in message  # Информация об ошибке

    @pytest.mark.asyncio
    async def test_send_notification_missing_fields(self):
        """Тест: обработка отсутствующих полей в order_params"""
        mock_bot = AsyncMock()
        telegram_id = 12345

        # Мокируем OrderSide для этого теста
        with patch("opinion.sync_orders.OrderSide", MockOrderSide):
            # order_params с неполными данными
            order_params = {
                "market_id": 100,
                # Отсутствуют некоторые поля
            }
            old_order_id = "order_123"
            errno = 10207
            errmsg = "Error"

            # Проверяем поведение: функция должна обработать отсутствующие поля
            await send_order_placement_error_notification(
                mock_bot, telegram_id, order_params, old_order_id, errno, errmsg
            )

            # Функция должна отправить сообщение даже с неполными данными
            assert mock_bot.send_message.called
            call_args = mock_bot.send_message.call_args
            message = call_args.kwargs["text"]
            assert "order_123" in message  # Основная информация должна быть

    @pytest.mark.asyncio
    async def test_send_notification_error_handling(self):
        """Тест: обработка ошибки при отправке уведомления"""
        mock_bot = AsyncMock()
        mock_bot.send_message.side_effect = Exception("Telegram API error")
        telegram_id = 12345

        # Мокируем OrderSide для этого теста
        with patch("opinion.sync_orders.OrderSide", MockOrderSide):
            order_params = {
                "market_id": 100,
                "token_name": "YES",
                "side": MockOrderSide.BUY,
                "current_price_at_creation": 0.500,
                "target_price": 0.490,
                "amount": 100.0,
            }
            old_order_id = "order_123"
            errno = 10207
            errmsg = "Error"

            # Функция должна обработать ошибку и не упасть
            await send_order_placement_error_notification(
                mock_bot, telegram_id, order_params, old_order_id, errno, errmsg
            )

            # Проверяем, что send_message был вызван (ошибка обработана внутри функции)
            assert mock_bot.send_message.called


class TestOrderFilledNotification:
    """Тесты для функции send_order_filled_notification"""

    @pytest.mark.asyncio
    async def test_send_notification_buy_order(self):
        """Тест: отправка уведомления об исполнении BUY ордера"""
        mock_bot = AsyncMock()
        telegram_id = 12345

        # Мокируем API ордер
        api_order = MagicMock()
        api_order.order_id = "order_123"
        api_order.market_id = 100
        api_order.market_title = "Test Market"
        api_order.root_market_id = 200
        api_order.root_market_title = "Root Market"
        api_order.price = "0.490"
        api_order.side_enum = "Buy"
        api_order.outcome = "YES"
        api_order.filled_amount = "100.0"
        api_order.order_amount = "100.0"

        await send_order_filled_notification(mock_bot, telegram_id, api_order)

        # Проверяем поведение: сообщение отправлено правильному пользователю
        assert mock_bot.send_message.called
        call_args = mock_bot.send_message.call_args
        assert call_args.kwargs["chat_id"] == telegram_id

        # Проверяем, что ключевая информация присутствует
        message = call_args.kwargs["text"]
        assert "order_123" in message  # ID ордера
        assert "YES" in message or "Buy" in message  # Информация об ордере

    @pytest.mark.asyncio
    async def test_send_notification_sell_order(self):
        """Тест: отправка уведомления об исполнении SELL ордера"""
        mock_bot = AsyncMock()
        telegram_id = 12345

        # Мокируем API ордер
        api_order = MagicMock()
        api_order.order_id = "order_456"
        api_order.market_id = 200
        api_order.market_title = "Test Market 2"
        api_order.root_market_id = None  # Нет root_market_id
        api_order.root_market_title = "N/A"
        api_order.price = "0.510"
        api_order.side_enum = "Sell"
        api_order.outcome = "NO"
        api_order.filled_amount = "50.0"
        api_order.order_amount = "50.0"

        await send_order_filled_notification(mock_bot, telegram_id, api_order)

        # Проверяем поведение: сообщение отправлено с ключевой информацией
        assert mock_bot.send_message.called
        call_args = mock_bot.send_message.call_args
        message = call_args.kwargs["text"]
        assert "order_456" in message  # ID ордера
        assert "NO" in message or "Sell" in message  # Информация об ордере

    @pytest.mark.asyncio
    async def test_send_notification_missing_fields(self):
        """Тест: обработка отсутствующих полей в api_order"""
        mock_bot = AsyncMock()
        telegram_id = 12345

        # Мокируем API ордер с неполными данными
        api_order = MagicMock()
        api_order.order_id = "order_123"
        # Некоторые поля отсутствуют

        # Проверяем поведение: функция должна обработать неполные данные
        await send_order_filled_notification(mock_bot, telegram_id, api_order)

        # Функция должна отправить сообщение даже с неполными данными
        assert mock_bot.send_message.called
        call_args = mock_bot.send_message.call_args
        message = call_args.kwargs["text"]
        assert "order_123" in message  # Основная информация должна быть

    @pytest.mark.asyncio
    async def test_send_notification_error_handling(self):
        """Тест: обработка ошибки при отправке уведомления"""
        mock_bot = AsyncMock()
        mock_bot.send_message.side_effect = Exception("Telegram API error")
        telegram_id = 12345

        api_order = MagicMock()
        api_order.order_id = "order_123"
        api_order.market_id = 100
        api_order.market_title = "Test Market"
        api_order.root_market_id = 200
        api_order.root_market_title = "Root Market"
        api_order.price = "0.490"
        api_order.side_enum = "Buy"
        api_order.outcome = "YES"
        api_order.filled_amount = "100.0"
        api_order.order_amount = "100.0"

        # Функция должна обработать ошибку и не упасть
        await send_order_filled_notification(mock_bot, telegram_id, api_order)

        # Проверяем, что send_message был вызван (ошибка обработана внутри функции)
        assert mock_bot.send_message.called


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
