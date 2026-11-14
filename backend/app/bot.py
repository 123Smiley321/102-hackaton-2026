"""Telegram bot: free-form task intake, conversation history and notifications.

Run independently from FastAPI with: python -m app.bot
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from types import SimpleNamespace
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ForceReply,
)
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import AuditLog, Company, MessageHistory, Publication, Task
from app.services import (
    TELEGRAM_HISTORY_LIMIT,
    generate_content,
    get_history,
    get_history_for_display,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(settings.telegram_bot_token) if settings.telegram_bot_token else None
dp = Dispatcher()


def approval_keyboard(pub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Одобрить и опубликовать", callback_data=f"approve:{pub_id}")],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{pub_id}")],
            [InlineKeyboardButton(text="✏️ Доработать", callback_data=f"revise:{pub_id}")],
        ]
    )


async def find_company_for_chat(chat_id: str) -> Company | None:
    async with SessionLocal() as db:
        result = await db.execute(
            select(Company).where(
                Company.telegram_chat_id == chat_id,
                Company.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()


async def send_long_message(target_chat_id: int, text: str, reply_markup=None) -> None:
    """Send text safely within Telegram's 4096-character message limit."""
    if bot is None:
        return
    chunks = [text[i : i + 3900] for i in range(0, len(text), 3900)] or [" "]
    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        await bot.send_message(target_chat_id, chunk, reply_markup=markup)


async def notify_approval_chat(
    company: Company,
    publication: Publication,
) -> None:
    """Send one platform-specific draft with its schedule and approval controls."""
    if bot is None:
        return
    target_value = company.approval_chat_id or company.telegram_chat_id
    if not target_value:
        raise RuntimeError("Для компании не задан чат согласования")
    target = int(target_value)
    platform_names = {"vk": "ВКонтакте", "telegram": "Telegram", "max": "MAX"}
    if publication.scheduled_at:
        scheduled = publication.scheduled_at
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        schedule_text = scheduled.astimezone().strftime("%d.%m.%Y %H:%M %Z")
    else:
        schedule_text = "Сразу после одобрения"
    header = (
        f"📝 Согласование публикации №{publication.id}\n"
        f"Компания: «{company.name}»\n"
        f"Площадка: {platform_names.get(publication.platform, publication.platform)}\n"
        f"Время: {schedule_text}\n"
        f"Версия: {publication.approval_version}\n\n"
    )
    if publication.image_url:
        await bot.send_photo(target, photo=publication.image_url)
    await send_long_message(
        target,
        header + publication.text,
        reply_markup=approval_keyboard(publication.id),
    )


@dp.message(CommandStart())
async def start(message: Message) -> None:
    chat_id = str(message.chat.id)
    company = await find_company_for_chat(chat_id)
    if company:
        await message.answer(
            f"Здравствуйте! Чат подключён к компании «{company.name}».\n\n"
            "Напишите поручение обычным сообщением — я сохраню контекст диалога "
            "и передам задачу AI-маркетологу.\n"
            "Команда /history покажет последние сообщения, /help — подсказку."
        )
    else:
        await message.answer(
            "Здравствуйте! Я AI-маркетолог. Чтобы начать работу, администратор "
            "должен привязать этот чат к профилю компании.\n\n"
            f"Ваш chat_id: {chat_id}\n"
            "Передайте его администратору компании."
        )


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "Как пользоваться ботом:\n"
        "• отправляйте поручения обычным текстом;\n"
        "• уточняйте предыдущую задачу — история диалога будет передана агенту;\n"
        "• /history — последние сообщения;\n"
        "• /start — проверить подключение компании.\n\n"
        "При успешной обработке бот сообщит результат. Если AI-сервис недоступен, "
        "вы получите уведомление об ошибке."
    )


