# Кибер 102 — подтверждённые результаты и границы проверки

Дата обновления: 15.11.2026.

Отчёт разделяет ранее наблюдавшийся живой прогон, изолированные проверки объединённого кода и сценарии, которые ещё требуют запуска. Секреты, access tokens, реальные folder ID и значения внутренних ключей не включены.

## 1. Живой контур публикаций прежнего `main` — журнал Claw

До интеграции AI-agent проверялась четырёхсервисная конфигурация: PostgreSQL, Backend, Telegram-бот и publication worker.

- Сервисы поднялись, Backend health check вернул `status=ok`.
- Telegram: согласованная публикация появилась в тестовом чате, пришло уведомление, запись появилась в mirror `platform_posts`.
- Перезапуск worker не создал дубль. Повторный internal publish вернул `already_published`; неверный publisher key — HTTP 401.
- Несогласованная публикация (`pending_approval`) получила HTTP 409.
- VK: пост появился на стене тестового сообщества; community token хранился зашифрованным в БД.
- Кросспостинг VK + Telegram и отложенная публикация по `scheduled_at` отработали; до наступления времени Backend возвращал HTTP 409.
- При сетевых обрывах механика `publish_unknown` и ручного `resolve` сработала; автоматический слепой retry не использовался.
- Internal notify доставлял уведомления; пустой текст отклонялся с HTTP 422.

Эти наблюдения относятся к прежнему живому прогону и не заменяют финальный E2E объединённого AI + MAX дерева.

## 2. AI-интеграция — объединена в `main`, живой gate ещё требуется

AI PR #1 ветки `ai-agent-integration` объединён в `main` merge commit `68eb8f9bc12e07bb69dee69f115fd7a47db8d01f`.

Подтверждено кодом и изолированными проверками:

- В Compose добавлен пятый сервис `ai-agent`; порт `8001` опубликован только на `127.0.0.1` хоста.
- Backend, bot и worker передают `X-Internal-Key` при заданном `AI_AGENT_API_KEY`.
- `OPENAI_API_KEY` выдаётся только AI-agent. Backend, bot и worker не наследуют общий `.env` целиком.
- AI-agent не получает admin/publisher/Fernet/Telegram/VK/MAX secrets.
- YandexGPT подключается через OpenAI-compatible API; реальный ключ и folder ID задаются только локально.

Не считать выполненным без фактического запуска:

- полный unit suite `ai_agent/tests`;
- Docker build и health checks пяти сервисов;
- живой YandexGPT request и подтверждение фактического `engine`;
- HTTP 401 без/с неверным `X-Internal-Key` и HTTP 200 с верным;
- generation с пустой и непустой KB, изоляция двух компаний и comment reply без изображения;
- полный Telegram → AI-agent → согласование → worker → Backend → VK/Telegram E2E.

## 3. MAX — рабочий адаптер, живой E2E не выполнялся

После merge AI PR #1 обновление MAX PR #2 вызвало конфликт. MAX-изменения перенесены поверх актуального `main` в отдельной ветке `final-ai-max-integration`; AI-only конфигурация worker и Compose не перезаписывалась MAX baseline.

Реализовано и проверено изолированно:

- Официальный `POST https://platform-api2.max.ru/messages?chat_id=<int64>`, token только в `Authorization`.
- Текст и изображение через direct URL attachment после HTTPS/SSRF-проверки.
- Company-scoped `max_access_token` и `max_chat_id`; token шифруется Fernet, не возвращается API и не передаётся worker-у.
- Успех только при реальном `message.message.mid`; синтетического MID и fake success нет.
- `not_configured` и `publish_failed` возвращают HTTP 422 и не создают бессмысленный retry loop.
- Timeout, transport failure, 5xx, invalid JSON и HTTP 200 без MID остаются `publish_unknown` и требуют ручной сверки.
- Миграция `migration_add_max_publisher.sql`, revoke и ручной requeue добавлены.

Объединённый локальный gate перед итоговым PR:

- Backend/MAX: **14/14** unit/mock-тестов пройдено.
- Worker AI + MAX contract: **11/11** тестов пройдено.
- Проверены повторный `already_published`, отсутствие inline retry, AI internal key и company-scoped comment payload.

**Статус живого MAX E2E: `live_not_verified`.** Команда не является юрлицом и на момент хакатона не имела токена MAX. Поэтому реализован рабочий адаптер по официальному API и проведены статические и unit/mock-проверки, но живая публикация и полный MAX E2E не выполнялись.

Старое наблюдение Claw «MAX → HTTP 502 / `publish_unknown`» относится к прежнему baseline без реализованного адаптера. Оно не является результатом нового MAX-кода и не выдаётся за такой результат.

## 4. Известные ограничения

- VK comments не входят в обязательный MVP демонстрации. Ранее выданный community token получил VK API error 27; живой сценарий comments не подтверждён. Автоматических публичных ответов система не выполняет.
- Из WSL2 наблюдались временные TLS/ConnectError к `api.telegram.org`. При `publish_unknown` сначала сверять площадку, затем использовать `/api/publications/{id}/resolve`; не повторять отправку вслепую.
- VK token был виден в рабочем журнале и должен быть перевыпущен. Старое значение в отчёт и репозиторий не переносится.

## 5. Протокол финального живого прогона

Для каждой проверки фиксировать ветку и commit SHA, фактический HTTP code/status/engine, publication ID и внешний URL тестового поста. Доказательство — очищенный короткий лог или screenshot; итог — PASS / FAIL / BLOCKED.

Перед сохранением логов удалить Authorization/API keys/Telegram, VK и MAX tokens, значения внутренних ключей и секретные URL. Не прикладывать `.env`. При ошибке указать точное место сбоя и не заявлять успешный E2E.
