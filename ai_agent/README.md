# AI-агент (Р2, Михайлова Ангелина)

Задачи из «Спринтов»: 5 (аккаунт ChatGPT), 9 (промпт-контракт), 14 (изучение бизнеса и база знаний), 15 (пост: текст + картинка). Шаги сценария: 3, 4, 9. Слайд 5.

Стек: Python 3.12, FastAPI, PostgreSQL 16 (общая БД), SQLAlchemy 2.x + asyncpg, HTTPX, Docker Compose. Агент — отдельный сервис `ai-agent`; Backend, Telegram-бот и publication worker обращаются к нему по `AI_AGENT_URL` с внутренним заголовком `X-Internal-Key`.

## Запуск

### Локально без Docker и облачных ключей

SQLite во временной папке, offline-модель:

```bash
pip install -r requirements.txt
python demo.py
python -m unittest discover -s tests -v
```

### В общем пятисервисном контуре

Запуск выполняется из корня репозитория, а не из вложенного `ai_agent/docker-compose.yml`:

```bash
cp .env.example .env
# Заполнить независимые ADMIN_API_KEY, PUBLISHER_INTERNAL_KEY, AI_AGENT_API_KEY,
# TELEGRAM_BOT_TOKEN, ENCRYPTION_KEY и параметры LLM.
docker compose --profile telegram up --build -d
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8001/health
```

AI health показывает фактический `engine`. Для YandexGPT в локальном `.env`:

```dotenv
AI_AGENT_URL=http://ai-agent:8000/v1/generate
AI_AGENT_API_KEY=<отдельный случайный внутренний ключ>
LLM_PROVIDER=openai
OPENAI_BASE_URL=https://llm.api.cloud.yandex.net/v1
OPENAI_MODEL=gpt://<folder_id>/yandexgpt/latest
OPENAI_API_KEY=<API-ключ сервисного аккаунта>
IMAGE_PROVIDER=pollinations
```

Yandex key получает только `ai-agent`. Backend, bot и publications получают общий `AI_AGENT_API_KEY`, но не `OPENAI_API_KEY`. Агент, в свою очередь, не получает admin/publisher/Fernet/Telegram/VK secrets.

Без облачного ключа используйте `LLM_PROVIDER=offline`: тексты собираются шаблоном из фактов KB. Это явно видимый план Б, а не YandexGPT.

Полный AI suite из корня после сборки image:

```bash
docker compose run --rm --no-deps \
  -v "$PWD/ai_agent:/app" -w /app \
  --entrypoint python ai-agent -m unittest discover -s tests -v
```

## Форма настройки компании (критерий 11)

Агент отдаёт форму `/admin` на `127.0.0.1:8001`: профиль, документы, согласование, проверка. Кнопка «Создать компанию» отправляет профиль и файлы в базу знаний и запускает анализ. У каждого файла выбирается тип: «Факты» или «Правила и контекст». Повторная загрузка того же файла не задваивает базу.

Если задан `AI_AGENT_API_KEY`, ключ вводится в форме и хранится только до закрытия вкладки. Compose публикует admin/docs только на loopback-интерфейсе хоста.

## Профиль компании без программирования

Тело `PUT /v1/companies/{company_id}` — JSON-профиль: тон, сегменты ЦА с CTA, ссылки на официальные источники [S1]…, правила атрибуции, запрещённые утверждения, инструкции компании и стиль изображения. Пример — `demo_data/salavatsteklo/profile.json`. Документы пакета загружаются через `/documents`.

Промпт-контракт — `marketer/prompts/*_v3.md`. Фрагменты базы знаний агент помечает [K1], [K2]…, метки [S1]… внутри документов — официальные источники компании.

## Таблицы в общей БД

Все таблицы модуля начинаются с `ai_` и создаются при старте сервиса: `ai_company_profile`, `ai_document`, `ai_chunk`, `ai_message`, `ai_decision`, `ai_draft`, `ai_action_log`.

Backend владеет своими таблицами (`companies`, `tasks`, `publications`, `message_history`, `company_knowledge_items` и другими), worker — `platform_posts` и `comment_events`. AI-agent не меняет их схему и не публикует материалы.

Внутри AI-контура идентификаторы — UUID. Backend передаёт строковый `company_id`/`external_id`: UUID используется как есть, любая другая строка детерминированно превращается в `uuid5`. Поэтому одна Backend-компания всегда попадает в один изолированный AI-профиль. Возвращённый `agent_company_id` можно использовать в `/admin` и native AI API.

Изоляция компаний: любой запрос к данным идёт через `CompanyRepo` с фильтром `company_id`. Чужой черновик для другой компании — 404; это проверяется тестами.

## Основной интеграционный контракт

`POST {AI_AGENT_URL}`; в общем Compose это `http://ai-agent:8000/v1/generate`. Те же обработчики доступны на `/generate` и `/`.

Заголовок при заданном ключе:

```http
X-Internal-Key: <AI_AGENT_API_KEY>
```

Запрос:

```json
{
  "prompt": "Подготовь пост о стекле Ultraview R для архитекторов",
  "company": {"id": 7, "company_id": "salavatsteklo", "external_id": "salavatsteklo", "name": "АО «Салаватстекло»"},
  "history": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}],
  "knowledge_base": [{"title": "О компании", "content": "до 4000 символов", "source": "https://salstek.ru/..."}]
}
```

