#!/bin/bash
# Полный бэкап системы (ТЗ, Блок 10). Ручной запуск одной командой:
#   bash scripts/backup.sh
# Автоматически: cron, воскресенье 03:00 МСК (00:00 UTC):
#   0 0 * * 0 cd /home/yulia/geikina-bot && bash scripts/backup.sh
# Состав: код · база знаний · промпты · .env · данные Airtable (CSV) ·
# структура CRM · логи за 7 дней. Хранение: 8 последних копий (~2 месяца).
set -e

PROJECT_DIR="${PROJECT_DIR:-/home/yulia/geikina-bot}"
BACKUP_ROOT="${BACKUP_ROOT:-/home/yulia/backups}"
PYTHON="${PYTHON:-$PROJECT_DIR/.venv/bin/python}"

DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="$BACKUP_ROOT/$DATE"

# При любой ошибке — уведомление Юлии с текстом (ТЗ, Блок 10)
notify_fail() {
    cd "$PROJECT_DIR" && "$PYTHON" -m scripts.notify_backup --fail "шаг: $CURRENT_STEP" || true
}
trap notify_fail ERR
CURRENT_STEP="подготовка"

mkdir -p "$BACKUP_DIR"
cd "$PROJECT_DIR"

# 1. Код проекта
CURRENT_STEP="копирование кода"
cp -r "$PROJECT_DIR" "$BACKUP_DIR/code"
rm -rf "$BACKUP_DIR/code/logs" "$BACKUP_DIR/code/.git" "$BACKUP_DIR/code/.venv"

# 2. База знаний (отдельно, для быстрого доступа)
CURRENT_STEP="база знаний"
cp -r "$PROJECT_DIR/bot/knowledge" "$BACKUP_DIR/knowledge"

# 3. Промпты (отдельно)
CURRENT_STEP="промпты"
cp -r "$PROJECT_DIR/bot/prompts" "$BACKUP_DIR/prompts"

# 4. Конфигурация
CURRENT_STEP="конфигурация"
cp "$PROJECT_DIR/.env" "$BACKUP_DIR/.env"

# 5. Данные Airtable (все таблицы в CSV)
CURRENT_STEP="экспорт данных Airtable"
"$PYTHON" -m bot.services.backup --output "$BACKUP_DIR/data"

# 6. Структура CRM
CURRENT_STEP="схема Airtable"
"$PYTHON" -m scripts.setup_airtable --dump-schema > "$BACKUP_DIR/airtable_schema.json"

# 7. Логи за последние 7 дней
CURRENT_STEP="логи"
mkdir -p "$BACKUP_DIR/logs"
find "$PROJECT_DIR/logs" -mtime -7 -type f -exec cp {} "$BACKUP_DIR/logs/" \; 2>/dev/null || true

# 8. Архивация
CURRENT_STEP="архивация"
tar -czf "$BACKUP_ROOT/backup_$DATE.tar.gz" -C "$BACKUP_ROOT" "$DATE"
rm -rf "$BACKUP_DIR"

# 9. Ротация: хранить последние 8
CURRENT_STEP="ротация"
ls -1t "$BACKUP_ROOT"/backup_*.tar.gz | tail -n +9 | xargs -r rm

# 10. Уведомление Юлии (ТЗ: «Резервная копия создана: 25.07, размер 4.2 МБ»)
CURRENT_STEP="уведомление"
"$PYTHON" -m scripts.notify_backup --ok "$BACKUP_ROOT/backup_$DATE.tar.gz" || true

echo "✅ Бэкап завершён: $BACKUP_ROOT/backup_$DATE.tar.gz"
