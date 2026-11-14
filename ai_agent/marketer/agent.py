"""Фасад AI-маркетолога (Р2). Снаружи доступен через HTTP API (marketer/api.py, AI_AGENT_URL).

Агент НИКОГДА не публикует. Он возвращает черновик (status="draft"); публикацию создаёт бэкенд (Р1),
согласование — бот Р1, отправку на площадки — модуль публикаций (Р5).
Формат content в черновике совпадает с content в контракте publication:
    {"text": ..., "images": [url], "cta_buttons": [], "source_url": null}
"""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field

from sqlalchemy.ext.asyncio import async_sessionmaker

from .checks import check_draft, dedupe_sentences, strip_marks
from .config import PLATFORMS, PROMPTS_DIR, PROMPT_VERSION, Settings
from .images import generate_image, public_image_url
from .knowledge import KnowledgeBase, format_sources
from .llm import LLMError, make_llm, parse_json
from .offline_writer import META_RE, write_post
from .storage import CompanyNotFound, CompanyRepo, Draft, upsert_company

POST_RE = re.compile(r"\b(пост\w*|публикац\w*|контент\w*|анонс\w*)", re.I)
REVISE_RE = re.compile(r"\b(доработ\w*|исправ\w*|переделай|измени\w*|убери|замени|перепиши|короче|длиннее)", re.I)
NEW_RE = re.compile(r"\b(следующ\w*|нов\w+ пост|ещё один|еще один)", re.I)
SAME_RE = re.compile(r"\b(эт|т)(от|ом|ого|ому|у|ой|а|ими?|ех?) же\b", re.I)  # этот/эту/той/тому же …
DECISION_RE = re.compile(r"\b(помни|всегда|никогда|не используй|не пиши|убери|убрать|без\s+\w+)", re.I)
COMMENT_RE = re.compile(r"ответ\w*\s+на\s+(?:этот\s+)?комментари", re.I)
AUDIENCE_RE = re.compile(r"\bдля\s+([а-яё\- ]{4,40}?)(?:[.,!?]|$| и )", re.I)
TOPIC_RE = re.compile(r"\b(?:о|об|про)\s+(.+?)(?:[.,!?]|\s+для\s|\s+в\s+(?:наш|тестов)|$)", re.I)


def _prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}_{PROMPT_VERSION}.md").read_text(encoding="utf-8").split("-->", 1)[-1].strip()


@dataclass
class AgentReply:
    kind: str                     # "draft" | "answer"
    text: str                     # что отправить пользователю в Telegram
    engine: str                   # какая модель ответила (критерий 12)
    draft: dict | None = None     # см. MarketerAgent.draft_view
    sources: list[dict] = field(default_factory=list)

    def dict(self) -> dict:
        return asdict(self)