Ответ:

```json
{
  "text": "непустой текст",
  "image_url": "https://…",
  "kind": "draft",
  "engine": "фактический LLM engine",
  "draft_id": "uuid",
  "status": "draft",
  "notes": "пояснение для клиента",
  "agent_company_id": "uuid"
}
```

`image_url` также может быть `null`; дополнительные служебные поля Backend игнорирует.

- Backend передаёт до 20 последних knowledge items именно выбранной компании; `content` каждого ограничен 4000 символами.
- Пустая или отсутствующая `knowledge_base` — не ошибка, `text` всё равно обязан быть непустым.
- `image_url` — только публичный HTTPS или `null`: Pollinations либо `AI_PUBLIC_MEDIA_BASE_URL` для опубликованного наружу агента. Offline-иллюстрация без публичного media URL → `null`.
- Промпт worker-а «черновик ответа на комментарий» → 1–3 предложения без поста и картинки, `kind=comment_reply`.
- Поля Backend `tone_of_voice`, `target_audience`, `advantages`, `description`, `products` попадают в профиль компании.
- Если компанию не настраивали через `/admin`, профиль создаётся автоматически из `company`.
- `history` принимает `{role, content}` или `{role, text}`; учитываются последние `HISTORY_LIMIT` сообщений.
- Записи `knowledge_base` сохраняются в AI KB компании; дубли пропускаются по хэшу. Удаление записи в Backend автоматически не очищает старую AI-копию.
- Ошибки: 401 — нет/неверен internal key; 422 — в `company` нет ID; 502 — модель недоступна.

Изоляция и контракт покрыты `test_backend_contract_kb_isolation`.

## Как это ложится на актуальный цикл Backend

1. Telegram-бот получает сообщение, находит компанию по chat ID, сохраняет `Task` и историю.
2. Общий Backend service `generate_content()` читает knowledge items выбранной компании и вызывает **`/v1/generate`** с `X-Internal-Key`.
3. Бот создаёт отдельные `publications` со статусом `pending_approval` для включённых площадок и отправляет материал на согласование.
4. «Доработать» повторно вызывает тот же `generate_content()` с исходным текстом и комментарием, увеличивает `approval_version` и снова требует approve.
5. После approve worker выбирает только due-записи `approved` и вызывает internal publish Backend. Только Backend обращается к VK/Telegram и меняет канонический статус.
6. Для комментариев worker вызывает тот же `/v1/generate` и получает `comment_reply`; автоматической публичной отправки ответа нет.

Native endpoints `/messages`, `/drafts`, `/revise`, `/status` остаются частью самостоятельного AI API и demo. Текущий интегрированный Backend/bot/worker использует основной `/v1/generate`; не нужно переписывать этот поток на native endpoints, чтобы «заставить агент работать».

Сам агент ничего не публикует.

## API агента

Все изменяющие/читающие company endpoints, кроме `/health`, `/admin` и `/media`, используют `X-Internal-Key`, если ключ задан.

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/v1/generate` (также `/generate`, `/`) | Основной Backend/bot/worker контракт |
| GET | `/health` | Статус и фактический LLM engine |
| GET | `/v1/engine` | Провайдеры и ограничения |
| GET | `/admin` | Форма настройки компании |
| PUT/GET | `/v1/companies/{company_id}` | Создать/обновить или получить AI-профиль |
| POST | `/v1/companies/{id}/documents` | TXT/MD/HTML/PDF/DOCX multipart |
| POST | `/v1/companies/{id}/documents/text` | Текстовый документ `{name, text, kind}` |
| POST | `/v1/companies/{id}/sites` | Загрузка сайта `{url}` |
| POST | `/v1/companies/{id}/analyze` | Анализ материалов компании |
| POST | `/v1/companies/{id}/messages` | Native AI-диалог для standalone/demo |
| POST | `/v1/companies/{id}/drafts` | Native черновик без диалога |
| POST | `/v1/companies/{id}/drafts/{draft_id}/revise` | Native новая версия по комментарию |
| POST | `/v1/companies/{id}/drafts/{draft_id}/status` | Native статус черновика |
| GET | `/v1/companies/{id}/drafts/{draft_id}` | Native черновик |
| GET | `/media/{company_id}/{file}.png` | Локальное изображение черновика |

Native ответ на `/messages`, `/drafts`, `/revise` содержит `kind`, `text`, `engine`, объект `draft` с версиями, изображениями, автопроверкой и источниками. Этот расширенный формат не следует путать с коротким интеграционным ответом `/v1/generate`, из которого Backend требует только `text` и необязательный `image_url`.

## Ограничения (для п. 8.4)

- В offline-режиме тексты собираются шаблоном из фактов KB. Это план Б, а не полноценная облачная генерация.
- Поиск по словам (BM25), без векторной модели.
- Таблицы создаются при старте сервиса; для production нужны Alembic-миграции.
- GigaChat не подключён: нужна отдельная авторизация и реализация провайдера.
- Картинки отдаются по неугадываемому адресу без авторизации; для production нужны подписанные ссылки или защищённая раздача.
- Pollinations может отвечать медленно или быть недоступен; fallback `image_url=null` нельзя выдавать за успешно сгенерированную картинку.
- Для полного запуска и живого E2E используется корневой бриф Данила; успешный offline unit suite не заменяет проверку YandexGPT.
