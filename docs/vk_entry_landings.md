# VK entry landings (план, не реализовано)

Зафиксировано 2026-07-28. Реализацию отложили: сначала offline-gift + правки админки.

## Зачем

У VK нет надёжного deep link как у Telegram (`?start=`).  
`vk.com/write-...?ref=` хрупкий: нужен «Начать» / сообщение, `ref` может «липнуть».

Нужны **3 публичные ссылки запуска** на одном домене (не внутри UI админки):

1. Воронка бронирования  
2. Розыгрыш со скринами  
3. Офлайн-розыгрыш подарка (подписка → список участников)

## Домен (тест)

Админка уже на:

```text
https://standupadmin.duckdns.org/admin
```

Публичные ленды на **том же домене**, отдельные пути, **без логина**:

```text
https://standupadmin.duckdns.org/vk/booking
https://standupadmin.duckdns.org/vk/raffle
https://standupadmin.duckdns.org/vk/offline-gift
```

Боевой красивый поддомен позже (например `go.moscowstandupshow.ru`) — не блокер.

## Как работает (целевая схема)

1. QR / ссылка → публичная страница.
2. На странице: виджет VK «Разрешить сообщения от сообщества» + кнопка «Продолжить» / «Участвовать».
3. Страница получает `vk_id`.
4. Backend `POST /vk/entry` шлёт человеку VK-сообщение с кнопкой payload:
   - booking → `{"cmd":"book"}`
   - raffle → `{"cmd":"raffle"}`
   - offline_gift → `{"cmd":"offline_gift"}` (без event_id в URL)
5. VK-бот уже умеет ветки по `cmd`.

## Offline-gift без привязки к шоу в QR

Одна универсальная ссылка:

```text
https://standupadmin.duckdns.org/vk/offline-gift
```

Логика (уже частично в коде VK/TG, без лендинга):

- сегодня **1** шоу → сразу участие / проверка подписки;
- сегодня **несколько** шоу → человек выбирает шоу (независимо от совпадения времени).

## Что нужно для лендингов

- `VK_OPENAPI_APP_ID` — VK Developers → Мои приложения → тип **Сайт**, домен `standupadmin.duckdns.org`
- В `.env` веб-сервиса (тот, что крутит админку):

```env
VK_OPENAPI_APP_ID=...
VK_PUBLIC_ENTRY_BASE=https://standupadmin.duckdns.org
```

Плюс уже есть / нужны: `VK_GROUP_ID`, `VK_GROUP_TOKEN`, `VK_COMMUNITY_LINK`.

## Где код

Не «вкладки админки», а **тот же aiohttp** (`bot/admin/app.py`), публичные роуты рядом с `/admin`:

- `GET /vk/booking`
- `GET /vk/raffle`
- `GET /vk/offline-gift`
- `POST /vk/entry`

`/admin` остаётся закрытым.

## Уже сделано отдельно (offline gift MVP)

- Таблица `vk_offline_gift_entries`
- VK: слово `подарок` / `чек-лист`, выбор шоу если несколько, подписка, запись
- TG ведущий: `https://t.me/ira_test_stend_bot?start=chek_list`  
  (бой: `https://telegram.me/StandUp_Show_bot?start=chek_list`)

## Когда вернуться

После правок админ-панели: OpenAPI app → публичные роуты → `POST /vk/entry` → деплой веб + VK-бот.
