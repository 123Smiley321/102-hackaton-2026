# Публикация в MAX

## Что реализовано

Backend публикует согласованную запись через официальный MAX Bot API:

- `POST https://platform-api2.max.ru/messages?chat_id=<int64>`;
- токен передаётся только в заголовке `Authorization: <access_token>`, без `Bearer` и без query-параметра;
- текст передаётся в JSON-поле `text`;
- изображение передаётся как официальный attachment `{"type":"image","payload":{"url":"https://…"}}`;
- успешным результатом считается только ответ с реальным `message.message.mid`; он сохраняется в `published_post_id`, а `message.url`, если MAX его вернул, — в `published_url`.

Фейкового `published`, синтетического message ID и автоматического повторного POST после неоднозначного сбоя нет. Двухшаговая загрузка через `/uploads` для текущего MVP не требуется: MAX официально поддерживает изображение по прямой URL-ссылке. Backend перед отправкой применяет к ней существующую HTTPS/SSRF-проверку.

Официальные источники:

- [MAX Bot API: авторизация, домен и коды ответов](https://dev.max.ru/docs-api/);
- [POST /messages](https://dev.max.ru/docs-api/methods/POST/messages/);
- [Отправка изображения по URL](https://dev.max.ru/docs-api/use-cases/sending-messages/media/);
- [Создание и модерация бота](https://dev.max.ru/docs/chatbots/bots-create/create/).

## Доступ и честный статус проверки

По официальной документации подключение доступно юрлицам, ИП и самозанятым — резидентам РФ после верификации профиля; бот также проходит модерацию. Для запросов рекомендуется `platform-api2.max.ru` и добавление сертификата Минцифры в доверенные.

**Статус живого E2E: `live_not_verified`.** Команда не является юрлицом и на момент хакатона не имела токена MAX. Поэтому команда реализовала рабочий адаптер по официальному API и проверила его статическими и unit/mock-тестами, но живую публикацию и полный MAX E2E не выполняла и не заявляет их как пройденные.

## Миграция существующей БД

После `migration_task17_18_security_publishing.sql` один раз выполнить:

```bash
docker compose exec -T db psql -U postgres -d marketer \
  < backend/migration_add_max_publisher.sql
```

Миграция добавляет `companies.max_chat_id` и `companies.max_access_token`. Для новой БД поля создаются через модель SQLAlchemy, но для существующей `create_all` недостаточно.

## Настройка компании

MAX token не добавляется в `.env`, Compose, Git или worker. Он сохраняется через admin API и шифруется Fernet вместе с другими секретами площадок:

```http
PATCH /api/companies/1
X-Admin-Key: <ADMIN_API_KEY>
Content-Type: application/json

{
  "enabled_platforms": ["vk", "telegram", "max"],
  "max_chat_id": "-123456789",
  "max_access_token": "<токен, полученный после верификации и модерации бота>"
}
```

`max_chat_id` — числовой ID чата или канала в виде строки; Backend проверяет, что это int64. В `CompanyOut` возвращается только `max_chat_id`, токен не сериализуется. Отзыв доступа:

```json
{"platform":"max"}
```

для `POST /api/companies/{id}/credentials/revoke`. Он очищает MAX token/chat ID только выбранной компании и убирает `max` из `enabled_platforms`.

## Статусы и ручное разрешение

- `not_configured` + HTTP 422: не заданы token/chat ID, chat ID невалиден или секрет не расшифровывается. Внешний POST не выполнялся.
- `publish_failed` + HTTP 422: MAX вернул определённый отказ HTTP 4xx либо `success:false`.
- `publish_unknown` + HTTP 502: timeout, обрыв соединения, 5xx, невалидный JSON или HTTP 200 без подтверждающего `message.message.mid`. Автоматический повтор запрещён.
- `published` + HTTP 200: получен реальный `mid`; повторный вызов возвращает `already_published` без нового внешнего POST.

После исправления конфигурации или материала `not_configured`/`publish_failed` возвращается в очередь осознанно:

```http
POST /api/publications/1/resolve
X-Admin-Key: <ADMIN_API_KEY>
Content-Type: application/json

{"action":"retry"}
```

Для `publish_unknown` прежнее строгое правило сохраняется: либо `mark_published` с реальным внешним ID после сверки, либо `{"action":"retry","confirmed_not_published":true}` только после подтверждения, что поста нет.

## Проверки без реального токена

```bash
python -m pip install -r backend/requirements-dev.txt
PYTHONPATH=backend python -m unittest discover -s backend/tests -v
python -m unittest discover -s publications/tests -v
```

Тесты используют `httpx.MockTransport` и изолированную SQLite БД. Проверяются request/response contract, текст, image attachment, реальный `mid`, отсутствие сетевого вызова без конфигурации, redaction токена, 4xx/5xx, transport failure, отсутствие inline retry, терминальные статусы, ручной requeue и revoke. Это не заменяет живой MAX E2E.
