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
