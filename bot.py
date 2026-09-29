#!/usr/bin/env python3
"""
Yunur Consult — Telegram CRM-бот для обзвона
- Несколько менеджеров
- Запись результатов звонков
- Напоминания о перезвоне
- Статистика
"""

import os
import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv()

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8664712029:AAElLvlU-ALvuuEugCnp2mXy2YR2P0M-Sp4")
DB_PATH = Path(__file__).parent / "crm.db"

# Если хочешь ограничить доступ только определённым людям —
# укажи их Telegram ID через запятую в .env (ADMIN_IDS=123456,789012)
# Если пусто — доступ есть у всех, кто написал боту
ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

# ================== ЛОГИ ==================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ================== СОСТОЯНИЯ РАЗГОВОРА ==================
(
    COMPANY,
    CONTACT,
    PHONE,
    STATUS,
    COMMENT,
    CALLBACK_TIME,
) = range(6)

# Статусы звонка
STATUSES = {
    "no_answer": "📵 Недозвон",
    "talk": "📞 Разговор состоялся",
    "interest": "🔥 Интерес / отправить тарифы",
    "callback": "⏰ Перезвонить",
    "refuse": "❌ Отказ",
    "meeting": "📅 Встреча / консультация",
    "deal": "✅ Договор",
}


# ================== БАЗА ДАННЫХ ==================
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS managers (
            telegram_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manager_id INTEGER,
            company TEXT NOT NULL,
            contact_name TEXT,
            phone TEXT,
            status TEXT NOT NULL,
            comment TEXT,
            callback_at TEXT,
            reminded INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (manager_id) REFERENCES managers(telegram_id)
        )
    """)

    conn.commit()
    conn.close()


def register_manager(user):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO managers (telegram_id, name, username)
        VALUES (?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            name = excluded.name,
            username = excluded.username
        """,
        (user.id, user.full_name, user.username or ""),
    )
    conn.commit()
    conn.close()


def is_allowed(user_id: int) -> bool:
    if not ADMIN_IDS:
        return True
    return user_id in ADMIN_IDS


# ================== КЛАВИАТУРЫ ==================
def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📞 Новый звонок"), KeyboardButton("📋 На сегодня")],
            [KeyboardButton("📊 Статистика"), KeyboardButton("📁 Все лиды")],
            [KeyboardButton("ℹ️ Помощь")],
        ],
        resize_keyboard=True,
    )


def status_keyboard():
    buttons = [
        [InlineKeyboardButton(text, callback_data=f"status:{key}")]
        for key, text in STATUSES.items()
    ]
    return InlineKeyboardMarkup(buttons)


def callback_time_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Через 1 час", callback_data="cb:1h"),
            InlineKeyboardButton("Через 3 часа", callback_data="cb:3h"),
        ],
        [
            InlineKeyboardButton("Завтра утром", callback_data="cb:tomorrow"),
            InlineKeyboardButton("Через 2 дня", callback_data="cb:2d"),
        ],
        [
            InlineKeyboardButton("Через неделю", callback_data="cb:7d"),
            InlineKeyboardButton("Не нужно", callback_data="cb:none"),
        ],
    ])


