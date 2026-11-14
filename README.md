# Кибер 102 — Автономный AI-маркетолог

Прототип для хакатона: представитель компании отправляет поручение в Telegram, AI-агент
готовит материал, ответственный согласует его, Backend публикует в VK/Telegram/MAX и хранит
аудит. Worker Данила отвечает за очередь, расписание и мониторинг комментариев VK.

## Состав репозитория

| Папка | Назначение | Владелец |
|---|---|---|
| `backend/` | FastAPI + aiogram: компании, история, knowledge base, согласование, публикация, безопасность, аудит | Р1 (Арина) |
| `publications/` | Очередь approved-публикаций, вызов internal API, мониторинг VK, черновики ответов | Р5 (Данил) |
| `docs/` | База знаний и материалы Салаватстекло | Р3 (Лиза) |
| `docker-compose.yml` | PostgreSQL, Backend, Telegram-бот и publication worker | общий |

## Быстрый старт

```bash
cp .env.example .env
# Заполнить TELEGRAM_BOT_TOKEN, ADMIN_API_KEY, PUBLISHER_INTERNAL_KEY, ENCRYPTION_KEY.
# ADMIN_API_KEY и PUBLISHER_INTERNAL_KEY должны быть разными случайными значениями.
docker compose --profile telegram up --build -d
curl http://localhost:8000/health   # {"status":"ok"}
```

Swagger: `http://localhost:8000/docs`. Создание и настройка компании описаны в
`backend/README.md`. VK community token передаётся только в `vk_access_token` через
company API; Backend шифрует его Fernet и не возвращает в ответах.

### Обновление существующей БД

`create_all` не добавляет столбцы в существующие таблицы. До запуска новой версии один раз
применить миграции в порядке:

1. `migration_add_company_external_id.sql`;
2. `migration_add_approved_at.sql`;
3. `migration_task13_company_settings.sql`;
4. `migration_task16_approval_workflow.sql`;
5. `migration_task17_18_security_publishing.sql`;
6. `migration_add_max_publisher.sql` — только для MAX-адаптера.

Пример миграции задач №17–18:

```bash
docker compose exec -T db psql -U postgres -d marketer \
  < backend/migration_task17_18_security_publishing.sql
```

Для существующей БД после неё примените MAX-миграцию:

```bash
docker compose exec -T db psql -U postgres -d marketer \
  < backend/migration_add_max_publisher.sql
```

После обновления контейнеров старый VK token из `.env` больше не используется: при необходимости
повторно сохраните его через `PATCH /api/companies/{id}`. MAX token также задаётся только через
company API (`max_access_token`), а не через `.env`; подробнее — `backend/MAX.md`.

## Контракт Backend ↔ worker

Worker каждые 15 секунд выбирает только `publications.status='approved'` с наступившим
`scheduled_at` (`NULL` означает «сразу») и вызывает:

```http
POST /internal/publications/{publication_id}/publish
X-Publisher-Key: <PUBLISHER_INTERNAL_KEY>
```

Backend повторно проверяет статус, время, активность компании и разрешённую площадку. Только
Backend вызывает VK/Telegram/MAX и меняет канонический статус `publications`.

- `200 published` / `already_published` — завершено; worker обновляет mirror `platform_posts`.
- `409` — проверка статуса/времени не обходится.
- `422 not_configured` / `publish_failed` — локальная конфигурация не задана либо площадка
  определённо отклонила запрос; worker прекращает попытки до ручного `resolve`.
- `502` — результат неоднозначен, Backend ставит `publish_unknown`; автоматический повтор запрещён.
- Уведомления об успешной публикации и ручной сверке отправляет Backend.
- После ручного `mark_published` worker синхронизирует mirror и подхватывает пост мониторингом.

Для ручного разрешения `publish_unknown`:

```http
POST /api/publications/{id}/resolve
X-Admin-Key: <ADMIN_API_KEY>
Content-Type: application/json

{"action":"mark_published","published_post_id":"123","published_url":"https://..."}
```

