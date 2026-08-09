# VK entry landings (виджет → сразу сообщение)

Публичные ссылки на `go.moscowstandupshow.ru` без логина админки.

## Ссылки

```text
https://go.moscowstandupshow.ru/vk/booking
https://go.moscowstandupshow.ru/vk/raffle
https://go.moscowstandupshow.ru/vk/offline-gift
```

Mini App (канонические ссылки — внутри VK):

```text
https://vk.com/app54704296_-225298932#flow=booking
https://vk.com/app54704296_-225298932#flow=raffle
https://vk.com/app54704296_-225298932#flow=offline_gift
```

`225298932` — `VK_GROUP_ID` сообщества, к которому привязан mini app.  
`54704296` — `VK_MINI_APP_ID`.

В кабинете VK Mini App поле **URL приложения** должно быть ровно:

```text
https://go.moscowstandupshow.ru/vk-mini
```

Не `.../vk-mini/start/booking` — иначе все `#flow=` ссылки открывают бронирование.

Служебные URL на `go…` (редиректят в VK с `#flow=`):

```text
https://go.moscowstandupshow.ru/vk-mini/start/booking
https://go.moscowstandupshow.ru/vk-mini/start/raffle
https://go.moscowstandupshow.ru/vk-mini/start/offline_gift
```

## Как работает

1. Страница грузит OpenAPI-виджет «Разрешить сообщения от сообщества».
2. Пользователь жмёт «Разрешить» → страница получает `vk_id`.
3. Сразу `POST /vk/entry` → сообщество пишет в личку кнопку нужной ветки.
4. VK-бот обрабатывает `cmd` (`book` / `raffle` / `offline_gift`).

Писать «начать» не нужно.

## Env (админка `/home/standup/app/.env`)

```env
VK_GROUP_ID=...
VK_GROUP_TOKEN=...
VK_OPENAPI_APP_ID=54704100
```

`VK_GROUP_*` — то же тестовое/боевое сообщество, от имени которого пишем.
`VK_OPENAPI_APP_ID` — ID Web-приложения с базовым доменом `go.moscowstandupshow.ru`.

Для Mini App:

```env
VK_MINI_APP_ID=54704296
VK_MINI_APP_SECRET=...
```

`VK_MINI_APP_SECRET` — защищённый ключ из кабинета VK Mini Apps, не коммитить.

## Деплой

Код лендингов — ветка **dev** (`standup-admin`).  
Обработка кнопок в чате — ветка **vk-mvp** (`standup-vk-bot`).

## Профиль VK ID

Баннер «Подтвердите профиль за 59 дней» — реальный: без подтверждения доступ к приложению могут ограничить. Для боя приложение лучше завести на профиле клиента и подтвердить бизнес.
