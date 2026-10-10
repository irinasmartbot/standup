# Telegram API relay

Статус: реализовано локально в ветке `feat/telegram-api-relay`, требует PR, deploy и production verification.

## Назначение

Relay нужен, чтобы сервер бота обращался к `https://api.telegram.org:443` через зарубежный VPS, не перенося бота, базу и файлы с текущего сервера проекта.

Туннель не является `HTTPS_PROXY` или SOCKS-прокси. Приложение сохраняет URL, Host, SNI и проверку сертификата Telegram. Остальные внешние сервисы продолжают использовать прямое подключение.

## Настройки приложения

Пустой `TELEGRAM_RELAY_HOST` означает прямое подключение к Telegram:

```env
TELEGRAM_RELAY_HOST=
TELEGRAM_RELAY_PORT=18443
TELEGRAM_RELAY_LIMIT=4
```

Включение relay на сервере бота:

```env
TELEGRAM_RELAY_HOST=127.0.0.1
TELEGRAM_RELAY_PORT=18443
TELEGRAM_RELAY_LIMIT=4
```

`TELEGRAM_RELAY_LIMIT` задает лимит одновременных Telegram API соединений в HTTP-клиенте приложения. Начальное production-значение: `4`.

## Туннель на сервере бота

Приватный ключ хранится только на сервере бота:

```text
/home/standup/.ssh/moscow_standup_tg_api_relay_185_221_196_110
```

Отдельный `known_hosts` для заранее сверенного host key:

```text
/home/standup/.ssh/telegram_relay_known_hosts
```

Проверка туннеля:

```bash
curl --connect-timeout 5 --max-time 15 \
  --connect-to api.telegram.org:443:127.0.0.1:18443 \
  -I https://api.telegram.org/
```

Ожидается HTTP-ответ Telegram, например `HTTP/2 302`, без отключения проверки сертификата.

## systemd

Сервис на сервере бота:

```text
telegram-api-relay.service
```

Основные требования к unit:

- `Restart=always`, `RestartSec=5`;
- `ExitOnForwardFailure=yes`;
- `ServerAliveInterval=15`, `ServerAliveCountMax=3`;
- `MemoryMax=48M`, `CPUQuota=10%`, `TasksMax=8`;
- `-L 127.0.0.1:18443:api.telegram.org:443`;
- строгая проверка host key через отдельный `UserKnownHostsFile`.

Проверка:

```bash
sudo systemctl status telegram-api-relay.service --no-pager -l
sudo systemctl restart telegram-api-relay.service
curl --connect-timeout 5 --max-time 15 --connect-to api.telegram.org:443:127.0.0.1:18443 -I https://api.telegram.org/
```

## Включение после deploy

1. Проверить, что `telegram-api-relay.service` активен.
2. Добавить в `/home/standup/app/.env`:

   ```env
   TELEGRAM_RELAY_HOST=127.0.0.1
   TELEGRAM_RELAY_PORT=18443
   TELEGRAM_RELAY_LIMIT=4
   ```

3. Перезапустить только нужные сервисы приложения.
4. Проверить `getMe`, `getWebhookInfo`, меню владельца, получение и отправку одного изображения.
5. Проверить логи на отсутствие новых сетевых ошибок Telegram.

## Отключение

1. Вернуть прямой транспорт в `/home/standup/app/.env`:

   ```env
   TELEGRAM_RELAY_HOST=
   ```

2. Перезапустить приложение.
3. Проверить меню владельца и логи.
4. Только после этого останавливать туннель:

   ```bash
   sudo systemctl stop telegram-api-relay.service
   ```

Сообщения, база и файлы не переносятся и не переигрываются.

## Фактическая проверка 2026-10-10

На сервере бота `31.128.47.4` создан и запущен `telegram-api-relay.service`.

Проверено:

- `curl --connect-to api.telegram.org:443:127.0.0.1:18443 -I https://api.telegram.org/` вернул `HTTP/2 302`;
- после `sudo systemctl restart telegram-api-relay.service` тот же curl снова вернул `HTTP/2 302`;
- в status сервиса было видно `Memory: 1.4M` при лимите `48.0M`.

Не проверено в рамках этой записи:

- приложение с `TELEGRAM_RELAY_HOST=127.0.0.1`;
- реальные `getMe` / `getWebhookInfo`;
- меню владельца и отправка изображения;
- влияние на зарубежный VPS и сервисы приюта.