Либо `{"action":"retry","confirmed_not_published":true}` только после проверки, что поста
на площадке нет. Это не exactly-once магия: VK/Telegram/MAX не дают универсального idempotency key
для отправки постов. Для `not_configured`/`publish_failed` после исправления используется
`{"action":"retry"}` без подтверждения неопубликованности: внешний исход там не неоднозначен.

### Мониторинг комментариев

Worker не получает VK/Telegram tokens. Он использует:

- `GET /internal/publications/{id}/comments` — Backend читает `wall.getComments`;
- `POST /internal/publications/{id}/notify` — Backend отправляет алерт в чат компании.

Новые комментарии сохраняются в `comment_events`. Негатив эскалируется человеку; для остальных
AI готовит только черновик ответа. Автоматических публичных ответов нет.

## Проверка публикации без AI-агента

Оставьте `AI_AGENT_URL` пустым. Нужны существующие компания с `enabled_platforms=["telegram"]`,
заполненным `telegram_chat_id` и задача в `tasks`. Затем:

```bash
docker compose exec db psql -U postgres -d marketer -c \
  "INSERT INTO publications (company_id, task_id, text, platform, status, idempotency_key, scheduled_at, created_at)
   VALUES (1, 1, 'Тестовый пост из модуля публикаций 🚀', 'telegram', 'approved', 'test-key-tg-1', NULL, now());"
```

Для повторного теста используйте новый `idempotency_key`. В течение интервала опроса Backend
отправляет пост в `telegram_chat_id` этой компании, пишет `published`, remote ID/URL/время и аудит,
затем отправляет уведомление в `approval_chat_id` (или `telegram_chat_id`). Перезапуск worker-а не
должен создавать дубль.

## MAX: официальный API без имитации публикации

Реализованы текст и изображение по прямой HTTPS-ссылке через `POST platform-api2.max.ru/messages`,
company-scoped encrypted token, обработка ответов и ручное разрешение ошибок. Подтверждением успеха
служит только реальный `message.message.mid`; подробный контракт и команды — `backend/MAX.md`.

**Живой MAX E2E не выполнялся (`live_not_verified`).** Команда не является юрлицом и на момент
хакатона не имела токена MAX. Код реализован по официальному API и проверен статически и
mock/unit-тестами; это не заявляется как реальная публикация в MAX.

## AI-контракт

Backend отправляет `{prompt, company, history, knowledge_base}`. `knowledge_base` — до 20 последних
записей выбранной компании, каждая содержит `title`, `content` (до 4000 символов) и `source`.
Ожидаемый ответ: `{"text":"...","image_url":"https://..."}`, где `image_url` необязателен.

## Проверки качества

- Чек-листы Backend: `backend/tests_task12.md`, `tests_task13.md`, `tests_task16.md`, `tests_task17_18.md`.
- Автотесты worker-контракта: `python -m unittest discover -s publications/tests -v`.
- MAX/backend mock-тесты: `PYTHONPATH=backend python -m unittest discover -s backend/tests -v`
  (зависимости: `backend/requirements-dev.txt`).
- После рестарта: повторный internal request возвращает `already_published`, дублей нет.
- Для 502 проверить `publish_unknown` и оба сценария ручного разрешения.

## Безопасность и ограничения

- `.env` не коммитится. Используйте тестовые токены и синтетические данные.
- Worker получает только `DATABASE_URL`, `BACKEND_URL`, `PUBLISHER_INTERNAL_KEY`, AI URL и настройки опроса.
  `ADMIN_API_KEY`, `TELEGRAM_BOT_TOKEN`, `ENCRYPTION_KEY`, VK и MAX tokens ему не передаются.
- Bot получает Telegram token и AI URL, но не admin/publisher/Fernet keys.
- Company API keys хранятся как SHA-256 hash, ограничены компанией/scopes и отзываются.
- MAX token передаётся только в `Authorization` официального API и хранится зашифрованным в БД;
  без конфигурации возвращается `not_configured`, фейковой публикации нет.
- Для production нужны Alembic, JWT/RBAC и PostgreSQL RLS; текущий вариант — прототип хакатона.