@dp.message(Command("history"))
async def history_command(message: Message) -> None:
    chat_id = str(message.chat.id)
    async with SessionLocal() as db:
        company_result = await db.execute(
            select(Company).where(
                Company.telegram_chat_id == chat_id,
                Company.is_active.is_(True),
            )
        )
        company = company_result.scalar_one_or_none()
        if company is None:
            await message.answer(
                f"Чат не подключён к компании. Ваш chat_id: {chat_id}"
            )
            return

        rows = await get_history_for_display(
            db, company.id, chat_id, limit=TELEGRAM_HISTORY_LIMIT
        )

    if not rows:
        await message.answer("История пока пуста. Отправьте первое поручение.")
        return

    lines = [f"🕘 Последние сообщения — компания «{company.name}»:\n"]
    for row in rows:
        role = "Вы" if row.role == "user" else "AI-маркетолог"
        content = row.content.replace("\n", " ").strip()
        if len(content) > 500:
            content = content[:497] + "..."
        lines.append(f"{role}: {content}")
    await send_long_message(message.chat.id, "\n\n".join(lines))


@dp.message(F.text)
async def handle_text(message: Message) -> None:
    if bot is None:
        await message.answer("Бот временно не настроен. Обратитесь к администратору.")
        return

    prompt = (message.text or "").strip()
    # A reply to the ForceReply prompt is a revision instruction, not a new task.
    reply_text = (message.reply_to_message.text or "") if message.reply_to_message else ""
    if reply_text.startswith("Напишите одним сообщением, что нужно изменить в публикации №"):
        try:
            revision_pub_id = int(reply_text.rsplit("№", 1)[1].rstrip("."))
        except (TypeError, ValueError):
            await message.answer("Не удалось определить публикацию. Нажмите «Доработать» ещё раз.")
            return
        if not prompt:
            await message.answer("Комментарий не должен быть пустым.")
            return
        if len(prompt) > 2000:
            await message.answer("Комментарий слишком длинный. Максимум 2000 символов.")
            return
        async with SessionLocal() as db:
            pub = await db.get(Publication, revision_pub_id)
            if pub is None or pub.status != "pending_approval":
                await message.answer("Эта публикация уже обработана или не найдена.")
                return
            company = await db.get(Company, pub.company_id)
            if company is None:
                await message.answer("Не найдена компания публикации.")
                return
            expected_chat = str(company.approval_chat_id or company.telegram_chat_id or "")
            if expected_chat != str(message.chat.id):
                await message.answer("Этот чат не имеет права изменять публикацию.")
                return
            pub.status = "revision_requested"
            pub.revision_comment = prompt
            db.add(AuditLog(
                company_id=pub.company_id, actor=str(message.from_user.id),
                action="revision_requested", object_type="publication",
                object_id=str(pub.id), details=prompt,
            ))
            company_snapshot = {
                "id": company.id, "name": company.name, "description": company.description,
                "tone_of_voice": company.tone_of_voice, "products": company.products,
                "target_audience": company.target_audience, "advantages": company.advantages,
                "approval_chat_id": company.approval_chat_id, "telegram_chat_id": company.telegram_chat_id,
                "external_id": company.external_id,
            }
            old_text = pub.text
            old_image = pub.image_url
            platform = pub.platform
            task_id = pub.task_id
            await db.commit()
        await message.answer(f"✏️ Комментарий принят. Дорабатываю публикацию №{revision_pub_id}…")
        try:
            revised = await generate_content(
                SimpleNamespace(**company_snapshot),
                "Доработай публикацию для площадки " + platform + ".\n"
                "Исходный текст:\n" + old_text + "\n\nКомментарий ответственного:\n" + prompt +
                "\nВерни обновлённый текст публикации с учётом комментария.",
                [],
            )
            async with SessionLocal() as db:
                pub = await db.get(Publication, revision_pub_id)
                company = await db.get(Company, company_snapshot["id"])
                if pub is None or company is None or pub.status != "revision_requested":
                    await message.answer("Статус публикации изменился. Повторите согласование вручную.")
                    return
                pub.text = revised["text"]
                pub.image_url = revised.get("image_url") or old_image
                pub.status = "pending_approval"
                pub.approval_version += 1
                pub.approved_by = None
                pub.approved_at = None
                if company.approval_mode == "all_platforms":
                    siblings_result = await db.execute(
                        select(Publication).where(Publication.task_id == task_id)
                    )
                    for sibling in siblings_result.scalars().all():
                        if sibling.id != pub.id and sibling.status in {"approved_waiting", "approved"}:
                            sibling.status = "pending_approval"
                            sibling.approved_by = None
                            sibling.approved_at = None
                db.add(AuditLog(
                    company_id=pub.company_id, actor="system", action="revision_completed",
                    object_type="publication", object_id=str(pub.id),
                    details=f"approval_version={pub.approval_version}",
                ))
                await db.commit()
                await db.refresh(pub)
                await db.refresh(company)
                await notify_approval_chat(company, pub)
            await message.answer(
                f"✅ Доработка публикации №{revision_pub_id} готова. "
                "Новая версия отправлена на повторное согласование. До одобрения она не публикуется."
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Revision failed for publication %s", revision_pub_id)
            async with SessionLocal() as db:
                pub = await db.get(Publication, revision_pub_id)
                if pub is not None and pub.status == "revision_requested":
                    pub.status = "pending_approval"
                    db.add(AuditLog(
                        company_id=pub.company_id, actor="system", action="revision_failed",
                        object_type="publication", object_id=str(pub.id), details=type(exc).__name__,
                    ))
                    await db.commit()
            await message.answer("⚠️ Не удалось автоматически доработать материал. Черновик сохранён; передайте комментарий автору контента.")
        return
    if not prompt:
        await message.answer("Напишите поручение текстом.")
        return
    if len(prompt) > 4000:
        await message.answer("Сообщение слишком длинное. Отправьте поручение до 4000 символов.")
        return

    chat_id = str(message.chat.id)
    task_id: int | None = None
    company_snapshot: dict | None = None
    history: list[dict[str, str]] = []

    # Persist the task and user message before the network call. This way, the
    # conversation survives bot/agent restarts and failures.
    async with SessionLocal() as db:
        result = await db.execute(
            select(Company).where(
                Company.telegram_chat_id == chat_id,
                Company.is_active.is_(True),
            )
        )
        company = result.scalar_one_or_none()
        if company is None:
            await message.answer(
                "Чат не привязан к компании. Передайте администратору ваш chat_id: "
                f"{chat_id}"
            )
            return

        task = Task(
            company_id=company.id,
            prompt=prompt,
            source="telegram",
            status="generating",
        )
        db.add(task)
        db.add(
            MessageHistory(
                company_id=company.id,
                chat_id=chat_id,
                role="user",
                content=prompt,
            )
        )
        await db.flush()
        task_id = task.id
        history = await get_history(db, company.id, chat_id)
        company_snapshot = {
            "id": company.id,
            "external_id": company.external_id,
            "name": company.name,
            "description": company.description,
            "tone_of_voice": company.tone_of_voice,
            "products": company.products,
            "target_audience": company.target_audience,
            "advantages": company.advantages,
            "approval_chat_id": company.approval_chat_id,
            "telegram_chat_id": company.telegram_chat_id,
        }
        await db.commit()

    await message.answer(f"Приняла задачу №{task_id}. Подготавливаю материал…")
    await bot.send_chat_action(message.chat.id, action="typing")

    # Do not hold a database connection while waiting for the AI-agent.
    try:
        agent_company = SimpleNamespace(**company_snapshot)
        generated = await generate_content(agent_company, prompt, history)
    except Exception as exc:  # noqa: BLE001 - details stay in server logs
        logger.exception("AI processing for task %s failed", task_id)
        safe_error = (
            "Не удалось обработать задачу. Проверьте подключение AI-агента "
            "или повторите запрос позже."
        )
        async with SessionLocal() as db:
            task = await db.get(Task, task_id)
            if task is not None:
                task.status = "error"
                db.add(
                    MessageHistory(
                        company_id=task.company_id,
                        chat_id=chat_id,
                        role="assistant",
                        content=f"Не удалось выполнить задачу №{task_id}: {safe_error}",
                    )
                )
                db.add(
                    AuditLog(
                        company_id=task.company_id,
                        actor="system",
                        action="task_failed",
                        object_type="task",
                        object_id=str(task.id),
                        details=type(exc).__name__,
                    )
                )
                await db.commit()
        await message.answer(f"⚠️ Задача №{task_id}: {safe_error}")
        return

    # Persist the generated draft before trying to notify the approver.
    publication_id: int | None = None
    try:
        async with SessionLocal() as db:
            company = await db.get(Company, company_snapshot["id"])
            task = await db.get(Task, task_id)
            if company is None or task is None:
                raise RuntimeError("Company profile or task row not found while saving the result")

            platforms = list(company.enabled_platforms or ["vk"])
            if not platforms:
                platforms = ["vk"]
            publications = []
            for platform in platforms:
                publication = Publication(
                    company_id=company.id,
                    task_id=task.id,
                    text=str(generated["text"]),
                    image_url=generated.get("image_url"),
                    platform=platform,
                    idempotency_key=hashlib.sha256(
                        f"{company.id}:{task.id}:{platform}:{prompt}".encode("utf-8")
                    ).hexdigest(),
                )
                db.add(publication)
                publications.append(publication)
            task.status = "pending_approval"
            db.add(
                MessageHistory(
                    company_id=company.id,
                    chat_id=chat_id,
                    role="assistant",
                    content=str(generated["text"]),
                )
            )
            db.add(
                AuditLog(
                    company_id=company.id,
                    actor=chat_id,
                    action="content_generated",
                    object_type="task",
                    object_id=str(task.id),
                    details="Черновик создан и сохранён для согласования",
                )
            )
            await db.commit()
            for publication in publications:
                await db.refresh(publication)
            publication_id = publications[0].id
            approval_publication_ids = [publication.id for publication in publications]
            approval_snapshot = {
                "name": company.name,
                "approval_chat_id": company.approval_chat_id,
                "telegram_chat_id": company.telegram_chat_id,
                "publication_ids": approval_publication_ids,
            }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Saving generated result for task %s failed", task_id)
        safe_error = "AI подготовил ответ, но не удалось сохранить результат в базе данных. Обратитесь к администратору."
        async with SessionLocal() as db:
            task = await db.get(Task, task_id)
            if task is not None:
                task.status = "error"
                db.add(
                    MessageHistory(
                        company_id=task.company_id,
                        chat_id=chat_id,
                        role="assistant",
                        content=f"Не удалось сохранить результат задачи №{task_id}: {safe_error}",
                    )
                )
                db.add(
                    AuditLog(
                        company_id=task.company_id,
                        actor="system",
                        action="task_result_save_failed",
                        object_type="task",
                        object_id=str(task.id),
                        details=type(exc).__name__,
                    )
                )
                await db.commit()
        await message.answer(f"⚠️ Задача №{task_id}: {safe_error}")
        return

    # Notification failure must not roll back or mislabel an already-saved draft.
    try:
        async with SessionLocal() as db:
            saved_company = await db.get(Company, company_snapshot["id"])
            if saved_company is None:
                raise RuntimeError("Saved company profile not found")
            for saved_publication_id in approval_snapshot["publication_ids"]:
                saved_publication = await db.get(Publication, saved_publication_id)
                if saved_publication is None:
                    raise RuntimeError("Saved publication not found")
                await notify_approval_chat(saved_company, saved_publication)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Approval notification for publication %s failed", publication_id)
        async with SessionLocal() as db:
            db.add(
                AuditLog(
                    company_id=company_snapshot["id"],
                    actor="system",
                    action="approval_notification_failed",
                    object_type="publication",
                    object_id=str(publication_id),
                    details=type(exc).__name__,
                )
            )
            await db.commit()
        await message.answer(
            f"⚠️ Задача №{task_id} обработана, черновик сохранён (публикация №{publication_id}), "
            "но не удалось отправить уведомление ответственному. Администратору нужно проверить чат согласования."
        )
        return

    approval_target = str(approval_snapshot["approval_chat_id"] or chat_id)
    if approval_target != chat_id:
        await message.answer(
            f"✅ Задача №{task_id} выполнена. Черновик отправлен ответственному на согласование. "
            "До одобрения публикация не размещается."
        )
    else:
        await message.answer(
            f"✅ Задача №{task_id} обработана. Черновик отправлен выше на согласование."
        )


# Approval is a state transition only. The publications worker owns delivery to
# VK/Telegram and polls rows with status="approved".
async def get_publication_for_approver(db, pub_id: int, call: CallbackQuery):
    result = await db.execute(
        select(Publication).where(Publication.id == pub_id).with_for_update()
    )
    pub = result.scalar_one_or_none()
    if pub is None:
        return None, "Публикация не найдена"
    company = await db.get(Company, pub.company_id)
    if company is None:
        return None, "Компания не найдена"
    expected_chat = str(company.approval_chat_id or company.telegram_chat_id or "")
    actual_chat = str(call.message.chat.id) if call.message else ""
    if not expected_chat or expected_chat != actual_chat:
        return None, "Этот чат не имеет права согласовывать публикацию"
    return pub, None


@dp.callback_query(F.data.startswith("approve:"))
async def approve(call: CallbackQuery) -> None:
    try:
        pub_id = int(call.data.split(":", 1)[1])
    except (TypeError, ValueError, AttributeError):
        await call.answer("Некорректный ID публикации", show_alert=True)
        return

    result_message = ""
    async with SessionLocal() as db:
        pub, error = await get_publication_for_approver(db, pub_id, call)
        if error:
            await call.answer(error, show_alert=True)
            return
        if pub.status != "pending_approval":
            await call.answer(f"Нельзя одобрить: статус {pub.status}", show_alert=True)
            return
        company = await db.get(Company, pub.company_id)
        pub.approved_by = str(call.from_user.id)
        pub.approved_at = datetime.now(timezone.utc)
        if company and company.approval_mode == "all_platforms":
            pub.status = "approved_waiting"
            siblings_result = await db.execute(
                select(Publication).where(Publication.task_id == pub.task_id).with_for_update()
            )
            siblings = list(siblings_result.scalars().all())
            if siblings and all(item.status in {"approved_waiting", "approved"} for item in siblings):
                for item in siblings:
                    item.status = "approved"
                result_message = "Все площадки одобрены. Публикации переданы в очередь."
            else:
                result_message = "Одобрено для этой площадки. Ожидается согласование остальных площадок."
        else:
            pub.status = "approved"
            result_message = "Публикация одобрена и передана в очередь."
        db.add(AuditLog(
            company_id=pub.company_id, actor=str(call.from_user.id), action="approved",
            object_type="publication", object_id=str(pub.id),
            details=f"platform={pub.platform}; status={pub.status}; approval_version={pub.approval_version}",
        ))
        await db.commit()

    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.warning("Could not remove approval keyboard for publication %s", pub_id)
    await call.message.answer(f"✅ Публикация №{pub_id}: {result_message}")
    await call.answer("Решение сохранено")


@dp.callback_query(F.data.startswith("reject:"))
async def reject(call: CallbackQuery) -> None:
    try:
        pub_id = int(call.data.split(":", 1)[1])
    except (TypeError, ValueError, AttributeError):
        await call.answer("Некорректный ID публикации", show_alert=True)
        return
    async with SessionLocal() as db:
        pub, error = await get_publication_for_approver(db, pub_id, call)
        if error:
            await call.answer(error, show_alert=True)
            return
        if pub.status != "pending_approval":
            await call.answer(f"Нельзя отклонить: статус {pub.status}", show_alert=True)
            return
        pub.status = "rejected"
        db.add(AuditLog(
            company_id=pub.company_id, actor=str(call.from_user.id), action="rejected",
            object_type="publication", object_id=str(pub.id), details=f"platform={pub.platform}",
        ))
        await db.commit()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("❌ Публикация отклонена. В социальную сеть ничего не отправлено.")
    await call.answer("Отклонено")


@dp.callback_query(F.data.startswith("revise:"))
async def revise(call: CallbackQuery) -> None:
    try:
        pub_id = int(call.data.split(":", 1)[1])
    except (TypeError, ValueError, AttributeError):
        await call.answer("Некорректный ID публикации", show_alert=True)
        return
    async with SessionLocal() as db:
        pub, error = await get_publication_for_approver(db, pub_id, call)
        if error:
            await call.answer(error, show_alert=True)
            return
        if pub.status != "pending_approval":
            await call.answer(f"Нельзя отправить на доработку: статус {pub.status}", show_alert=True)
            return
    await call.message.answer(
        f"Напишите одним сообщением, что нужно изменить в публикации №{pub_id}.",
        reply_markup=ForceReply(selective=True, input_field_placeholder="Комментарий к доработке"),
    )
    await call.answer("Ожидаю комментарий")


async def run_bot() -> None:
    if bot is None:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан. Добавьте токен в .env.")
    logger.info("Starting Telegram polling")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run_bot())
