# AI-агент (Р2, Михайлова Ангелина)

Задачи из «Спринтов»: 5 (аккаунт ChatGPT), 9 (промпт-контракт), 14 (изучение бизнеса и база знаний), 15 (пост: текст + картинка). Шаги сценария: 3, 4, 9. Слайд 5.

Стек согласован с бэкендом: Python 3.12, FastAPI, PostgreSQL 16 (общая БД), SQLAlchemy 2.x + asyncpg, HTTPX, Docker Compose. Агент — отдельный сервис `ai-agent`, бэкенд обращается к нему по `AI_AGENT_URL`.

## Запуск

```bash
# локально без Docker и без ключей (SQLite во временной папке, offline-модель)
pip install -r requirements.txt
python demo.py
python -m unittest discover -s tests -v

# в Docker вместе с Postgres
cp .env.example .env
docker compose up --build            # API на http://localhost:8001, документация /docs
AI_AGENT_URL=http://localhost:8001 python demo.py
```

Модель меняется только переменными окружения: `LLM_PROVIDER=offline | openai | codex`. Данные компаний при этом не теряются.

## Форма настройки компании (критерий 11)

Агент сам отдаёт форму по адресу `/admin` (например, http://localhost:8001/admin). Это макет Лизы, подключённый к API: 4 шага — профиль, документы, согласование, проверка; кнопка «Создать компанию» отправляет профиль и файлы в базу знаний и запускает анализ. У каждого файла выбирается тип: «Факты» (источник для постов) или «Правила и контекст» (КП, методички, ToV — в посты не цитируются). Повторная загрузка того же файла не задваивает базу. Если на сервере задан `AI_AGENT_API_KEY`, ключ вводится в форме и хранится только до закрытия вкладки.

## Профиль компании без программирования

Тело `PUT /v1/companies/{company_id}` — это JSON-профиль: тон, сегменты ЦА с призывами к действию, ссылки на официальные источники [S1]…, правила атрибуции («показатели группы компаний — не показатели АО»), запрещённые утверждения, инструкция компании, стиль и подпись изображения. Пример — `demo_data/salavatsteklo/profile.json`, собран из пакета материалов Лизы. Документы пакета (01–06) загружаются через `/documents`.

Промпт-контракт — `marketer/prompts/*_v3.md` (v1 и v2 оставлены для истории). Фрагменты базы знаний агент помечает [K1], [K2]…, метки [S1]… внутри документов — официальные источники компании.

## Таблицы в общей БД

Все таблицы модуля начинаются с `ai_` и создаются при старте сервиса: `ai_company_profile`, `ai_document`, `ai_chunk`, `ai_message`, `ai_decision`, `ai_draft`, `ai_action_log`. Таблицы `company`, `task`, `publication`, `platform_post` — не мои, я их не трогаю. Все ID — UUID; `company_id` и `task_id` те же, что у бэкенда.

Изоляция компаний: любой запрос к данным идёт через `CompanyRepo`, который добавляет фильтр `company_id`. Чужой черновик для другой компании — 404 (проверяется тестом).

## Основной контракт Backend → AI-агент

`POST {AI_AGENT_URL}/v1/generate` (те же обработчики висят на `/generate` и `/`), заголовок `X-Internal-Key`.

```json
{
  "prompt": "Подготовь пост о стекле Ultraview R для архитекторов",
  "company": {"id": 7, "company_id": "salavatsteklo", "external_id": "salavatsteklo", "name": "АО «Салаватстекло»"},
  "history": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}],
  "knowledge_base": [{"title": "О компании", "content": "до 4000 символов", "source": "https://salstek.ru/..."}]
}
```

Ответ: `{"text": "непустой текст", "image_url": "https://…" | null}` и служебные поля `kind` (draft/answer), `engine`, `draft_id`, `status`, `notes` (пояснение для клиента: что учтено, что исправила автопроверка), `agent_company_id`.

- Компания в контуре агента определяется по `company.company_id`, затем `external_id`, затем `id`. UUID берётся как есть; любая другая строка превращается в постоянный UUID (uuid5), так что одна компания бэкенда всегда попадает в одну изолированную базу. Числовой `id` бэкенда агенту не нужен.
- `knowledge_base` пустой или отсутствует — не ошибка, `text` всё равно непустой.
- `image_url` — только публичный `https` (бэкенд других не принимает, Telegram и VK качают картинку сами): ссылка Pollinations с фиксированным seed или `AI_PUBLIC_MEDIA_BASE_URL`, если агент опубликован наружу. Офлайн-картинка → `null`.
- Промпт «черновик ответа на комментарий» (воркер публикаций) → 1–3 предложения без поста и картинки, `kind = comment_reply`.
- Поля бэкенда `tone_of_voice`, `target_audience`, `advantages` попадают в профиль компании.
- Если компанию не настраивали через `/admin`, профиль создаётся автоматически из полей `company` (name, industry, audience, tone, description, products).
- `history` принимается в виде `{role, content}` или `{role, text}`, учитываются последние `HISTORY_LIMIT` сообщений.
- E2E «знания двух компаний не смешиваются» — тест `test_backend_contract_kb_isolation`.

## API (внутренний, заголовок `X-Internal-Key: $AI_AGENT_API_KEY`)

| Метод | Путь | Кто вызывает | Что делает |
|---|---|---|---|
| POST | `/v1/generate` (то же на `/generate` и `/`) | бэкенд, **основной контракт** | `{prompt, company, history, knowledge_base}` → `{text, image_url}`, см. ниже |
| GET | `/admin` | администратор | форма настройки компании |
| PUT | `/v1/companies/{company_id}` | форма /admin или бэкенд | профиль компании (поля — в `CompanyIn`, пример — `demo_data/salavatsteklo/profile.json`) |
| POST | `/v1/companies/{id}/documents` | бэкенд (админка) | загрузка файла multipart: txt, md, html, pdf, docx |
| POST | `/v1/companies/{id}/documents/text` | бэкенд | текст `{name, text}` |
| POST | `/v1/companies/{id}/sites` | бэкенд | загрузка сайта `{url}` |
| POST | `/v1/companies/{id}/analyze` | бэкенд | продукты, ЦА, преимущества → в профиль |
| POST | `/v1/companies/{id}/messages` | бот | любое сообщение пользователя `{chat_id, text, task_id?}` |
| POST | `/v1/companies/{id}/drafts` | планировщик | черновик без диалога `{request, platform?, task_id?}` |
| POST | `/v1/companies/{id}/drafts/{draft_id}/revise` | бот, кнопка «На доработку» | `{comment, chat_id?}` → новая версия |
| POST | `/v1/companies/{id}/drafts/{draft_id}/status` | бэкенд | итог: approved / rejected / published |
| GET | `/v1/companies/{id}/drafts/{draft_id}` | любой | черновик |
| GET | `/media/{company_id}/{file}.png` | модуль публикаций | картинка черновика |
| GET | `/health` | Docker | статус и текущая модель |

Ответ на `/messages`, `/drafts`, `/revise`:

```json
{
  "kind": "draft",
  "text": "Черновик (версия 1) для telegram готов и ждёт согласования. ...",
  "engine": "codex (ChatGPT account)",
  "draft": {
    "draft_id": "uuid", "company_id": "uuid", "task_id": "uuid|null",
    "parent_draft_id": null, "version": 1, "status": "draft", "topic": "...",
    "platforms": ["telegram"],
    "content": {"text": "...", "images": ["http://ai-agent:8000/media/<company>/<draft>.png"],
                "cta_buttons": [], "source_url": null},
    "check": {"ok": true, "issues": [], "fixed": []},
    "sources_used": [{"sid": "S1", "doc": "o_kompanii.md", "text": "..."}]
  },
  "sources": []
}
```

`kind = "answer"` — обычный ответ: бот просто пересылает `text`, публикацию не создаёт.

`draft.content` уже в формате `content` из контракта publication — его можно переносить без изменений.

## Как это ложится на цикл бэкенда

1. Бот получает сообщение → `POST /messages`.
2. Если `kind = "draft"`: бэкенд создаёт `publication` (status = pending_approval) с `content` из ответа и шлёт на согласование: текст + картинка + площадка + время.
3. «Одобрить» → бэкенд ставит `approved`, передаёт в модуль публикаций и сообщает мне `POST /status {approved}`.
4. «На доработку» → `POST /drafts/{draft_id}/revise {comment}` → новая версия → снова на согласование. Старая версия у меня становится `superseded`.
5. «Отклонить» → `POST /status {rejected}`.

Сам агент ничего не публикует.

## Ограничения (для п. 8.4)

- В режиме offline тексты собираются шаблоном из фактов базы знаний. Это план Б, а не «творческий» маркетолог.
- Поиск по словам (BM25), без векторной модели.
- Таблицы создаются при старте сервиса; для продакшена нужны миграции Alembic.
- GigaChat пока не подключён: у него своя авторизация, не OpenAI-совместимая. Если команда выберет его по «Затратам», нужен отдельный провайдер.
- Картинки отдаются по неугадываемому UUID-адресу без авторизации — для хакатона нормально, в продакшене нужна подписанная ссылка.
- Записи `knowledge_base` от бэкенда сохраняются в базу знаний компании (дубли пропускаются по хэшу). Если запись удалили в бэкенде, у агента она остаётся до очистки базы.
- Docker-образ собирается из `python:3.12-slim`; `docker compose up --build` проверен на Windows (Docker Desktop): образ собирается, Postgres и агент стартуют.
