# Контракт интеграции для Данила: Backend №17–18

## Публикация
Backend остаётся единственной точкой вызова API площадок. Worker Данила:

1. Выбирает только `publications.status = 'approved'` и учитывает `scheduled_at`.
2. Вызывает `POST http://backend:8000/internal/publications/{publication_id}/publish`.
3. Передаёт `X-Publisher-Key: ${PUBLISHER_INTERNAL_KEY}`.
4. Не вызывает VK/Telegram API самостоятельно и не переводит строку в `published` самостоятельно. Backend запишет результат и audit log.
5. При HTTP 200 и `status=published` считает задачу завершённой. `already_published` — также завершённая задача.
6. При HTTP 409 не пытается обходить проверку статуса/времени.
7. При HTTP 502 не повторяет отправку автоматически: публикация переведена в `publish_unknown`, требуется ручная сверка.

Пример запроса:

```bash
curl -X POST "http://backend:8000/internal/publications/123/publish" \
  -H "X-Publisher-Key: ${PUBLISHER_INTERNAL_KEY}"
```

Состояния: `pending_approval` / `approved_waiting` / `rejected` / `revision_requested` — не публиковать; `approved` — можно передать Backend; `publishing` — уже выполняется; `published` — готово; `publish_unknown` — ручная сверка.

## Обязательные переменные
- Backend `.env`: `PUBLISHER_INTERNAL_KEY` — отдельный длинный случайный ключ.
- Worker Данила: тот же ключ в переменной окружения. Не передавать `ADMIN_API_KEY` worker-у.
- Backend `.env`: `ENCRYPTION_KEY` — Fernet key для VK community token.
- Telegram bot token остаётся на Backend; worker его не получает.
- Worker получает только `DATABASE_URL`, `BACKEND_URL`, `PUBLISHER_INTERNAL_KEY`, `AI_AGENT_URL`, `AI_AGENT_API_KEY` и числовые настройки опроса. `ENCRYPTION_KEY`, `OPENAI_API_KEY` и VK community token ему не передаются.

## VK
Используется community token и `wall.post`; если к публикации прикреплено изображение, Backend использует Photos API (`photos.getWallUploadServer`, `photos.saveWallPhoto`). Для изображений у community token должно быть право на Photos API.

## MAX
Адаптер MAX пока возвращает явную ошибку, пока команда не подтвердит API endpoint, тип токена и способ загрузки медиа. Не подменять это фейковой успешной публикацией.

## Мультиарендность
Внешний ключ компании (`company_id`, например `salavatsteklo_demo`) сопоставляется с внутренним integer `companies.id`. Все публикации, история и knowledge items привязаны к внутреннему `company_id`. Ключи клиентов ограничены компанией и scopes; worker получает только внутренний publisher key.

## Уведомления и мониторинг

Backend отправляет уведомление после `published` и при переходе в `publish_unknown`. Ошибка такого уведомления не откатывает уже подтверждённую публикацию.

Чтобы сохранить мониторинг без передачи platform tokens worker-у, добавлены internal endpoint-ы с тем же `X-Publisher-Key`:

- `GET /internal/publications/{id}/comments` — Backend вызывает VK `wall.getComments` и возвращает нормализованный список комментариев;
- `POST /internal/publications/{id}/notify` с `{"text":"..."}` — Backend отправляет алерт ответственному компании.

Worker ведёт собственный mirror `platform_posts` для мониторинга и `comment_events`, но не меняет `publications`. После ручного `mark_published` mirror синхронизируется автоматически. Автотесты контракта: `publications/tests/test_internal_contract.py`.
