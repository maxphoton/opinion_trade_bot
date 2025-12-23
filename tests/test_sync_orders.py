"""
Тесты для bot/sync_orders.py

Покрывает различные кейсы:
- Когда изменение цены достаточно для перестановки ордера
- Когда изменение недостаточно
- Проверка уведомлений
- Проверка правильности списков для отмены/размещения
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Dict, List

# Импортируем функции для тестирования
# conftest.py настроит sys.path для работы с относительными импортами
from sync_orders import (
    process_user_orders,
    calculate_new_target_price,
    get_current_market_price
)
from config import TICK_SIZE


class TestCalculateNewTargetPrice:
    """Тесты для функции calculate_new_target_price"""
    
    def test_calculate_buy_price(self):
        """Тест расчета целевой цены для BUY ордера"""
        current_price = 0.5
        offset_ticks = 10
        side = "BUY"
        
        result = calculate_new_target_price(current_price, side, offset_ticks)
        
        # Для BUY: target = current_price - offset_ticks * TICK_SIZE
        expected = current_price - offset_ticks * TICK_SIZE
        assert result == expected
    
    def test_calculate_sell_price(self):
        """Тест расчета целевой цены для SELL ордера"""
        current_price = 0.5
        offset_ticks = 10
        side = "SELL"
        
        result = calculate_new_target_price(current_price, side, offset_ticks)
        
        # Для SELL: target = current_price + offset_ticks * TICK_SIZE
        expected = current_price + offset_ticks * TICK_SIZE
        assert result == expected
    
    def test_price_limits_min(self):
        """Тест ограничения минимальной цены (0.001)"""
        current_price = 0.01
        offset_ticks = 100  # Большой отступ для BUY
        side = "BUY"
        
        result = calculate_new_target_price(current_price, side, offset_ticks)
        
        # Цена не должна быть меньше 0.001
        assert result >= 0.001
    
    def test_price_limits_max(self):
        """Тест ограничения максимальной цены (0.999)"""
        current_price = 0.99
        offset_ticks = 100  # Большой отступ для SELL
        side = "SELL"
        
        result = calculate_new_target_price(current_price, side, offset_ticks)
        
        # Цена не должна быть больше 0.999
        assert result <= 0.999


class TestProcessUserOrders:
    """Тесты для функции process_user_orders"""
    
    @pytest.fixture
    def mock_user(self):
        """Мок пользователя"""
        return {
            'telegram_id': 12345,
            'username': 'test_user',
            'wallet_address': '0x123',
            'private_key': 'key',
            'api_key': 'api_key'
        }
    
    @pytest.fixture
    def mock_client(self):
        """Мок клиента Opinion SDK"""
        client = MagicMock()
        return client
    
    @pytest.fixture
    def mock_orderbook_response(self):
        """Мок ответа orderbook"""
        response = MagicMock()
        response.errno = 0
        
        # Мок orderbook с bids и asks
        orderbook = MagicMock()
        
        # Мок bids (для BUY)
        bid1 = MagicMock()
        bid1.price = "0.500"
        bid2 = MagicMock()
        bid2.price = "0.499"
        orderbook.bids = [bid1, bid2]
        
        # Мок asks (для SELL)
        ask1 = MagicMock()
        ask1.price = "0.501"
        ask2 = MagicMock()
        ask2.price = "0.502"
        orderbook.asks = [ask1, ask2]
        
        response.result = orderbook
        return response
    
    @pytest.mark.asyncio
    async def test_no_user(self):
        """Тест: пользователь не найден"""
        with patch('sync_orders.get_user', new_callable=AsyncMock) as mock_get_user:
            mock_get_user.return_value = None
            
            orders_to_cancel, orders_to_place, notifications = await process_user_orders(12345)
            
            assert orders_to_cancel == []
            assert orders_to_place == []
            assert notifications == []
    
    @pytest.mark.asyncio
    async def test_no_orders(self, mock_user):
        """Тест: у пользователя нет активных ордеров"""
        with patch('sync_orders.get_user', new_callable=AsyncMock) as mock_get_user, \
             patch('sync_orders.get_user_orders', new_callable=AsyncMock) as mock_get_orders:
            
            mock_get_user.return_value = mock_user
            mock_get_orders.return_value = []
            
            orders_to_cancel, orders_to_place, notifications = await process_user_orders(12345)
            
            assert orders_to_cancel == []
            assert orders_to_place == []
            assert notifications == []
    
    @pytest.mark.asyncio
    async def test_reposition_sufficient_change(self, mock_user, mock_client):
        """Тест: изменение достаточно для перестановки ордера"""
        # Настройка ордера: изменение будет 1.0 цент (>= 0.5)
        db_order = {
            "order_id": "order_123",
            "market_id": 100,
            "token_id": "token_yes",
            "token_name": "YES",
            "side": "BUY",
            "current_price": 0.500,  # Старая текущая цена
            "target_price": 0.490,   # Старая целевая цена (offset 10 ticks = 0.01 = 1.0 cent)
            "offset_ticks": 10,
            "amount": 100.0,
            "reposition_threshold_cents": 0.5
        }
        
        # Новая текущая цена: 0.510 (изменилась на 0.01)
        # Новая целевая цена: 0.500 (0.510 - 10*0.001)
        # Изменение целевой цены: 0.010 = 1.0 цент (>= 0.5)
        
        with patch('sync_orders.get_user', new_callable=AsyncMock) as mock_get_user, \
             patch('sync_orders.get_user_orders', new_callable=AsyncMock) as mock_get_orders, \
             patch('sync_orders.create_client') as mock_create_client, \
             patch('sync_orders.get_current_market_price') as mock_get_price:
            
            mock_get_user.return_value = mock_user
            mock_get_orders.return_value = [db_order]
            mock_create_client.return_value = mock_client
            mock_get_price.return_value = 0.510  # Новая текущая цена
            
            orders_to_cancel, orders_to_place, notifications = await process_user_orders(12345)
            
            # Проверяем, что ордер добавлен в списки для отмены/размещения
            assert len(orders_to_cancel) == 1
            assert orders_to_cancel[0] == "order_123"
            assert len(orders_to_place) == 1
            
            # Проверяем параметры нового ордера
            new_order = orders_to_place[0]
            assert new_order["old_order_id"] == "order_123"
            assert new_order["market_id"] == 100
            assert new_order["token_id"] == "token_yes"
            assert new_order["price"] == pytest.approx(0.500, abs=0.0001)  # 0.510 - 10*0.001
            
            # Проверяем уведомление
            assert len(notifications) == 1
            notification = notifications[0]
            assert notification["order_id"] == "order_123"
            assert notification["will_reposition"] is True
            assert notification["target_price_change_cents"] >= 0.5
    
    @pytest.mark.asyncio
    async def test_reposition_insufficient_change(self, mock_user, mock_client):
        """Тест: изменение недостаточно для перестановки ордера"""
        # Настройка ордера: изменение будет 0.3 цент (< 0.5)
        db_order = {
            "order_id": "order_456",
            "market_id": 100,
            "token_id": "token_no",
            "token_name": "NO",
            "side": "SELL",
            "current_price": 0.500,  # Старая текущая цена
            "target_price": 0.510,   # Старая целевая цена (offset 10 ticks = 0.01)
            "offset_ticks": 10,
            "amount": 100.0,
            "reposition_threshold_cents": 0.5
        }
        
        # Новая текущая цена: 0.503 (изменилась на 0.003)
        # Новая целевая цена: 0.513 (0.503 + 10*0.001)
        # Изменение целевой цены: 0.003 = 0.3 цент (< 0.5)
        
        with patch('sync_orders.get_user', new_callable=AsyncMock) as mock_get_user, \
             patch('sync_orders.get_user_orders', new_callable=AsyncMock) as mock_get_orders, \
             patch('sync_orders.create_client') as mock_create_client, \
             patch('sync_orders.get_current_market_price') as mock_get_price:
            
            mock_get_user.return_value = mock_user
            mock_get_orders.return_value = [db_order]
            mock_create_client.return_value = mock_client
            mock_get_price.return_value = 0.503  # Новая текущая цена
            
            orders_to_cancel, orders_to_place, notifications = await process_user_orders(12345)
            
            # Проверяем, что ордер НЕ добавлен в списки для отмены/размещения
            assert len(orders_to_cancel) == 0
            assert len(orders_to_place) == 0
            
            # Проверяем, что уведомление все равно отправлено
            assert len(notifications) == 1
            notification = notifications[0]
            assert notification["order_id"] == "order_456"
            assert notification["will_reposition"] is False
            assert notification["target_price_change_cents"] < 0.5
    
    @pytest.mark.asyncio
    async def test_no_price_change(self, mock_user, mock_client):
        """Тест: цена не изменилась"""
        db_order = {
            "order_id": "order_789",
            "market_id": 100,
            "token_id": "token_yes",
            "token_name": "YES",
            "side": "BUY",
            "current_price": 0.500,
            "target_price": 0.490,  # offset 10 ticks
            "offset_ticks": 10,
            "amount": 100.0,
            "reposition_threshold_cents": 0.5
        }
        
        # Цена не изменилась
        with patch('sync_orders.get_user', new_callable=AsyncMock) as mock_get_user, \
             patch('sync_orders.get_user_orders', new_callable=AsyncMock) as mock_get_orders, \
             patch('sync_orders.create_client') as mock_create_client, \
             patch('sync_orders.get_current_market_price') as mock_get_price:
            
            mock_get_user.return_value = mock_user
            mock_get_orders.return_value = [db_order]
            mock_create_client.return_value = mock_client
            mock_get_price.return_value = 0.500  # Та же цена
            
            orders_to_cancel, orders_to_place, notifications = await process_user_orders(12345)
            
            # Новая целевая цена будет та же: 0.490 (0.500 - 10*0.001)
            # Изменение: 0.0 (< 0.5)
            assert len(orders_to_cancel) == 0
            assert len(orders_to_place) == 0
            assert len(notifications) == 1
            assert notifications[0]["will_reposition"] is False
            assert notifications[0]["target_price_change_cents"] == 0.0
    
    @pytest.mark.asyncio
    async def test_multiple_orders_mixed(self, mock_user, mock_client):
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
                "reposition_threshold_cents": 0.5
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
                "reposition_threshold_cents": 0.5
            }
        ]
        
        with patch('sync_orders.get_user', new_callable=AsyncMock) as mock_get_user, \
             patch('sync_orders.get_user_orders', new_callable=AsyncMock) as mock_get_orders, \
             patch('sync_orders.create_client') as mock_create_client, \
             patch('sync_orders.get_current_market_price') as mock_get_price:
            
            mock_get_user.return_value = mock_user
            mock_get_orders.return_value = db_orders
            mock_create_client.return_value = mock_client
            
            # Первый ордер: изменение достаточно (1.0 цент)
            # Второй ордер: изменение недостаточно (0.3 цента)
            # get_current_market_price принимает (client, token_id, side)
            def get_price_side_effect(client, token_id, side):
                if token_id == "token_yes" and side == "BUY":
                    return 0.510  # Изменение 0.01 = 1.0 цент
                elif token_id == "token_no" and side == "SELL":
                    return 0.503  # Изменение 0.003 = 0.3 цента
                return None
            
            mock_get_price.side_effect = get_price_side_effect
            
            orders_to_cancel, orders_to_place, notifications = await process_user_orders(12345)
            
            # Первый ордер должен быть переставлен
            assert len(orders_to_cancel) == 1
            assert orders_to_cancel[0] == "order_1"
            assert len(orders_to_place) == 1
            
            # Оба уведомления должны быть отправлены
            assert len(notifications) == 2
            
            # Проверяем первое уведомление (достаточно)
            notif1 = next(n for n in notifications if n["order_id"] == "order_1")
            assert notif1["will_reposition"] is True
            
            # Проверяем второе уведомление (недостаточно)
            notif2 = next(n for n in notifications if n["order_id"] == "order_2")
            assert notif2["will_reposition"] is False
    
    @pytest.mark.asyncio
    async def test_notification_always_sent(self, mock_user, mock_client):
        """Тест: уведомление отправляется всегда, даже если изменение недостаточно"""
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
            "reposition_threshold_cents": 1.0  # Высокий порог
        }
        
        with patch('sync_orders.get_user', new_callable=AsyncMock) as mock_get_user, \
             patch('sync_orders.get_user_orders', new_callable=AsyncMock) as mock_get_orders, \
             patch('sync_orders.create_client') as mock_create_client, \
             patch('sync_orders.get_current_market_price') as mock_get_price:
            
            mock_get_user.return_value = mock_user
            mock_get_orders.return_value = [db_order]
            mock_create_client.return_value = mock_client
            mock_get_price.return_value = 0.501  # Небольшое изменение
            
            orders_to_cancel, orders_to_place, notifications = await process_user_orders(12345)
            
            # Ордер не переставляется (изменение 0.001 = 0.1 цент < 1.0 цент)
            assert len(orders_to_cancel) == 0
            assert len(orders_to_place) == 0
            
            # Но уведомление должно быть отправлено
            assert len(notifications) == 1
            notification = notifications[0]
            assert notification["order_id"] == "order_notify"
            assert notification["will_reposition"] is False
            assert "target_price_change_cents" in notification
            assert "reposition_threshold_cents" in notification
    
    @pytest.mark.asyncio
    async def test_notification_structure(self, mock_user, mock_client):
        """Тест: проверка структуры уведомления"""
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
            "reposition_threshold_cents": 0.5
        }
        
        with patch('sync_orders.get_user', new_callable=AsyncMock) as mock_get_user, \
             patch('sync_orders.get_user_orders', new_callable=AsyncMock) as mock_get_orders, \
             patch('sync_orders.create_client') as mock_create_client, \
             patch('sync_orders.get_current_market_price') as mock_get_price:
            
            mock_get_user.return_value = mock_user
            mock_get_orders.return_value = [db_order]
            mock_create_client.return_value = mock_client
            mock_get_price.return_value = 0.510
            
            _, _, notifications = await process_user_orders(12345)
            
            assert len(notifications) == 1
            notification = notifications[0]
            
            # Проверяем все обязательные поля
            required_fields = [
                "order_id", "market_id", "token_name", "side",
                "old_current_price", "new_current_price",
                "old_target_price", "new_target_price",
                "price_change", "target_price_change",
                "target_price_change_cents", "reposition_threshold_cents",
                "offset_ticks", "will_reposition"
            ]
            
            for field in required_fields:
                assert field in notification, f"Поле {field} отсутствует в уведомлении"
            
            # Проверяем значения
            assert notification["order_id"] == "order_struct"
            assert notification["market_id"] == 200
            assert notification["token_name"] == "YES"
            assert notification["side"] == "BUY"
            assert notification["old_current_price"] == 0.500
            assert notification["new_current_price"] == 0.510
            assert notification["reposition_threshold_cents"] == 0.5
            assert isinstance(notification["will_reposition"], bool)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

