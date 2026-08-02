# VK entry landings (упрощённый вход)

Публичные ссылки запуска VK-бота на домене `go.moscowstandupshow.ru`  
(без логина админки; `/admin` по-прежнему за basic auth).

**Без OpenAPI-виджета и без ИП** — кнопка открывает диалог сообщества с `ref`.

## Ссылки

```text
https://go.moscowstandupshow.ru/vk/booking
https://go.moscowstandupshow.ru/vk/raffle
https://go.moscowstandupshow.ru/vk/offline-gift
```

## Как работает

1. QR / ссылка → публичная страница на `go…`.
2. Кнопка → `https://vk.com/write-{GROUP_ID}?ref=…` (или `vk.me/{screen_name}?ref=…`).
3. Пользователь в VK нажимает «Начать».
4. Бот получает `command=start` + `ref` и открывает нужную ветку:
   - `standup_book` → бронь
   - `standup_rozygr` → розыгрыш
   - `offline_gift` → офлайн-подарок

Важно: использовать именно `write-` / `vk.me`, не `vk.com/club…` — иначе `ref` часто не доходит.

## Код

- `bot/admin/vk_entry.py` — публичные страницы (сервит **standup-admin**, ветка **dev**)
- `bot/vk/app.py` — разбор `ref` (VK-бот, ветка **vk-mvp**)

## Env

На админке (`/home/standup/app/.env`) достаточно того же, что у VK-бота:

```env
VK_GROUP_ID=...
VK_COMMUNITY_LINK=https://vk.com/...   # запасной вариант, если нет GROUP_ID
```

`VK_OPENAPI_APP_ID` для этого режима **не нужен**.

## Деплой

1. Лендинги → ветка **dev** (автодеплой `standup-admin`).
2. Обработка `ref` брони → ветка **vk-mvp**, ручной деплой VK-бота:
   ```bash
   ssh standup@31.128.47.4
   cd /home/standup/vk-app && git pull && sudo systemctl restart standup-vk
   ```
3. Проверить три URL выше → «Начать» → нужная ветка бота.

## Позже (опционально)

Если появится ИП / приложение типа «Сайт» — можно вернуть OpenAPI-виджет «Разрешить сообщения» без обязательного «Начать».
