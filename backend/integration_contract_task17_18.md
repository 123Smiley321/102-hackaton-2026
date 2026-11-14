# Контракт интеграции для Данила: Backend №17–18

## Публикация
Backend остаётся единственной точкой вызова API площадок. Worker Данила:

1. Выбирает только `publications.status = 'approved'` и учитывает `scheduled_at`.
2. Вызывает `POST http://backend:8000/internal/publications/{publication_id}/publish`.
3. Передаёт `X-Publisher-Key: ${PUBLISHER_INTERNAL_KEY}`.
4. Не вызывает VK/Telegram/MAX API самостоятельно и не переводит строку в `published` самостоятельно. Backend запишет результат и audit log.
5. При HTTP 200 и `status=published` считает задачу завершённой. `already_published` — также завершённая задача.
6. При HTTP 409 не пытается обходить проверку статуса/времени.
7. При HTTP 502 не повторяет отправку автоматически: публикация переведена в `publish_unknown`, требуется ручная сверка.
8. При HTTP 422 и `status=not_configured`/`publish_failed` не повторяет отправку: Backend уже снял запись с approved-очереди до ручного `resolve`.

Пример запроса:

```bash
curl -X POST "http://backend:8000/internal/publications/123/publish" \
  -H "X-Publisher-Key: ${PUBLISHER_INTERNAL_KEY}"
```

Состояния: `pending_approval` / `approved_waiting` / `rejected` / `revision_requested` — не публиковать; `approved` — можно передать Backend; `publishing` — уже выполняется; `published` — готово; `publish_unknown` — ручная сверка; `not_configured`/`publish_failed` — исправить и вручную вернуть в `approved` через `resolve`.

## Обязательные переменные
- Backend `.env`: `PUBLISHER_INTERNAL_KEY` — отдельный длинный случайный ключ.
- Worker Данила: тот же ключ в переменной окружения. Не передавать `ADMIN_API_KEY` worker-у.
- Backend `.env`: `ENCRYPTION_KEY` — Fernet key для VK community token и MAX bot token.
- Telegram bot token остаётся на Backend; worker его не получает.
- Worker получает только `DATABASE_URL`, `BACKEND_URL`, `PUBLISHER_INTERNAL_KEY`, `AI_AGENT_URL`, `AI_AGENT_API_KEY` и числовые настройки опроса. `ADMIN_API_KEY`, `ENCRYPTION_KEY`, `OPENAI_API_KEY`, Telegram bot token, VK community token и MAX bot token ему не передаются.
- Backend, bot и worker передают `X-Internal-Key: ${AI_AGENT_API_KEY}` при обращении к AI-agent; `OPENAI_API_KEY` остаётся только у AI-agent.

## VK
Используется community token и `wall.post`; если к публикации прикреплено изображение, Backend использует Photos API (`photos.getWallUploadServer`, `photos.saveWallPhoto`). Для изображений у community token должно быть право на Photos API.

## MAX
Backend вызывает официальный `POST https://platform-api2.max.ru/messages?chat_id=<int64>` с токеном только в `Authorization: <access_token>`. Поддерживаются текст и изображение через `attachments=[{"type":"image","payload":{"url":"https://…"}}]` после HTTPS/SSRF-проверки.

MAX token и chat ID задаются отдельно для каждой компании через admin API. Token хранится зашифрованным Fernet, не попадает в `CompanyOut`, `.env`, Compose или worker. Успех подтверждается только реальным `message.message.mid`; `message.url`, если он возвращён, сохраняется как публичная ссылка. Фейкового `published` и синтетического ID нет.

Отсутствие/ошибка конфигурации → `not_configured`; определённый отказ HTTP 4xx или `success:false` → `publish_failed`; timeout, transport failure, 5xx, invalid JSON или HTTP 200 без MID → `publish_unknown`. Первые два состояния можно вернуть в `approved` после исправления через `{"action":"retry"}`; для `publish_unknown` по-прежнему обязательна ручная сверка и `confirmed_not_published=true`.

**Живой MAX E2E: `live_not_verified`.** Команда не является юрлицом и на момент хакатона не имела токена MAX. Реализован и проверен mock/unit-тестами работоспособный код по официальному API, но реальная публикация и полный MAX E2E не выполнялись.

## Мультиарендность
Внешний ключ компании (`company_id`, например `salavatsteklo_demo`) сопоставляется с внутренним integer `companies.id`. Все публикации, история и knowledge items привязаны к внутреннему `company_id`. Ключи клиентов ограничены компанией и scopes; worker получает только внутренний publisher key.

## Уведомления и мониторинг

Backend отправляет уведомление после `published`, при переходе в `publish_unknown`, а также при `not_configured`/`publish_failed` с указанием остановки автоматического повтора. Ошибка такого уведомления не откатывает уже подтверждённую публикацию или сохранённый терминальный статус.

Чтобы сохранить мониторинг без передачи platform tokens worker-у, добавлены internal endpoint-ы с тем же `X-Publisher-Key`:

- `GET /internal/publications/{id}/comments` — Backend вызывает VK `wall.getComments` и возвращает нормализованный список комментариев;
- `POST /internal/publications/{id}/notify` с `{"text":"..."}` — Backend отправляет алерт ответственному компании.

Worker ведёт собственный mirror `platform_posts` для мониторинга и `comment_events`, но не меняет `publications`. После ручного `mark_published` mirror синхронизируется автоматически. Автотесты контракта: `publications/tests/test_internal_contract.py`.