# ================== КОМАНДЫ ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("⛔ Доступ ограничен. Обратитесь к администратору.")
        return

    register_manager(user)
    await update.message.reply_text(
        f"Привет, {user.first_name}!\n\n"
        "Это CRM-бот Yunur Consult для обзвона.\n\n"
        "Что умею:\n"
        "• Записывать результаты звонков\n"
        "• Напоминать о перезвоне\n"
        "• Показывать статистику и список на сегодня\n\n"
        "Выбери действие на клавиатуре или напиши /help",
        reply_markup=main_keyboard(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Команды и кнопки*\n\n"
        "📞 *Новый звонок* — записать результат\n"
        "📋 *На сегодня* — кому нужно позвонить сегодня\n"
        "📊 *Статистика* — сводка по твоим звонкам\n"
        "📁 *Все лиды* — последние 20 записей\n\n"
        "Также можно писать команды:\n"
        "/call — новый звонок\n"
        "/today — на сегодня\n"
        "/stats — статистика\n"
        "/list — все лиды\n"
        "/cancel — отменить текущий ввод"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())


# ---------- Новый звонок (Conversation) ----------
async def call_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ ограничен.")
        return ConversationHandler.END

    register_manager(update.effective_user)
    context.user_data.clear()
    await update.message.reply_text(
        "🏢 Название компании:\n\n(или /cancel чтобы отменить)",
        reply_markup=ReplyKeyboardRemove(),
    )
    return COMPANY


async def call_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["company"] = update.message.text.strip()
    await update.message.reply_text("👤 Имя контакта (или «-» если нет):")
    return CONTACT


async def call_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["contact"] = "" if text == "-" else text
    await update.message.reply_text("📱 Телефон (или «-» если нет):")
    return PHONE


async def call_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["phone"] = "" if text == "-" else text
    await update.message.reply_text(
        "📊 Выбери результат звонка:",
        reply_markup=status_keyboard(),
    )
    return STATUS


async def call_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    status_key = query.data.split(":")[1]
    context.user_data["status"] = status_key

    await query.edit_message_text(
        f"Статус: {STATUSES[status_key]}\n\n"
        "💬 Комментарий (что сказал клиент, важные детали).\n"
        "Можно написать «-» если комментария нет:"
    )
    return COMMENT


async def call_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["comment"] = "" if text == "-" else text

    await update.message.reply_text(
        "⏰ Когда напомнить о перезвоне?",
        reply_markup=callback_time_keyboard(),
    )
    return CALLBACK_TIME


async def call_callback_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]

    now = datetime.now()
    callback_at = None

    if choice == "1h":
        callback_at = now + timedelta(hours=1)
    elif choice == "3h":
        callback_at = now + timedelta(hours=3)
    elif choice == "tomorrow":
        callback_at = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0)
    elif choice == "2d":
        callback_at = now + timedelta(days=2)
    elif choice == "7d":
        callback_at = now + timedelta(days=7)
    # none → callback_at остаётся None

    # Сохраняем в БД
    data = context.user_data
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO calls (manager_id, company, contact_name, phone, status, comment, callback_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            update.effective_user.id,
            data["company"],
            data.get("contact", ""),
            data.get("phone", ""),
            data["status"],
            data.get("comment", ""),
            callback_at.isoformat(sep=" ", timespec="minutes") if callback_at else None,
        ),
    )
    conn.commit()
    call_id = cur.lastrowid
    conn.close()

    status_text = STATUSES[data["status"]]
    cb_text = callback_at.strftime("%d.%m.%Y %H:%M") if callback_at else "не нужно"

    await query.edit_message_text(
        f"✅ Запись #{call_id} сохранена\n\n"
        f"🏢 {data['company']}\n"
        f"👤 {data.get('contact') or '—'}\n"
        f"📱 {data.get('phone') or '—'}\n"
        f"📊 {status_text}\n"
        f"💬 {data.get('comment') or '—'}\n"
        f"⏰ Перезвонить: {cb_text}"
    )

    await context.bot.send_message(
        chat_id=update.effective_user.id,
        text="Можно добавлять следующий звонок.",
        reply_markup=main_keyboard(),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Отменено.",
        reply_markup=main_keyboard(),
    )
    return ConversationHandler.END


# ---------- Списки и статистика ----------
async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    conn = get_conn()
    cur = conn.cursor()
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Кому нужно перезвонить сегодня + звонки, созданные сегодня
    cur.execute(
        """
        SELECT * FROM calls
        WHERE manager_id = ?
          AND (
                date(callback_at) = ?
                OR date(created_at) = ?
              )
        ORDER BY callback_at IS NULL, callback_at, created_at DESC
        LIMIT 50
        """,
        (update.effective_user.id, today_str, today_str),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(
            "На сегодня записей нет. Можно добавить новый звонок.",
            reply_markup=main_keyboard(),
        )
        return

    lines = ["📋 *На сегодня / активные:*\n"]
    for r in rows:
        status = STATUSES.get(r["status"], r["status"])
        cb = ""
        if r["callback_at"]:
            try:
                cb_dt = datetime.fromisoformat(r["callback_at"])
                cb = f"\n⏰ {cb_dt.strftime('%d.%m %H:%M')}"
            except Exception:
                cb = f"\n⏰ {r['callback_at']}"

        lines.append(
            f"#{r['id']} *{r['company']}*\n"
            f"{status}{cb}\n"
            f"👤 {r['contact_name'] or '—'} | 📱 {r['phone'] or '—'}\n"
            f"_{r['comment'] or ''}_\n"
        )

    text = "\n".join(lines)
    # Telegram limit ~4096
    if len(text) > 4000:
        text = text[:4000] + "\n…"

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())