class MarketerAgent:
    def __init__(self, settings: Settings, sessionmaker: async_sessionmaker):
        self.s = settings
        self.sm = sessionmaker
        self.llm = make_llm(settings)

    async def _repo(self, session, company_id) -> CompanyRepo:
        return await CompanyRepo(session, company_id).load()

    # ---------- администратор (вызывает бэкенд при создании/настройке компании) ----------
    async def upsert_company(self, company_id, name: str, **data) -> dict:
        data.setdefault("tone", "деловой, без рекламных преувеличений")
        data.setdefault("platform", "telegram")
        async with self.sm() as session:
            prof = await upsert_company(session, uuid.UUID(str(company_id)), name, data)
            return {"company_id": str(prof.company_id), "name": prof.name, **prof.data}

    async def get_company(self, company_id) -> dict:
        async with self.sm() as session:
            repo = await self._repo(session, company_id)
            return {"company_id": str(repo.company_id), **repo.profile, "documents": await repo.documents()}

    async def add_text(self, company_id, name: str, text: str, kind: str = "facts") -> dict:
        async with self.sm() as session:
            doc_id, dup = await KnowledgeBase(await self._repo(session, company_id)).add_text(name, text, kind=kind)
            return {"document_id": str(doc_id), "duplicate": dup}

    async def add_file(self, company_id, path) -> dict:
        async with self.sm() as session:
            doc_id, dup = await KnowledgeBase(await self._repo(session, company_id)).add_file(path)
            return {"document_id": str(doc_id), "duplicate": dup}

    async def add_upload(self, company_id, filename: str, data: bytes, kind: str = "facts") -> dict:
        async with self.sm() as session:
            doc_id, dup = await KnowledgeBase(await self._repo(session, company_id)).add_upload(filename, data, kind=kind)
            return {"document_id": str(doc_id), "duplicate": dup}

    async def add_site(self, company_id, url: str) -> dict:
        async with self.sm() as session:
            doc_id, dup = await KnowledgeBase(await self._repo(session, company_id)).add_url(url)
            return {"document_id": str(doc_id), "duplicate": dup}

    async def analyze_business(self, company_id) -> dict:
        """Изучение бизнеса (ТЗ 3.2): продукты, ЦА, преимущества, конкуренты -> в профиль."""
        async with self.sm() as session:
            repo = await self._repo(session, company_id)
            chunks = await KnowledgeBase(repo).search("продукция продукты услуги клиенты преимущества конкуренты", k=8)
            if self.llm.name == "offline":
                from .offline_writer import _facts
                facts = _facts(chunks, "продукция стекло разработки история категории", 4)
                result = {"facts": [f"{f} [{', '.join(refs)}]" for f, _, refs in facts]}
            else:
                result = parse_json(await self.llm.complete(
                    "Ты аналитик-маркетолог. Отвечай только JSON.",
                    _prompt("profile").format(company_name=repo.profile["name"], knowledge=format_sources(chunks)),
                    json_mode=True))
            await repo.update_profile(analysis=result)
            return result

    async def set_draft_status(self, company_id, draft_id, status: str) -> None:
        async with self.sm() as session:
            await (await self._repo(session, company_id)).set_draft_status(draft_id, status)

    async def get_draft(self, company_id, draft_id) -> dict:
        async with self.sm() as session:
            repo = await self._repo(session, company_id)
            return self.draft_view(await repo.get_draft(draft_id))

    # ---------- клиент в Telegram ----------
    async def handle_message(self, company_id, chat_id, text: str, task_id=None) -> AgentReply:
        async with self.sm() as session:
            repo = await self._repo(session, company_id)
            await repo.add_message(chat_id, "user", text)
            if DECISION_RE.search(text):
                await repo.add_decision(text.strip())
            last = await repo.last_draft()
            if last and last.status in ("draft", "needs_changes") and REVISE_RE.search(text) and not NEW_RE.search(text):
                reply = await self._revise(repo, last, text, chat_id)
            elif POST_RE.search(text) and not self._is_question(text):
                reply = await self._create_post(repo, text, chat_id, task_id=task_id)
            else:
                reply = await self._answer(repo, chat_id, text)
            await repo.add_message(chat_id, "assistant", reply.text)
            return reply

    async def create_post(self, company_id, request: str, *, chat_id=None, platform: str | None = None,
                          task_id=None) -> AgentReply:
        """Без диалога — для планировщика (задача 20) и для бэкенда."""
        async with self.sm() as session:
            return await self._create_post(await self._repo(session, company_id), request, chat_id,
                                           platform=platform, task_id=task_id)

    async def revise(self, company_id, draft_id, comment: str, *, chat_id=None) -> AgentReply:
        """«Вернуть на доработку с комментарием» из цикла согласования."""
        async with self.sm() as session:
            repo = await self._repo(session, company_id)
            reply = await self._revise(repo, await repo.get_draft(draft_id), comment, chat_id)
            if chat_id is not None:
                await repo.add_message(chat_id, "assistant", reply.text)
            return reply

    # ---------- финальный контракт бэкенда (Данил): prompt + company + history + knowledge_base ----------
    @staticmethod
    def company_uuid(company: dict) -> uuid.UUID:
        """company_id / external_id (строки) -> UUID нашего контура. Не UUID — детерминированный uuid5,
        чтобы одна и та же компания бэкенда всегда попадала в одну и ту же изолированную базу знаний."""
        for key in ("company_id", "external_id", "id"):
            val = company.get(key)
            if val not in (None, ""):
                try:
                    return uuid.UUID(str(val))
                except ValueError:
                    return uuid.uuid5(uuid.NAMESPACE_URL, f"kiber102:company:{val}")
        raise ValueError("в company нет ни company_id, ни external_id, ни id")

    async def generate_for_backend(self, prompt: str, company: dict, history: list | None,
                                   knowledge_base: list | None) -> dict:
        """Ответ всегда {"text": непустой, "image_url": str | None, ...служебные поля}.
        knowledge_base — записи ТОЛЬКО этой компании; кладём их в её изолированную базу (дубли пропускаются)."""
        cid = self.company_uuid(company)
        async with self.sm() as session:
            try:
                repo = await self._repo(session, cid)
            except CompanyNotFound:  # компанию не настраивали через /admin — создаём профиль из того, что прислал бэкенд
                alias = {"tone_of_voice": "tone", "target_audience": "audience", "advantages": "benefits"}
                data = {alias.get(k, k): v for k, v in company.items()
                        if k in ("industry", "audience", "tone", "description", "products", "site_url", "platform",
                                 "tone_of_voice", "target_audience", "advantages", "benefits")
                        and isinstance(v, str) and v}
                data.setdefault("tone", "деловой, без рекламных преувеличений")
                if data.get("platform") not in PLATFORMS:
                    data["platform"] = "telegram"
                await upsert_company(session, cid, str(company.get("name") or "Компания"), data)
                repo = await self._repo(session, cid)
            kb = KnowledgeBase(repo)
            for item in knowledge_base or []:
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content") or "").strip()[:4000]
                if not content:
                    continue
                title = str(item.get("title") or item.get("source") or "запись базы знаний")[:200]
                source = str(item.get("source") or "backend")[:500]
                try:
                    await kb.add_text(title, content, source=source, kind="facts")
                except ValueError:
                    pass  # запись без текста — пропускаем, не падаем
            # история из бэкенда — в отдельный чат этого запроса (у агента история хранится по чатам)
            chat_id = f"backend:{uuid.uuid4().hex[:16]}"
            for m in (history or [])[-self.s.history_limit:]:
                if isinstance(m, dict):
                    role = "assistant" if str(m.get("role", "user")).lower() in ("assistant", "bot", "ai") else "user"
                    text = str(m.get("text") or m.get("content") or m.get("message") or "").strip()
                else:
                    role, text = "user", str(m).strip()
                if text:
                    await repo.add_message(chat_id, role, text[:4000])
        prompt = (prompt or "").strip() or "Расскажи о компании"
        if COMMENT_RE.search(prompt):  # воркер Данила: черновик ответа на комментарий — без поста и картинки
            text = await self._comment_reply(cid, prompt)
            return {"text": text, "image_url": None, "kind": "comment_reply", "engine": self.llm.engine_label,
                    "draft_id": None, "status": None, "notes": None, "agent_company_id": str(cid)}
        reply = await self.handle_message(cid, chat_id, prompt)
        if reply.draft:
            # бэкенд принимает только публичный https (Telegram/VK качают картинку сами), внутренний адрес не годится
            text, image_url = reply.draft["content"]["text"], reply.draft.get("public_image_url")
        else:
            text, image_url = reply.text, None
        if not (text or "").strip():
            text = "В материалах компании пока нет сведений по этому запросу. Добавьте документы в базу знаний."
        return {"text": text, "image_url": image_url, "kind": reply.kind, "engine": reply.engine,
                "draft_id": reply.draft["draft_id"] if reply.draft else None,
                "status": reply.draft["status"] if reply.draft else None,
                "notes": reply.text if reply.draft else None,
                "agent_company_id": str(cid)}

    async def _comment_reply(self, cid, prompt: str) -> str:
        """Короткий вежливый черновик ответа на комментарий. Публикует только человек."""
        async with self.sm() as session:
            repo = await self._repo(session, cid)
            prof = repo.profile
            m = re.search(r"Комментарий:\s*«(.+?)»", prompt, re.S)
            comment = m.group(1) if m else prompt
            sources = await KnowledgeBase(repo).search(comment, k=3)
        if self.llm.name == "offline":
            return (f"Спасибо за комментарий! Мы передали ваш вопрос специалистам {prof['name']}, "
                    "ответим подробнее в ближайшее время.")
        system = (f"Ты SMM-специалист компании «{prof['name']}». Тон: {prof.get('tone', 'деловой, вежливый')}. "
                  "Пиши черновик ответа на комментарий от имени компании: 1–3 предложения, по-русски, без хэштегов. "
                  "Не обещай сроков, цен, скидок и компенсаций, не спорь, не раскрывай внутренние данные. "
                  "Факты о компании — только из фрагментов ниже; если их нет — поблагодари и предложи уточнить "
                  "в личных сообщениях.\n\nФрагменты:\n" + (format_sources(sources) or "—"))
        text = (await self.llm.complete(system, f"Комментарий: «{comment}»")).strip()
        return text or "Спасибо за обратную связь! Ответим подробнее в ближайшее время."

    # ---------- внутреннее ----------
    async def _answer(self, repo: CompanyRepo, chat_id, question: str) -> AgentReply:
        sources = await KnowledgeBase(repo).search(question, k=self.s.top_k)
        last = await repo.last_draft()
        if re.search(r"на как\w+ (сведени|данн|материал|источник)", question, re.I) and last:
            used, facts = last.data.get("sources_used", []), last.data.get("facts_used")
            if facts:  # конкретные факты из черновика + документ + официальный источник
                docs = {u["sid"]: u["doc"] for u in used}
                text = "В последнем черновике использованы факты из материалов компании:\n" + "\n".join(
                    f"• {f['fact']} — {docs.get(f['sid'], f['sid'])}" + (f", источник: {', '.join(f['urls'])}" if f["urls"] else "")
                    for f in facts)
            elif used:
                urls_map = repo.profile.get("source_urls") or {}
                lines = []
                for u in used:
                    sentences = [s for line in u["text"].splitlines() if line.strip() and not line.startswith("#")
                                 for s in re.split(r"(?<=[.!?])\s+", line.strip())]
                    factual = [s for s in sentences if re.search(r"\[S\d", s) and not META_RE.search(s)]
                    pick = (factual or sentences or [""])[0]
                    first = strip_marks(pick).strip()[:220]
                    refs = sorted(set(re.findall(r"\[(S\d+)", pick))) or sorted(set(re.findall(r"\[(S\d+)", u["text"])))
                    links = ", ".join(urls_map[r] for r in refs if r in urls_map)
                    lines.append(f"• {first} — {u['doc']}" + (f", источник: {links}" if links else ""))
                text = "Опираюсь на загруженные материалы компании:\n" + "\n".join(lines)
            else:
                text = "Источники не найдены."
            return AgentReply("answer", text, self.llm.engine_label, sources=used)
        if self.llm.name == "offline":
            text = ("Вот что есть в материалах компании по вашему вопросу:\n" +
                    "\n".join(f"• {c['text'][:220]}… [{c['sid']}]" for c in sources[:3])) if sources \
                else "В загруженных материалах ответа нет. Пришлите документ или уточните вопрос."
        else:
            text = await self.llm.complete(await self._system(repo, chat_id, sources), question)
        return AgentReply("answer", text, self.llm.engine_label, sources=sources)

    async def _create_post(self, repo: CompanyRepo, request: str, chat_id, platform=None, task_id=None) -> AgentReply:
        prof = repo.profile
        platform = platform or self._detect_platform(request, prof.get("platform", "telegram"))
        last = await repo.last_draft()
        continued = bool(SAME_RE.search(request) and last)
        topic = last.topic if continued else self._topic(request)
        segment = self._segment(prof, request) or (last.data.get("segment") if continued else None)
        focus = segment.get("focus", "") if segment else ""
        # ищем по теме и адресату, а не по всей фразе: «тестовый канал», «согласование» тянут служебные документы
        sources = await KnowledgeBase(repo).search(f"{topic} {focus}", k=self.s.top_k)
        draft = await self._generate(repo, chat_id, request, topic, segment, platform, sources)
        draft["request"], draft["segment"] = request, segment
        note = f"Продолжаю тему прошлого поста: «{topic}»." if continued else ""
        return await self._finalize(repo, topic, platform, draft, sources, version=1, task_id=task_id,
                                    context_note=note)

    async def _revise(self, repo: CompanyRepo, prev: Draft, comment: str, chat_id) -> AgentReply:
        await repo.set_draft_status(prev.id, "superseded")
        await repo.add_decision(f"Замечание к посту «{prev.topic}»: {comment.strip()}")
        d = prev.data
        kb = KnowledgeBase(repo)
        sources, seen = [], set()  # сначала то, о чём просит замечание, затем тема
        for c in await kb.search(comment, k=2) + await kb.search(prev.topic, k=self.s.top_k):
            if c["text"] not in seen:
                seen.add(c["text"])
                sources.append({**c, "sid": f"K{len(sources) + 1}"})
        segment = d.get("segment")
        draft = await self._generate(repo, chat_id, d.get("request", prev.topic), prev.topic, segment,
                                     d["platform"], sources,
                                     revision=f"Предыдущая версия:\n{d['text']}\n\nЗамечание клиента: {comment}")
        draft["request"], draft["segment"] = d.get("request", prev.topic), segment
        return await self._finalize(repo, prev.topic, d["platform"], draft, sources, version=prev.version + 1,
                                    task_id=prev.task_id, parent=prev.id, revision_note=comment)

    async def _generate(self, repo, chat_id, request, topic, segment, platform, sources, revision="") -> dict:
        prof, rules = repo.profile, PLATFORMS[platform]
        if self.llm.name == "offline":
            return write_post(topic, segment, prof, sources, rules["max_len"],
                              revision=revision.split("Замечание клиента:")[-1].strip() if revision else "")
        cta = (segment or {}).get("cta") or prof.get("default_cta", "нейтральный призыв узнать подробности")
        user = _prompt("post").format(
            request=request, platform=platform, max_len=rules["max_len"], platform_rules=rules["rules"], cta=cta,
            image_style=prof.get("image_style", "realistic illustration, no text, no logos"),
            revision_block=f"\nЭто доработка.\n{revision}\nИсправь всё, о чём просит клиент.\n" if revision else "")
        system = await self._system(repo, chat_id, sources, segment)
        try:
            draft = parse_json(await self.llm.complete(system, user, json_mode=True))
            # Самопроверка: если проверка что-то вырезала или нашла нарушение — одна попытка переписать с замечаниями
            pre = check_draft({**draft, "image_path": "-"}, [s["text"] for s in sources], rules["max_len"],
                              prof.get("attribution_rules"))
            problems = pre["fixed"] + [i for i in pre["issues"] if "лимит" not in i]
            if problems:
                await repo.log("self_review", "; ".join(problems)[:500])
                retry = (user + "\n\nПредыдущий вариант не прошёл проверку компании:\n- " + "\n- ".join(problems)
                         + "\nПерепиши пост: без оценок и обещаний, только факты из фрагментов [K…], "
                           "с правильной атрибуцией. Верни JSON того же формата.")
                try:
                    draft = parse_json(await self.llm.complete(system, retry, json_mode=True))
                    draft["self_review"] = problems
                except (LLMError, ValueError):
                    pass  # остаётся первый вариант, его исправит финальная проверка
            return draft
        except (LLMError, ValueError) as e:
            await repo.log("llm_error", str(e))
            raise

    async def _finalize(self, repo: CompanyRepo, topic, platform, draft, sources, *, version, task_id=None,
                        parent=None, revision_note="", context_note="") -> AgentReply:
        prof = repo.profile
        draft_id = uuid.uuid4()
        img_rel = f"{repo.company_id}/{draft_id}.png"
        img, img_engine = await generate_image(draft.get("image_prompt", topic), draft.get("title", topic),
                                               self.s.media_dir / img_rel, self.s, prof.get("brand_color", "#1f5f8b"))
        tags = " ".join(draft.get("hashtags", []))
        max_len = PLATFORMS[platform]["max_len"] - (len(tags) + 2 if tags else 0)
        draft.update(image_url=f"{self.s.media_base_url.rstrip('/')}/{img_rel}", image_path=str(img),
                     public_image_url=public_image_url(img, img_rel, self.s),
                     image_engine=img_engine, platform=platform, engine=self.llm.engine_label,
                     sources_used=[s for s in sources if s["sid"] in draft.get("source_ids", [])] or sources[:2])
        caption = prof.get("image_caption", "AI-иллюстрация.")
        max_len -= len(caption) + 2
        draft["image_caption"] = caption
        if not draft.get("source_url"):
            draft["source_url"] = None
        report = check_draft(draft, [s["text"] for s in sources], max_len, prof.get("attribution_rules"))
        draft["text"], draft["check"] = report["text"], report
        self._enforce_link(draft, prof, revision_note)
        row = await repo.save_draft(topic, draft, version=version, task_id=task_id, parent_id=parent,
                                    draft_id=draft_id)

        lines = [f"Черновик (версия {version}) для {platform} готов и ждёт согласования.",
                 context_note, draft.get("summary_for_client", "")]
        decisions = await repo.decisions()
        if decisions and not revision_note:
            short = [re.sub(r"^Замечание к посту «.*»: ", "к прошлому посту: ", x) for x in decisions[-2:]]
            short = [x if len(x) <= 140 else x[:137].rsplit(" ", 1)[0] + "…" for x in short]
            lines.append("Учитываю ваши прежние решения: " + "; ".join(f"«{x}»" for x in short))
        if revision_note:
            lines.append(f"Учтено замечание: «{revision_note.strip()}».")
        if report["fixed"]:
            lines.append("Автопроверка: " + "; ".join(report["fixed"]))
        if report["issues"]:
            lines.append("Требует внимания: " + "; ".join(report["issues"]))
        lines.append("Без вашего одобрения публикации не будет.")
        return AgentReply("draft", "\n".join(l for l in lines if l), self.llm.engine_label,
                          self.draft_view(row), sources)

    @staticmethod
    def _enforce_link(draft: dict, prof: dict, revision_note: str) -> None:
        """Требования, которые модель иногда пропускает, проверяем кодом, а не надеждой на промпт:
        просили ссылку на каталог или призыв сегмента содержит ссылку — она обязана быть в тексте."""
        catalog = prof.get("catalog_url")
        segment = draft.get("segment") or {}
        cta = segment.get("cta", "")
        asked = bool(re.search(r"ссылк", revision_note, re.I) and re.search(r"каталог", revision_note, re.I))
        url = catalog if asked else next((u for u in re.findall(r"https?://\S+", cta)), None)
        if not url:
            return
        url = url.rstrip(".,;")
        if url not in draft["text"]:
            line = cta if url in cta else f"Каталог продукции: {url}"
            # обязательный призыв ставим последним, а похожие призывы модели убираем, чтобы не было трёх подряд
            draft["text"] = dedupe_sentences(f"{draft['text'].rstrip()}\n\n{line}", draft["check"].setdefault("fixed", []))
            draft["check"]["fixed"].append(f"добавлена обязательная ссылка: {url}")
        draft["source_url"] = draft.get("source_url") or url

    @staticmethod
    def draft_view(row: Draft) -> dict:
        """Черновик для бэкенда: content в формате контракта publication."""
        d = row.data
        tags = " ".join(d.get("hashtags", []))
        return {
            "draft_id": str(row.id), "company_id": str(row.company_id),
            "task_id": str(row.task_id) if row.task_id else None,
            "parent_draft_id": str(row.parent_id) if row.parent_id else None,
            "version": row.version, "status": row.status, "topic": row.topic,
            "platforms": [d["platform"]],
            "content": {"text": "\n\n".join(x for x in (d["text"], tags, d.get("image_caption", "")) if x),
                        "images": [d["image_url"]], "cta_buttons": [], "source_url": d.get("source_url")},
            "audience": d.get("audience"), "image_caption": d.get("image_caption"),
            "title": d.get("title"), "hashtags": d.get("hashtags", []),
            "image_path": d.get("image_path"), "image_engine": d.get("image_engine"),
            "public_image_url": d.get("public_image_url"),
            "sources_used": d.get("sources_used", []), "check": d.get("check", {}), "engine": d.get("engine"),
        }

    async def _system(self, repo: CompanyRepo, chat_id, sources, segment: dict | None = None) -> str:
        prof = repo.profile
        hist = await repo.history(chat_id, self.s.history_limit) if chat_id is not None else []
        last = await repo.last_draft()
        hist_note = f"Последний черновик: «{last.topic}» (v{last.version}, статус {last.status})." if last else ""
        skip = {"brand_color", "segments", "source_urls", "attribution_rules", "extra_instructions", "image_style",
                "image_caption", "forbidden_claims", "analysis", "example_post", "document_kinds"}
        profile_txt = "\n".join(f"- {k}: {v}" for k, v in prof.items() if k not in skip and v)
        if segment:
            seg_txt = (f"{segment['name']}. Акцент: {segment.get('focus', '—')}. "
                       f"Призыв к действию: «{segment.get('cta', prof.get('default_cta', ''))}».")
        else:
            seg_txt = (f"Сегмент не указан — нейтральный B2B-тон, отметь это в summary_for_client. "
                       f"Возможные адресаты: {prof.get('audience', 'клиенты компании')}.")
        rules = prof.get("attribution_rules") or []
        return _prompt("system").format(
            company_name=prof["name"], profile=profile_txt, knowledge=format_sources(sources),
            extra_instructions=prof.get("extra_instructions", "—"),
            example_post=prof.get("example_post", "(не задан)"),
            source_urls="\n".join(f"[{k}] {v}" for k, v in (prof.get("source_urls") or {}).items()) or "—",
            segment=seg_txt, forbidden_claims=prof.get("forbidden_claims", "цены, гарантии, сертификаты, характеристики без источника"),
            attribution="; ".join(r.get("message", "") for r in rules) or "приписывай факты только тому, к кому они относятся в материалах",
            decisions="\n".join(f"- {d}" for d in await repo.decisions()) or "(пока нет)",
            history=("\n".join(f"{m['role']}: {m['text']}" for m in hist) + f"\n{hist_note}").strip() or "(пусто)",
            tone=prof.get("tone", "деловой"))

    @staticmethod
    def _is_question(text: str) -> bool:
        t = text.strip().lower()
        imperative = re.search(r"\b(подготовь|сделай|напиши|создай|придумай|опубликуй|составь)\b", t)
        return t.endswith("?") and not imperative

    @staticmethod
    def _detect_platform(text: str, default: str) -> str:
        t = text.lower()
        if re.search(r"\bвк\b|вконтакт", t):
            return "vk"
        if re.search(r"\bmax\b|\bмакс\b", t):
            return "max"
        if "telegram" in t or "телеграм" in t:
            return "telegram"
        return default if default in PLATFORMS else "telegram"

    @staticmethod
    def _segment(profile: dict, text: str) -> dict | None:
        """Адресат по явному указанию пользователя: сегмент из профиля или произвольное «для …»."""
        for seg in profile.get("segments") or []:
            if re.search(seg["match"], text, re.I):
                return seg
        m = AUDIENCE_RE.search(text)
        if m and not re.match(r"(наш|тестов|telegram|телеграм|вк)", m.group(1).strip(), re.I):
            return {"key": "custom", "name": m.group(1).strip(), "cta": profile.get("default_cta", "")}
        return None

    @staticmethod
    def _topic(request: str) -> str:
        m = TOPIC_RE.search(request)
        return m.group(1).strip() if m else request.strip()[:80]
