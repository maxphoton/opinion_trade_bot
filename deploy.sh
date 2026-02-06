#!/bin/bash

# ============================================================================
# Деплой скрипт для trade_bot
# Загружает файлы на сервер и запускает Docker контейнер
# ============================================================================

set -e  # Остановка при ошибке

# ============================================================================
# НАСТРОЙКИ СЕРВЕРА (измените на свои значения)
# ============================================================================

# Адрес сервера (IP или домен)
SERVER_HOST="90.156.208.239"

# Пользователь для SSH подключения
SERVER_USER="root"

# Путь к папке на сервере, куда загружать проект
SERVER_PATH="/root/opinion_bot_beta"

# Порт SSH (по умолчанию 22)
SSH_PORT="22"

# ============================================================================
# ФУНКЦИИ
# ============================================================================

# Цветной вывод
print_info() {
    echo -e "\033[0;32m[INFO]\033[0m $1"
}

print_error() {
    echo -e "\033[0;31m[ERROR]\033[0m $1"
}

print_warning() {
    echo -e "\033[0;33m[WARNING]\033[0m $1"
}

# Проверка подключения к серверу
check_connection() {
    print_info "Проверка подключения к серверу..."
    print_warning "Введите пароль для $SERVER_USER@$SERVER_HOST:$SSH_PORT (если потребуется)"
    # Убираем BatchMode=yes, чтобы разрешить интерактивный ввод пароля
    if ssh -p "$SSH_PORT" -o ConnectTimeout=10 -o PreferredAuthentications=keyboard-interactive,password "$SERVER_USER@$SERVER_HOST" exit; then
        print_info "Подключение успешно"
        return 0
    else
        print_error "Не удалось подключиться к серверу $SERVER_USER@$SERVER_HOST:$SSH_PORT"
        print_error "Убедитесь, что:"
        print_error "  1. Сервер доступен"
        print_error "  2. Правильно указаны SERVER_HOST, SERVER_USER, SSH_PORT"
        print_error "  3. Пароль введен корректно"
        exit 1
    fi
}

# Создание папки на сервере, если её нет
create_server_directory() {
    print_info "Создание папки на сервере (если не существует)..."
    print_warning "Может потребоваться ввод пароля"
    ssh -p "$SSH_PORT" "$SERVER_USER@$SERVER_HOST" "mkdir -p $SERVER_PATH"
    print_info "Папка создана/проверена"
}

# Загрузка файлов на сервер
upload_files() {
    print_info "Загрузка файлов на сервер..."
    print_warning "Может потребоваться ввод пароля"
    
    # Исключаем ненужные файлы и папки
    # Используем опции SSH для интерактивного ввода пароля
    rsync -avz --progress \
        -e "ssh -p $SSH_PORT -o PreferredAuthentications=keyboard-interactive,password" \
        --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.pytest_cache' \
        --exclude='venv' \
        --exclude='.venv' \
        --exclude='.env' \
        --exclude='*.db' \
        --exclude='bot/__pycache__' \
        --exclude='develop' \
        --exclude='simple_flow.py' \
        --exclude='tests' \
        --exclude='pytest.ini' \
        --exclude='logs' \
        --exclude='*.log' \
        --exclude='deploy.sh' \
        ./ "$SERVER_USER@$SERVER_HOST:$SERVER_PATH/"
    
    print_info "Файлы загружены"
}

# Остановка существующего контейнера
stop_container() {
    print_info "Остановка существующего контейнера (если запущен)..."
    ssh -p "$SSH_PORT" -o PreferredAuthentications=keyboard-interactive,password "$SERVER_USER@$SERVER_HOST" "cd $SERVER_PATH && docker-compose down 2>/dev/null || true"
    print_info "Контейнер остановлен (если был запущен)"
}

# Сборка и запуск Docker контейнера
start_container() {
    print_info "Сборка и запуск Docker контейнера..."
    ssh -p "$SSH_PORT" -o PreferredAuthentications=keyboard-interactive,password "$SERVER_USER@$SERVER_HOST" "cd $SERVER_PATH && docker-compose up -d --build"
    print_info "Контейнер запущен"
}

# Проверка статуса контейнера
check_container_status() {
    print_info "Проверка статуса контейнера..."
    ssh -p "$SSH_PORT" -o PreferredAuthentications=keyboard-interactive,password "$SERVER_USER@$SERVER_HOST" "cd $SERVER_PATH && docker-compose ps"
}

# Просмотр логов
show_logs() {
    print_info "Последние 50 строк логов контейнера:"
    ssh -p "$SSH_PORT" -o PreferredAuthentications=keyboard-interactive,password "$SERVER_USER@$SERVER_HOST" "cd $SERVER_PATH && docker-compose logs --tail=50 bot"
}

# ============================================================================
# ОСНОВНАЯ ЛОГИКА
# ============================================================================

main() {
    print_info "Начало деплоя trade_bot"
    print_info "Сервер: $SERVER_USER@$SERVER_HOST:$SSH_PORT"
    print_info "Путь: $SERVER_PATH"
    echo ""
    
    # Проверка подключения
    check_connection
    
    # Создание папки
    create_server_directory
    
    # Загрузка файлов
    upload_files
    
    # Остановка старого контейнера
    stop_container
    
    # Запуск нового контейнера
    start_container
    
    # Небольшая задержка для запуска
    sleep 2
    
    # Проверка статуса
    check_container_status
    
    echo ""
    print_info "Деплой завершен!"
    print_info "Для просмотра логов выполните:"
    print_info "  ssh -p $SSH_PORT $SERVER_USER@$SERVER_HOST 'cd $SERVER_PATH && docker-compose logs -f bot'"
}

# ============================================================================
# ОБРАБОТКА АРГУМЕНТОВ
# ============================================================================

case "${1:-deploy}" in
    deploy)
        main
        ;;
    logs)
        show_logs
        ;;
    status)
        check_container_status
        ;;
    stop)
        stop_container
        ;;
    restart)
        stop_container
        sleep 2
        start_container
        check_container_status
        ;;
    *)
        echo "Использование: $0 [команда]"
        echo ""
        echo "Команды:"
        echo "  deploy   - Полный деплой (по умолчанию)"
        echo "  logs     - Показать логи контейнера"
        echo "  status   - Показать статус контейнера"
        echo "  stop     - Остановить контейнер"
        echo "  restart  - Перезапустить контейнер"
        exit 1
        ;;
esac

