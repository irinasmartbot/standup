# VK entry landings

Публичные ссылки запуска VK-бота на домене `go.moscowstandupshow.ru`  
(без логина админки; `/admin` по-прежнему за basic auth).

## Ссылки

```text
https://go.moscowstandupshow.ru/vk/booking
https://go.moscowstandupshow.ru/vk/raffle
https://go.moscowstandupshow.ru/vk/offline-gift
```

## Как работает

1. QR / ссылка → публичная страница.
2. Виджет VK «Разрешить сообщения от сообщества» → страница получает `vk_id`.
3. Кнопка на странице → `POST /vk/entry`.
4. Backend шлёт человеку сообщение с inline-кнопкой:
   - booking → `{"cmd":"book"}`
   - raffle → `{"cmd":"raffle"}`
   - offline_gift → `{"cmd":"offline_gift"}`
5. VK-бот обрабатывает `cmd` как обычно.

## Код

- `bot/admin/vk_entry.py` — страницы + API
- Роуты регистрируются в `bot/admin/app.py` → `create_app()`

Веб крутится процессом `standup-admin` (`/home/standup/app`, ветка **dev**).  
VK-бот — отдельно (`/home/standup/vk-app`, ветка **vk-mvp**).

## Env (в `.env` админки на VPS)

```env
VK_GROUP_ID=...
VK_GROUP_TOKEN=...
VK_OPENAPI_APP_ID=...
VK_PUBLIC_ENTRY_BASE=https://go.moscowstandupshow.ru
```

`VK_GROUP_*` обычно уже есть (те же, что у VK-бота).

## VK Developers (разово)

1. [dev.vk.com](https://dev.vk.com) → Мои приложения → Создать → тип **Сайт** / веб.
2. Базовый домен: `go.moscowstandupshow.ru` (без `https://`).
3. Скопировать **ID приложения** → `VK_OPENAPI_APP_ID`.
4. В настройках сообщества: сообщения сообщества включены; бот/приложение может писать пользователям, которые разрешили сообщения.

## Деплой

1. Код лендов должен оказаться в **dev** (автодеплой `standup-admin`).
2. На сервере дописать `VK_OPENAPI_APP_ID` и `VK_PUBLIC_ENTRY_BASE` в `/home/standup/app/.env`.
3. `sudo systemctl restart standup-admin`
4. Проверить три URL выше.

Пока нет `VK_OPENAPI_APP_ID`, страницы показывают «ещё настраивается» — админка не ломается.
