# Проверки задач №17–18

## №17 — публикация через API площадок
1. Публикация со статусом `pending_approval` отклоняется HTTP 409.
2. Публикация `rejected`, `revision_requested`, `approved_waiting` не отправляется.
3. Только `approved` и наступивший `scheduled_at` допускаются к отправке.
4. Публикация в отключённую для компании площадку отклоняется.
5. VK: community token расшифровывается, при наличии изображения оно загружается в wall photos, затем вызывается `wall.post`.
6. Telegram: отправляется `sendMessage` либо `sendPhoto` в `telegram_chat_id` именно этой компании.
7. После успеха сохраняются remote post/message ID, URL, время и статус `published`.
8. Повторный запрос уже опубликованного ID возвращает `already_published` без повторного вызова API.
9. Параллельные запросы блокируются row lock и переходом `approved` → `publishing`.
10. При сетевой ошибке/неоднозначном результате статус `publish_unknown`; автоматический повтор запрещён до ручной сверки с площадкой.
11. `publish_unknown` разрешается только вручную: отметить опубликованным с внешним ID либо вернуть в `approved` после явного подтверждения, что поста на площадке нет.
12. Проверить, что worker Данила вызывает `POST /internal/publications/{id}/publish` с `X-Publisher-Key` и не публикует напрямую.
13. MAX: без company token/chat ID возвращается HTTP 422 + `not_configured` без внешнего вызова; с реальным `message.message.mid` сохраняется `published`; фейкового ID нет.
14. После успеха Backend отправляет уведомление ответственному; сбой уведомления не меняет статус `published`.
15. При `publish_unknown` ответственный получает алерт о ручной сверке.
16. Комментарии VK и worker-уведомления проходят только через internal Backend endpoints с `X-Publisher-Key`.
17. После ручного `mark_published` worker синхронизирует `platform_posts`, чтобы мониторинг увидел пост.
18. MAX text: `POST https://platform-api2.max.ru/messages?chat_id=<int64>` с token только в `Authorization`; в теле нет token.
19. MAX image: в `attachments` передаётся `type=image` и `payload.url` после HTTPS/SSRF-проверки.
20. MAX HTTP 4xx/`success:false` → `publish_failed` + HTTP 422, не `publish_unknown`.
21. MAX timeout/5xx/invalid JSON/HTTP 200 без `mid` → `publish_unknown`; inline/automatic retry отсутствует.
22. Worker распознаёт `not_configured`/`publish_failed`; запись покидает approved-очередь до ручного `resolve`.
23. `not_configured`/`publish_failed` возвращаются через `{"action":"retry"}` после исправления; для `publish_unknown` по-прежнему нужен `confirmed_not_published=true`.
24. Автотесты: `PYTHONPATH=backend python -m unittest discover -s backend/tests -v` и `python -m unittest discover -s publications/tests -v`. Это mock/static gate, не живой MAX E2E.

## №18 — изоляция и безопасность
1. Компания A не получает публикации, историю, ключи или knowledge items компании B.
2. У каждой записи профиля, истории, публикации и базы знаний есть `company_id`.
3. Company API key работает только для указанной компании и только с выданными scopes.
4. Ключ хранится как SHA-256 hash; исходное значение показывается один раз.
5. Отозванный ключ получает HTTP 401.
6. Ключ без нужного scope получает HTTP 403.
7. Отзыв VK credential очищает только VK token/group ID выбранной компании.
8. Отзыв Telegram destination отключает только chat ID выбранной компании; общий bot token не раскрывается.
9. VK token сохраняется только при настроенном Fernet `ENCRYPTION_KEY`; без ключа операция должна завершиться ошибкой.
10. Проверить audit log для создания/изменения компании, ключей, отзыва credentials и публикаций.
11. Проверить, что API-ответы CompanyOut не содержат токены, включая `max_access_token`.
12. MAX token сохраняется только через company API при настроенном Fernet и не добавляется в `.env`, Compose или worker.
13. Отзыв MAX credential очищает token/chat ID и `enabled_platforms` только выбранной компании, не затрагивая VK/Telegram.
14. Живой MAX E2E имеет статус `live_not_verified`: команда не является юрлицом и не имела MAX token; mock-тесты не выдаются за реальную публикацию.
