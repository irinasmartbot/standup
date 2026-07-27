# VK MVP

Цель первого этапа: запустить отдельного VK-бота на тестовом сообществе, не меняя запуск Telegram-бота.

## Что нужно в VK

1. Тестовое VK-сообщество.
2. Включенные сообщения сообщества.
3. Включенный Bots Long Poll API для сообщества.
4. Токен сообщества с правами на сообщения.
5. ID сообщества без минуса.
6. `peer_id` тестового получателя/админа для загрузки системных картинок.

## Env

```env
VK_ENABLED=1
VK_GROUP_ID=123456789
VK_GROUP_TOKEN=vk1.a....
VK_API_VERSION=5.199
VK_ADMIN_PEER_ID=92721078
VK_MANAGER_LINK=https://vk.com/...
VK_COMMUNITY_LINK=https://vk.com/...
VK_SYSTEM_IMAGES_CACHE=data/storage/vk_system_images.json
```

Telegram-переменные остаются как есть. `main.py` продолжает запускать только Telegram-бота.

Для VK обязательны `DATABASE_URL` и `EVENTS_SOURCE=postgres`. Google Sheets для афиши VK не используется.

## Первый тестовый запуск

```bash
python vk_bot.py
```

## Предзагрузка картинок

В этом проекте нет `docker compose` и `npm`, поэтому вместо команды из другого проекта используется Python-скрипт:

```bash
python scripts/upload_vk_system_images.py --peer-id 92721078
```

Скрипт загрузит системные картинки в VK и сохранит готовые attachment id в `VK_SYSTEM_IMAGES_CACHE`.

## Афиша Хитлото

Файл `фото/photo_2026-07-21_01-59-43.jpg` — актуальная афиша музыкального лото для VK.

Чтобы заменить стандартную картинку `hitloto_start` этой афишей, загрузите ее отдельной командой:

```bash
python scripts/upload_vk_system_images.py --peer-id 92721078 --image hitloto_start=фото/photo_2026-07-21_01-59-43.jpg
```

После загрузки скрипт обновит `data/storage/vk_system_images.json`, и VK-бот сможет отправлять эту афишу по ключу `hitloto_start`.

## Выкат тестового VK на сервер

Важно: автодеплой `dev` всегда делает `reset --hard origin/dev` в `/home/standup/app` и перезапускает только Telegram (`standup-bot`).  
Поэтому **нельзя** просто переключить эту папку на `vk-mvp` — сломается TG.

Рекомендуемый вариант для теста: отдельная копия репозитория под VK.

### 1. Push ветки (с ноутбука)

```bash
git push -u origin vk-mvp
```

### 2. На сервере — отдельная папка

Быстрый вариант (рекомендуется):

```bash
cd /home/standup
# после git pull vk-mvp в локальной копии или:
curl -fsSL https://raw.githubusercontent.com/irinasmartbot/standup/vk-mvp/scripts/bootstrap_vk_server.sh -o /tmp/bootstrap_vk_server.sh
bash /tmp/bootstrap_vk_server.sh
```

Или вручную:

```bash
git clone https://github.com/irinasmartbot/standup.git /home/standup/vk-app
cd /home/standup/vk-app
git checkout vk-mvp
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp /home/standup/app/.env /home/standup/vk-app/.env
# затем дописать VK_ENABLED=1, VK_GROUP_ID, VK_GROUP_TOKEN, VK_ADMIN_PEER_ID, ...
```

### 3. Картинки

```bash
cd /home/standup/vk-app
./venv/bin/python scripts/upload_vk_system_images.py --peer-id <VK_ADMIN_PEER_ID>
./venv/bin/python scripts/upload_vk_system_images.py --peer-id <VK_ADMIN_PEER_ID> --image hitloto_start=фото/photo_2026-07-21_01-59-43.jpg
```

### 4. systemd

Пример unit: `deploy/standup-vk-bot.service`

```bash
sudo cp /home/standup/vk-app/deploy/standup-vk-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now standup-vk-bot
sudo systemctl status standup-vk-bot
sudo journalctl -u standup-vk-bot -n 80 --no-pager
```

Обновление кода VK после новых коммитов в `vk-mvp`:

```bash
cd /home/standup/vk-app
git fetch origin vk-mvp
git reset --hard origin/vk-mvp
./venv/bin/pip install -r requirements.txt
sudo systemctl restart standup-vk-bot
```

Telegram (`/home/standup/app` + `standup-bot`) при этом не трогаем.

## Что уже в MVP (код)

- меню как в TG (с лимитами кнопок VK)
- BEST / Хитлото / Проверка (просмотр афиши из Postgres)
- бронь Проверки: имя → телефон (ручной ввод) → гости → БД `source=vkontakte`
- «Получить билет» картинкой через upload в VK
- analytics `channel=vkontakte`

## Следующие шаги после первого серверного теста

1. Отмена / изменение брони в VK
2. Напоминания для VK-броней
3. «Мои брони» (команды — место размещения решим отдельно)
4. Когда стабильно — аккуратный merge `vk-mvp` → `dev` (не раньше)