async def list_calls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM calls
        WHERE manager_id = ?
        ORDER BY created_at DESC
        LIMIT 20
        """,
        (update.effective_user.id,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Пока записей нет.", reply_markup=main_keyboard())
        return

    lines = ["📁 *Последние 20 записей:*\n"]
    for r in rows:
        status = STATUSES.get(r["status"], r["status"])
        lines.append(
            f"#{r['id']} {r['company']} — {status}\n"
            f"📱 {r['phone'] or '—'} | {r['created_at'][:16]}\n"
        )

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=main_keyboard()
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "SELECT status, COUNT(*) as cnt FROM calls WHERE manager_id = ? GROUP BY status",
        (update.effective_user.id,),
    )
    by_status = {r["status"]: r["cnt"] for r in cur.fetchall()}

    cur.execute(
        "SELECT COUNT(*) as cnt FROM calls WHERE manager_id = ?",
        (update.effective_user.id,),
    )
    total = cur.fetchone()["cnt"]

    # За сегодня
    today_str = datetime.now().strftime("%Y-%m-%d")
    cur.execute(
        "SELECT COUNT(*) as cnt FROM calls WHERE manager_id = ? AND date(created_at) = ?",
        (update.effective_user.id, today_str),
    )
    today_cnt = cur.fetchone()["cnt"]

    # Ожидают перезвона
    cur.execute(
        """
        SELECT COUNT(*) as cnt FROM calls
        WHERE manager_id = ? AND callback_at IS NOT NULL AND reminded = 0
          AND datetime(callback_at) >= datetime('now')
        """,
        (update.effective_user.id,),
    )
    pending = cur.fetchone()["cnt"]

    conn.close()

    lines = [
        "📊 *Твоя статистика*\n",
        f"Всего записей: *{total}*",
        f"Сегодня: *{today_cnt}*",
        f"Ожидают перезвона: *{pending}*\n",
        "*По статусам:*",
    ]
    for key, title in STATUSES.items():
        cnt = by_status.get(key, 0)
        if cnt:
            lines.append(f"{title}: {cnt}")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=main_keyboard()
    )


# ---------- Напоминания (фоновая задача) ----------
async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Проверяет, кому пора напомнить о перезвоне."""
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    cur.execute(
        """
        SELECT * FROM calls
        WHERE callback_at IS NOT NULL
          AND reminded = 0
          AND datetime(callback_at) <= datetime(?)
        """,
        (now,),
    )
    rows = cur.fetchall()

    for r in rows:
        try:
            status = STATUSES.get(r["status"], r["status"])
            text = (
                f"⏰ *Напоминание о перезвоне*\n\n"
                f"#{r['id']} *{r['company']}*\n"
                f"👤 {r['contact_name'] or '—'}\n"
                f"📱 {r['phone'] or '—'}\n"
                f"Статус: {status}\n"
                f"💬 {r['comment'] or '—'}"
            )
            await context.bot.send_message(
                chat_id=r["manager_id"],
                text=text,
                parse_mode="Markdown",
            )
            cur.execute("UPDATE calls SET reminded = 1 WHERE id = ?", (r["id"],))
        except Exception as e:
            logger.error(f"Не удалось отправить напоминание #{r['id']}: {e}")

    conn.commit()
    conn.close()


# ---------- Обработка кнопок меню ----------
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📞 Новый звонок":
        return await call_start(update, context)
    if text == "📋 На сегодня":
        await today(update, context)
        return ConversationHandler.END
    if text == "📊 Статистика":
        await stats(update, context)
        return ConversationHandler.END
    if text == "📁 Все лиды":
        await list_calls(update, context)
        return ConversationHandler.END
    if text == "ℹ️ Помощь":
        await help_cmd(update, context)
        return ConversationHandler.END
    return ConversationHandler.END


# ================== ЗАПУСК ==================
def main():
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬ_ТОКЕН_СЮДА":
        print("❌ Укажи токен бота в файле .env (BOT_TOKEN=...) или прямо в bot.py")
        return

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Диалог добавления звонка
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("call", call_start),
            MessageHandler(filters.Regex("^📞 Новый звонок$"), call_start),
        ],
        states={
            COMPANY: [MessageHandler(filters.TEXT & ~filters.COMMAND, call_company)],
            CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, call_contact)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, call_phone)],
            STATUS: [CallbackQueryHandler(call_status, pattern=r"^status:")],
            COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, call_comment)],
            CALLBACK_TIME: [CallbackQueryHandler(call_callback_time, pattern=r"^cb:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("list", list_calls))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(conv)

    # Остальные кнопки меню (когда не в диалоге)
    app.add_handler(MessageHandler(
        filters.Regex("^(📋 На сегодня|📊 Статистика|📁 Все лиды|ℹ️ Помощь)$"),
        menu_router,
    ))

    # Напоминания каждые 60 секунд
    if app.job_queue:
        app.job_queue.run_repeating(check_reminders, interval=60, first=10)
        logger.info("JobQueue: напоминания включены")
    else:
        logger.warning("JobQueue недоступен — напоминания не будут работать. Установи: pip install 'python-telegram-bot[job-queue]'")

    logger.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
