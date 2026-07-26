# Настройка системы с нуля (SETUP)

Пошаговая инструкция: аккаунты, ключи, сервер, запуск. Рассчитана на
технического исполнителя; пункты для Юлии помечены.

---

## 1. Telegram-бот (BotFather)

1. В Telegram откройте [@BotFather](https://t.me/BotFather) → `/newbot`.
2. Имя: `Юлия Гейкина · помощник` (или согласованное), username вида
   `geikina_assistant_bot`.
3. Сохраните **токен** → `TELEGRAM_BOT_TOKEN` в `.env`.
4. `/setprivacy` → выберите бота → **Disable** (боту нужны сообщения
   в discussion group для комментариев).
5. Узнайте Telegram ID Юлии: пусть Юлия напишет боту
   [@userinfobot](https://t.me/userinfobot) → `TELEGRAM_ADMIN_ID`.

## 2. Канал и группа обсуждений

1. В канале Юлии: Настройки → Обсуждение → создать/привязать группу.
2. Добавьте бота **администратором канала** и **участником группы
   обсуждений**.
3. ID канала и группы: перешлите любой пост канала и любое сообщение
   группы боту [@userinfobot](https://t.me/userinfobot) (или
   [@getidsbot](https://t.me/getidsbot)) →
   `TELEGRAM_CHANNEL_ID` и `TELEGRAM_DISCUSSION_GROUP_ID`
   (оба вида `-100XXXXXXXXXX`).

## 3. OpenAI

1. [platform.openai.com](https://platform.openai.com) → аккаунт для бренда
   (почта Юлии — система принадлежит бренду).
2. Billing → пополнить баланс (10–20 $ хватит на месяцы работы MVP
   на gpt-4o-mini).
3. API keys → Create new secret key → `OPENAI_API_KEY`.
4. Limits → поставить месячный лимит расходов (например, 20 $) — защита
   от неожиданностей.

## 4. Airtable

1. [airtable.com](https://airtable.com) → аккаунт бренда.
2. Создайте базу с 5 таблицами: **Contacts, Touches, Comments, Posts,
   Tasks** — все поля перечислены в модели данных (ТЗ, Часть 3) и
   проверяются скриптом (шаг 6). Быстрый путь: скопировать структуру
   из тестовой базы (Иван передаст ссылку-шаблон).
3. [airtable.com/create/tokens](https://airtable.com/create/tokens) →
   Personal Access Token со scopes `data.records:read`,
   `data.records:write`, `schema.bases:read` и доступом к этой базе →
   `AIRTABLE_API_KEY`.
4. ID базы (`appXXXXXXXXXXXXXX`) — из URL базы → `AIRTABLE_BASE_ID`.

## 5. VPS (Ubuntu 22.04+)

```bash
sudo apt update && sudo apt install -y python3.11 python3.11-venv git nginx certbot python3-certbot-nginx
sudo adduser yulia
su - yulia
git clone <репозиторий> geikina-bot && cd geikina-bot
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env   # заполнить все переменные
```

Webhook требует HTTPS. Домен (например `bot.example.com`) → A-запись на
IP сервера, затем:

```bash
sudo certbot --nginx -d bot.example.com
```

Nginx-проксирование на бота (порт 8080):

```nginx
location /webhook { proxy_pass http://127.0.0.1:8080/webhook; }
```

`WEBHOOK_URL=https://bot.example.com` в `.env`.

## 6. Проверка и запуск

```bash
# Структура CRM на месте?
.venv/bin/python -m scripts.setup_airtable --check

# Тесты
.venv/bin/python -m pytest tests/

# systemd
sudo cp systemd/geikina-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now geikina-bot
sudo systemctl status geikina-bot
```

## 7. Cron

`crontab -e` под пользователем yulia (время в UTC; МСК = UTC+3):

```cron
# Таймауты диалогов — каждый час
0 * * * * cd /home/yulia/geikina-bot && .venv/bin/python -m scripts.check_timeouts

# Еженедельный отчёт — понедельник 10:00 МСК
0 7 * * 1 cd /home/yulia/geikina-bot && .venv/bin/python -m scripts.weekly_report

# Бэкап — воскресенье 03:00 МСК
0 0 * * 0 cd /home/yulia/geikina-bot && bash scripts/backup.sh
```

## 8. Живая проверка после деплоя (чек-лист)

- [ ] `/health` от Юлии — все строки ✅
- [ ] `/start` с другого аккаунта → приветствие с представлением
      AI-помощником
- [ ] Диалог по сценарию B (описать проблему) → вопросы по одному,
      карточка Юлии при передаче
- [ ] «Сколько стоит диагностика?» → сценарий A, два вопроса, передача,
      цена 15 000 ₽ с оговоркой
- [ ] Комментарий под постом канала → запись в Comments, уведомление
      при потенциальном клиенте, публикация ответа по кнопке
- [ ] Кнопки карточки: Беру / Прогрев / Нецелевой / Пауза
- [ ] `bash scripts/backup.sh` вручную → архив + уведомление
- [ ] Дедупликация: 3 комментария + личное сообщение → 1 запись в Contacts
- [ ] Прогнать примеры из таблицы сценариев ТЗ (Блок 4) на живой модели

## 9. Перенос на аккаунты Юлии (передача системы)

1. Airtable: создать базу в аккаунте Юлии (шаг 4), перенести данные
   CSV-экспортом (`python -m bot.services.backup --output data/`),
   поменять `AIRTABLE_API_KEY`/`AIRTABLE_BASE_ID` в `.env`, рестарт.
2. OpenAI: ключ из аккаунта бренда (шаг 3).
3. BotFather: передать владение ботом аккаунту Юлии
   (`/setowner` недоступен — бот создаётся заново ИЛИ передаётся
   вместе с Telegram-аккаунтом; согласовать).
4. Все пароли/токены — в менеджер паролей Юлии; из личных аккаунтов
   исполнителя доступы удаляются (Конституция, п. 10).
