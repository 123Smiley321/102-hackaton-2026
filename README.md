# Кибер 102 — Автономный AI-маркетолог (прототип хакатона)

Многокомпонентное приложение: представитель компании управляет маркетингом через Telegram,
AI-агент готовит контент, модуль согласования собирает решения, воркер публикаций размещает
материал во ВКонтакте и Telegram.

## Состав репозитория

| Папка | Что это | Владелец |
|---|---|---|
| `backend/` | FastAPI + aiogram-бот: задачи, история диалога, профили компаний, согласование (approve/reject/доработка), аудит | Р1 (Арина) |
| `publications/` | Воркер публикаций: очередь approved-строк, адаптеры VK/Telegram, адаптация текста, ретраи, мониторинг комментариев | Р5 (Данил) |
| `docs/` | База знаний и материалы тестовой компании (Салаватстекло) | Р3 (Лиза) |
| `docker-compose.yml` | Все сервисы: db, backend, bot (profile telegram), publications | общий |

## Быстрый старт (Docker)

```bash
cp .env.example .env   # заполнить TELEGRAM_BOT_TOKEN, TELEGRAM_TARGET_CHAT, ADMIN_API_KEY, ENCRYPTION_KEY
docker compose --profile telegram up --build -d
curl http://localhost:8000/health   # {"status":"ok"}
```

Чистая база создаётся сама (`create_all`). Для существующей базы накатить миграции из
`backend/migration_*.sql` (порядок: external_id → approved_at → task13 → task16).

## Контракт backend ↔ модуль публикаций

- Кнопка «Одобрить» только переводит `pending_approval → approved` (+`approved_by`, `approved_at`, UTC),
  с блокировкой строки и проверкой чата согласовавшего. VK/TG API в боте не вызывается.
- Режим `approval_mode=all_platforms`: строки висят в `approved_waiting`, пока не одобрены все
  площадки задачи; воркер забирает только `approved`.
- Воркер раз в 15 сек опрашивает `publications WHERE status='approved'` (`FOR UPDATE SKIP LOCKED`),
  публикует через адаптер, пишет результат в `platform_posts`, ретраи 5/30/120 с, уведомления в TG.
- `scheduled_at IS NULL` → публикация сразу после одобрения; иначе — в назначенное время.
- Кросспостинг: отдельная строка `publications` на площадку, `idempotency_key` уникален.
- Статусы `approved_waiting`, `revision_requested`, `pending_approval`, `rejected` воркер не трогает.

## Проверка без AI-агента

Оставь `AI_AGENT_URL` пустым. Тестовую одобренную публикацию вставляем напрямую
(нужны существующие компания и задача, поэтому в INSERT добавлены `created_at` и `task_id`):

```bash
docker compose exec db psql -U postgres -d marketer -c \
  "INSERT INTO publications (company_id, task_id, text, platform, status, idempotency_key, scheduled_at, created_at)
   VALUES (1, 1, 'Тестовый пост из модуля публикаций 🚀', 'telegram', 'approved', 'test-key-tg-1', NULL, now());"
```

Через ≤15 сек пост в `TELEGRAM_TARGET_CHAT`, в БД `status='published'`, уведомление в чат согласования.
VK-реквизиты — через `POST /api/companies` или `PATCH /api/companies/{id}` (`vk_group_id`, `vk_access_token`,
токен шифруется). Проверка токена: `curl "https://api.vk.com/method/groups.getById?access_token=...&v=5.199"`.

## Проверки качества

- Backend: `backend/tests_task12.md`, `backend/tests_task13.md`, `backend/tests_task16.md`.
- Модуль публикаций: рестарт воркера не должен создавать дубли (идемпотентность по `idempotency_key` + статусам).

## Безопасность

Секреты только в `.env` (в gitignore). VK-токены компаний — в БД, шифрование Fernet (`ENCRYPTION_KEY`
единый для backend и воркера, менять только с чистой базой). Синтетические данные, минимальные права токенов.
