# bot.py — Webhook-ready Notify Bot (Original flow preserved, webhook+timezone fixes)
# -------------------------------------------------------------------
# Uses python-telegram-bot v21.6 (async), aiohttp for ping.
# Webhook path is: /webhook/<BOT_TOKEN>
# If WEBHOOK_URL env set -> webhook mode. Otherwise polling.
# -------------------------------------------------------------------

import asyncio
import os
import logging
import sqlite3
import json
import base64
from datetime import datetime, timedelta
from dotenv import load_dotenv
import requests



from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler

# timezone handling (stdlib)
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# ===============================================================
# Logging
# ===============================================================
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.ERROR
)

# ===============================================================
# Env
# ===============================================================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
FORCED_CHANNEL = os.getenv("FORCED_CHANNEL")
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)

# Webhook host (public) — set this to your site (Render URL) if you want webhook
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()

# Backup / GitHub (optional)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USER = os.getenv("GITHUB_USER")
GITHUB_REPO = os.getenv("GITHUB_REPO")
BACKUP_FILE = os.getenv("BACKUP_FILE", "backup.json")

# DB path
DB_PATH = os.getenv("DB_PATH", "bot.db")

# TIMEZONE (default Asia/Dhaka). You can set TZ env to other IANA zone.
TZ = os.getenv("TZ", "Asia/Dhaka")

# derive tzinfo if possible
_tzinfo = None
if ZoneInfo:
    try:
        _tzinfo = ZoneInfo(TZ)
    except Exception:
        logging.error("Invalid TZ '%s', falling back to system timezone", TZ)
        _tzinfo = None

# ===============================================================
# Admin helper
# ===============================================================
def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

# ===============================================================
# SQLite DB init
# ===============================================================
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER UNIQUE,
    lang TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    message TEXT,
    schedule_type TEXT,
    time_value TEXT,
    repeat INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active'
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER,
    job_id TEXT
)
""")

conn.commit()

# ===============================================================
# LANGUAGE TEXTS + Translator Function (t)
# (Kept original user-facing texts for BN and EN)
# ===============================================================
LANG = {
    "bn": {
        "force_join_text": "🚫 বট ব্যবহার করতে হলে আমাদের চ্যানেলে Join করুন।\n👇 নিচের বোতাম ব্যবহার করুন:",
        "select_lang_first": "🔰 প্রথমে আপনার ভাষা নির্বাচন করুন (/start)।",
        "choose_type": "🕹 রিমাইন্ডার টাইপ নির্বাচন করুন:",
        "enter_min_hour": "⏱ *Minutes/Hours Selected*\nউদাহরণ: `2m`, `10m`, `1h`",
        "wrong_format": "⚠️ ভুল ফরম্যাট। উদাহরণ: 2m / 1h",
        "enter_message": "✍ এখন Reminder-এর মেসেজ লিখুন:",
        "date_prompt": "📅 তারিখ লিখুন (Format: 15/11/25)",
        "time_prompt": "⏱ সময় লিখুন (Format: 10.15 PM)",
        "enter_message_date": "✍ রিমাইন্ডারের মেসেজ লিখুন:",
        "start_ready": "✔ এখন আপনি বট ব্যবহার করতে পারবেন।",
        "daily_single_time_prompt": "⏱ প্রতিদিন কোন একটি সময় চান?\nউদাহরণ: 10.00 AM",
        "daily_multi_time_prompt": "⏱ প্রতিদিন কোন কোন সময় চান?\nপ্রতিটি টাইম নতুন লাইনে লিখুন:\nউদাহরণ:\n10.00 AM\n01.30 PM",
        "wrong_time_format": "⚠️ সময় ফরম্যাট ভুল। উদাহরণ: 10.20 PM",
        "enter_message_daily": "✍ Daily Reminder-এর মেসেজ লিখুন:"
    },

    "en": {
        "force_join_text": "🚫 Please join our channel to use this bot.\n👇 Use the buttons below:",
        "select_lang_first": "🔰 Please select your language first (/start).",
        "choose_type": "🕹 Choose reminder type:",
        "enter_min_hour": "⏱ *Minutes/Hours Selected*\nExamples: `2m`, `10m`, `1h`",
        "wrong_format": "⚠️ Wrong format. Example: 2m / 1h",
        "enter_message": "✍ Now type the reminder message:",
        "date_prompt": "📅 Enter date (Format: 15/11/25)",
        "time_prompt": "⏱ Enter time (Format: 10.15 PM)",
        "enter_message_date": "✍ Enter reminder message:",
        "start_ready": "✔ You're now ready to use the bot.",
        "daily_single_time_prompt": "⏱ Enter the daily time:\nExample: 10.00 AM",
        "daily_multi_time_prompt": "⏱ Enter multiple times (each on new line):",
        "wrong_time_format": "⚠️ Wrong time format. Example: 10.20 PM",
        "enter_message_daily": "✍ Enter daily reminder message:"
    }
}

def t(uid, key):
    lang = get_lang(uid)
    if not lang:
        lang = "bn"  # default
    return LANG.get(lang, LANG["bn"]).get(key, f"{{Missing:{key}}}")

# ===============================================================
# DB helper functions
# ===============================================================
def save_lang(uid, lang):
    cursor.execute("INSERT OR REPLACE INTO users (user_id, lang) VALUES (?,?)",
                   (uid, lang))
    conn.commit()

def get_lang(uid):
    cursor.execute("SELECT lang FROM users WHERE user_id=?", (uid,))
    d = cursor.fetchone()
    return d[0] if d else None

def save_reminder(uid, msg, stype, tval, rep):
    cursor.execute("""
        INSERT INTO reminders (user_id, message, schedule_type, time_value, repeat)
        VALUES (?,?,?,?,?)
    """, (uid, msg, stype, tval, rep))
    conn.commit()
    return cursor.lastrowid

def set_completed(rem_id):
    cursor.execute("UPDATE reminders SET status='completed' WHERE id=?", (rem_id,))
    conn.commit()

def add_job_map(rem_id, job_id):
    cursor.execute("INSERT INTO scheduled_jobs(reminder_id, job_id) VALUES (?,?)",
                   (rem_id, job_id))
    conn.commit()

def get_jobs(rem_id):
    cursor.execute("SELECT job_id FROM scheduled_jobs WHERE reminder_id=?",
                   (rem_id,))
    return [i[0] for i in cursor.fetchall()]

def remove_mapping(rem_id):
    cursor.execute("DELETE FROM scheduled_jobs WHERE reminder_id=?",
                   (rem_id,))
    conn.commit()

def get_user_reminders(uid):
    cursor.execute("""
        SELECT id, message, schedule_type, time_value, repeat, status
        FROM reminders WHERE user_id=?
    """, (uid,))
    return cursor.fetchall()

# ===============================================================
# RELOAD — Load all reminders back to APScheduler on restart
# (keeps original behavior — but makes datetime parsing tz-aware if tz available)
# ===============================================================
def reload_scheduled_jobs(app=None):
    cursor.execute("""
        SELECT id, user_id, message, schedule_type, time_value, repeat
        FROM reminders
        WHERE status='active'
    """)
    rows = cursor.fetchall()

    for rem_id, uid, msg, stype, tval, rep in rows:

        # ONE-TIME → MIN/HOUR Reminder
        if stype == "min_hour":
            try:
                seconds = int(tval[:-1]) * (60 if tval.endswith("m") else 3600)
                run_time = (datetime.now(tz=_tzinfo) + timedelta(seconds=seconds)) if _tzinfo else (datetime.now() + timedelta(seconds=seconds))

                job = scheduler.add_job(
                    send_reminder,
                    trigger="date",
                    run_date=run_time,
                    kwargs={
                        "user_id": uid,
                        "message": msg,
                        "rem_id": rem_id
                    }
                )

                add_job_map(rem_id, job.id)

            except Exception as e:
                logging.error("Reload MIN/HOUR error: %s", e)

        # ONE-TIME → DATE Reminder
        elif stype == "date":
            try:
                dt_naive = datetime.strptime(tval, "%d/%m/%y %I.%M %p")
                dt = dt_naive.replace(tzinfo=_tzinfo) if _tzinfo else dt_naive

                if dt > (datetime.now(tz=_tzinfo) if _tzinfo else datetime.now()):
                    job = scheduler.add_job(
                        send_reminder,
                        trigger="date",
                        run_date=dt,
                        kwargs={
                            "user_id": uid,
                            "message": msg,
                            "rem_id": rem_id
                        }
                    )
                    add_job_map(rem_id, job.id)

            except Exception as e:
                logging.error("Reload DATE error: %s", e)

        # DAILY Reminder → cron jobs
        elif stype == "daily":
            try:
                times = tval.split(";")

                for tstr in times:
                    dt_obj = datetime.strptime(tstr, "%I.%M %p")
                    hour   = dt_obj.hour
                    minute = dt_obj.minute

                    if _tzinfo:
                        job = scheduler.add_job(
                            send_reminder,
                            trigger="cron",
                            hour=hour,
                            minute=minute,
                            timezone=_tzinfo,
                            kwargs={
                                "user_id": uid,
                                "message": msg,
                                "rem_id": None  # daily never sets completed
                            }
                        )
                    else:
                        job = scheduler.add_job(
                            send_reminder,
                            trigger="cron",
                            hour=hour,
                            minute=minute,
                            kwargs={
                                "user_id": uid,
                                "message": msg,
                                "rem_id": None
                            }
                        )

                    add_job_map(rem_id, job.id)

            except Exception as e:
                logging.error("Reload DAILY error: %s", e)

# ===========================
# PART 2/3 — scheduler, forced join, start, set, notify_user, callback handler
# ===========================

# Scheduler (use timezone when available)
scheduler = AsyncIOScheduler(timezone=_tzinfo) if _tzinfo else AsyncIOScheduler()
scheduler.start()

# GLOBAL BOT fallback
GLOBAL_BOT = None

async def send_reminder(user_id, message, context=None, rem_id: int = None):
    """
    This function is safe:
    - Accepts bot from context.bot if available
    - Accepts bot if context itself is a Bot instance
    - Falls back to GLOBAL_BOT during reload
    """
    bot = None

    # Case-1: context is normal telegram Context (has .bot)
    if context is not None and hasattr(context, "bot"):
        bot = context.bot

    # Case-2: context is actually a Bot instance
    elif context is not None and context.__class__.__name__ == "Bot":
        bot = context

    # Case-3: Fallback → reload job used GLOBAL_BOT
    elif GLOBAL_BOT is not None:
        bot = GLOBAL_BOT

    else:
        logging.error("❌ No bot instance found for sending reminder.")
        return

    # ---- SEND MESSAGE ----
    try:
        await bot.send_message(chat_id=user_id, text=f"⏰ Reminder:\n{message}")
    except Exception as e:
        logging.error(f"Reminder send error: {e}")

    # ---- If one-time reminder, mark completed ----
    if rem_id:
        try:
            set_completed(rem_id)
            remove_mapping(rem_id)
        except Exception as e:
            logging.error(f"Failed mark completed: {e}")

# FORCED JOIN
async def check_join_status(user_id, context):
    if not FORCED_CHANNEL:
        return True
    try:
        member = await context.bot.get_chat_member(FORCED_CHANNEL, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

async def send_force_join_message(update: Update, context):
    user_id = update.effective_user.id
    btn = [
        [
            InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{FORCED_CHANNEL.replace('@','')}"),
            InlineKeyboardButton("✔ Verify", callback_data="verify_join")
        ]
    ]

    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return
    await msg.reply_text(
        t(user_id, "force_join_text"),
        reply_markup=InlineKeyboardMarkup(btn),
        parse_mode="Markdown"
    )

# LANGUAGE MENU
async def send_language_menu(update: Update, context):
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    btn = [
        [
            InlineKeyboardButton("🇧🇩 বাংলা", callback_data="lang_bn"),
            InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")
        ]
    ]
    if msg:
        await msg.reply_text("🌐 Select your language:", reply_markup=InlineKeyboardMarkup(btn))

# /start
async def start(update: Update, context):
    user_id = update.effective_user.id

    # Forced join
    if not await check_join_status(user_id, context):
        return await send_force_join_message(update, context)

    lang = get_lang(user_id)

    if not lang:
        return await send_language_menu(update, context)

    # Language already set → ask change or continue
    text = "আপনার বর্তমান ভাষা: বাংলা 🇧🇩\nআপনি কি পরিবর্তন করতে চান?" if lang == "bn" else \
           "Your current language is English 🇬🇧\nDo you want to change it?"

    btn = [
        [InlineKeyboardButton("🌐 Change Language", callback_data="change_lang")],
        [InlineKeyboardButton("➡️ Continue", callback_data="go_ahead")]
    ]

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(btn))

# /set_reminder
async def set_reminder(update: Update, context):
    user_id = update.effective_user.id

    if not await check_join_status(user_id, context):
        return await send_force_join_message(update, context)

    if not get_lang(user_id):
        return await update.message.reply_text(t(user_id, "select_lang_first"))

    btn = [
        [InlineKeyboardButton("⏱ Minutes / Hours", callback_data="rem_min_hour")],
        [InlineKeyboardButton("📅 Date", callback_data="rem_date")],
        [InlineKeyboardButton("🔁 Daily", callback_data="rem_daily")],
    ]

    await update.message.reply_text(
        t(user_id, "choose_type"),
        reply_markup=InlineKeyboardMarkup(btn)
    )

# ADMIN → /notify_user
async def notify_user(update: Update, context):
    user_id = update.effective_user.id

    if not is_admin(user_id):
        return await update.message.reply_text("❌ You are not allowed.")

    await update.message.reply_text(
        "🔔 কাকে Notify করতে চান?\n"
        "User ID দিন অথবা @username লিখুন:"
    )

    context.user_data["mode"] = "notify_select_user"

# CALLBACK HANDLER
async def callback_handler(update: Update, context):
    q = update.callback_query
    user_id = q.from_user.id

    try:
        await q.answer()
    except:
        pass

    # Forced Join Verify
    if q.data == "verify_join":
        if not await check_join_status(user_id, context):
            btn = [
                [
                    InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{FORCED_CHANNEL.replace('@','')}"),
                    InlineKeyboardButton("✔ Verify", callback_data="verify_join")
                ]
            ]
            return await q.edit_message_text("⚠️ You have not joined yet!", reply_markup=InlineKeyboardMarkup(btn))

        return await q.edit_message_text("✔ Verified! Now send /start")

    # Language Change
    if q.data == "change_lang":
        return await send_language_menu(update, context)

    # Continue
    if q.data == "go_ahead":
        return await q.edit_message_text(t(user_id, "start_ready"))

    # Lang Select
    if q.data == "lang_bn":
        save_lang(user_id, "bn")
        return await q.edit_message_text("🇧🇩 বাংলা সেট হয়েছে ✔\n/start দিন")

    if q.data == "lang_en":
        save_lang(user_id, "en")
        return await q.edit_message_text("🇬🇧 English set ✔\nUse /start")

    # Reminder Type
    if q.data == "rem_min_hour":
        context.user_data["mode"] = "min_hour"
        return await q.edit_message_text(t(user_id, "enter_min_hour"), parse_mode="Markdown")

    if q.data == "rem_date":
        context.user_data["mode"] = "date_select"
        return await q.edit_message_text(t(user_id, "date_prompt"))

    if q.data == "rem_daily":
        btn = [
            [InlineKeyboardButton("🕛 Single Time", callback_data="daily_single")],
            [InlineKeyboardButton("🕒 Multiple Time", callback_data="daily_multi")],
        ]
        return await q.edit_message_text("🔁 Daily Reminder:", reply_markup=InlineKeyboardMarkup(btn))

    # Daily Single
    if q.data == "daily_single":
        context.user_data["mode"] = "daily_single_time"
        return await q.edit_message_text(t(user_id, "daily_single_time_prompt"))

    # Daily Multi
    if q.data == "daily_multi":
        context.user_data["mode"] = "daily_multi_time"
        return await q.edit_message_text(t(user_id, "daily_multi_time_prompt"))

    # Repeat
    if q.data == "repeat_yes":
        context.user_data["mode"] = "repeat_count"
        return await q.edit_message_text("🔁 কয়বার Repeat করতে চান?\nউদাহরণ: 2 / 3 / 5")

    if q.data == "repeat_no":
        # Reminder target (self or notify mode)
        target_id = context.user_data.get("notify_target", user_id)

        msg = context.user_data.get("msg")
        tval = context.user_data.get("time")

        if not msg or not tval:
             return await q.edit_message_text("⚠️ Invalid state. Please set reminder again.")

        # Save to DB
        rem_id = save_reminder(target_id, msg, "min_hour", tval, 0)

         # Convert time
        seconds = int(tval[:-1]) * (60 if tval.endswith("m") else 3600)
        run_time = (datetime.now(tz=_tzinfo) + timedelta(seconds=seconds)) if _tzinfo else (datetime.now() + timedelta(seconds=seconds))

         # Schedule job
        job = scheduler.add_job(
             send_reminder,
             trigger="date",
             run_date=run_time,
             kwargs={"user_id": target_id, "message": msg, "context": context, "rem_id": rem_id}
         )

         # Map job ID
        try:
           add_job_map(rem_id, job.id)
        except Exception as e:
           logging.error(f"Job mapping error: {e}")

       # Clear session
        context.user_data.clear()

       # Success summary
        return await q.edit_message_text(
           f"✅ Reminder Successfully Set!\n"
           f"📝 Message: {msg}\n"
           f"⏱ Time: {tval}\n"
           f"🔁 Repeat: No\n"
           f"📌 Your reminder is now active."
        )

# ===========================
# PART 3/3 — text_handler, show/delete reminders, main()
# ===========================

# TEXT HANDLER (ALL FLOWS)
async def text_handler(update: Update, context):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # ADMIN — notify user STEP-1: user id / @username
    if context.user_data.get("mode") == "notify_select_user":
        target = text.replace("@", "")
        context.user_data["notify_target"] = target
        context.user_data["mode"] = "notify_type"

        btn = [
            [InlineKeyboardButton("⏱ Minutes/Hours", callback_data="rem_min_hour")],
            [InlineKeyboardButton("📅 Date", callback_data="rem_date")],
            [InlineKeyboardButton("🔁 Daily", callback_data="rem_daily")]
        ]
        return await update.message.reply_text(
            "রিমাইন্ডার টাইপ নির্বাচন করুন:",
            reply_markup=InlineKeyboardMarkup(btn)
        )

    # MINUTES/HOURS — STEP 1 (time)
    if context.user_data.get("mode") == "min_hour" and "time" not in context.user_data:
        if not (text.endswith("m") or text.endswith("h")):
            return await update.message.reply_text(t(user_id, "wrong_format"))

        context.user_data["time"] = text
        context.user_data["mode"] = "min_hour_msg"
        return await update.message.reply_text(t(user_id, "enter_message"))

    # MINUTES/HOURS — STEP 2 (msg)
    if context.user_data.get("mode") == "min_hour_msg":
        context.user_data["msg"] = text

        btn = [
            [
                InlineKeyboardButton("✔ YES", callback_data="repeat_yes"),
                InlineKeyboardButton("✖ NO", callback_data="repeat_no")
            ]
        ]
        return await update.message.reply_text(
            "🔁 আপনি কি Repeat করতে চান?",
            reply_markup=InlineKeyboardMarkup(btn)
        )

    # MINUTES/HOURS — STEP 3 (repeat count)
    if context.user_data.get("mode") == "repeat_count":
        if not text.isdigit():
            return await update.message.reply_text("⚠️ শুধু সংখ্যা লিখুন (যেমন: 2 / 5)")

        repeat_count = int(text)
        msg = context.user_data.get("msg")
        tval = context.user_data.get("time")
        target = context.user_data.get("notify_target", user_id)

        rem_id = save_reminder(target, msg, "min_hour", tval, repeat_count)

        seconds = int(tval[:-1]) * (60 if tval.endswith("m") else 3600)

        for i in range(repeat_count):
            run_time = (datetime.now(tz=_tzinfo) + timedelta(seconds=seconds * (i + 1))) if _tzinfo else (datetime.now() + timedelta(seconds=seconds * (i + 1)))

            job = scheduler.add_job(
                send_reminder,
                trigger="date",
                run_date=run_time,
                kwargs={"user_id": target, "message": msg, "context": context, "rem_id": rem_id}
            )
            add_job_map(rem_id, job.id)

        context.user_data.clear()

        return await update.message.reply_text(
            f"✅ Reminder Successfully Set!\n"
            f"📝 Message: {msg}\n"
            f"⏱ Time: {tval}\n"
            f"🔁 Repeat: {repeat_count} times\n"
            f"📌 Your reminder is now active."
        )

    # DATE — STEP 1: date
    if context.user_data.get("mode") == "date_select":
        try:
            datetime.strptime(text, "%d/%m/%y")
        except:
            return await update.message.reply_text("⚠️ তারিখ ঠিক ফরম্যাটে দিন (15/11/25)")

        context.user_data["date"] = text
        context.user_data["mode"] = "date_time"
        return await update.message.reply_text(t(user_id, "time_prompt"))

    # DATE — STEP 2: time
    if context.user_data.get("mode") == "date_time":
        try:
            datetime.strptime(text, "%I.%M %p")
        except:
            return await update.message.reply_text("⚠️ সময় ঠিক ফরম্যাট (10.15 PM)")

        context.user_data["time"] = text
        context.user_data["mode"] = "date_message"
        return await update.message.reply_text(t(user_id, "enter_message_date"))

    # DATE — STEP 3: message
    if context.user_data.get("mode") == "date_message":
        msg = text
        date_str = context.user_data["date"]
        time_str = context.user_data["time"]
        target = context.user_data.get("notify_target", user_id)

        # build tz-aware datetime if tz available
        try:
            dt_naive = datetime.strptime(f"{date_str} {time_str}", "%d/%m/%y %I.%M %p")
            dt = dt_naive.replace(tzinfo=_tzinfo) if _tzinfo else dt_naive
        except Exception as e:
            logging.error("Date parse failed: %s", e)
            return await update.message.reply_text("⚠️ Date/time parse failed.")

        rem_id = save_reminder(target, msg, "date", f"{date_str} {time_str}", 0)

        job = scheduler.add_job(
            send_reminder,
            trigger="date",
            run_date=dt,
            kwargs={"user_id": target, "message": msg, "context": context, "rem_id": rem_id}
        )
        add_job_map(rem_id, job.id)

        context.user_data.clear()

        return await update.message.reply_text(
            f"✅ Reminder Successfully Set!\n"
            f"📝 Message: {msg}\n"
            f"📅 Date: {date_str}\n"
            f"⏱ Time: {time_str}\n"
            f"🔁 Repeat: No\n"
            f"📌 Your reminder is now active."
        )

    # DAILY — Single Time
    if context.user_data.get("mode") == "daily_single_time":
        try:
            datetime.strptime(text, "%I.%M %p")
        except:
            return await update.message.reply_text(t(user_id, "wrong_time_format"))

        context.user_data["daily_times"] = [text]
        context.user_data["mode"] = "daily_msg"
        return await update.message.reply_text(t(user_id, "enter_message_daily"))

    # DAILY — Multi Time (lines)
    if context.user_data.get("mode") == "daily_multi_time":
        lines = [i.strip() for i in text.split("\n") if i.strip()]
        valid = []

        for line in lines:
            try:
                datetime.strptime(line, "%I.%M %p")
                valid.append(line)
            except:
                return await update.message.reply_text(t(user_id, "wrong_time_format"))

        context.user_data["daily_times"] = valid
        context.user_data["mode"] = "daily_msg"
        return await update.message.reply_text(t(user_id, "enter_message_daily"))

    # DAILY — STEP Message
    if context.user_data.get("mode") == "daily_msg":
        msg = text
        times = context.user_data["daily_times"]
        target = context.user_data.get("notify_target", user_id)

        rem_id = save_reminder(target, msg, "daily", ";".join(times), 0)

        for tstr in times:
            dt_obj = datetime.strptime(tstr, "%I.%M %p")
            hour, minute = dt_obj.hour, dt_obj.minute

            if _tzinfo:
                job = scheduler.add_job(
                    send_reminder,
                    trigger="cron",
                    hour=hour,
                    minute=minute,
                    timezone=_tzinfo,
                    kwargs={"user_id": target, "message": msg, "context": context, "rem_id": None}
                )
            else:
                job = scheduler.add_job(
                    send_reminder,
                    trigger="cron",
                    hour=hour,
                    minute=minute,
                    kwargs={"user_id": target, "message": msg, "context": context, "rem_id": None}
                )

            add_job_map(rem_id, job.id)

        context.user_data.clear()

        return await update.message.reply_text(
            f"✅ Reminder Successfully Set!\n"
            f"📝 Message: {msg}\n"
            f"⏱ Times: {', '.join(times)}\n"
            f"🔁 Repeat: Daily\n"
            f"📌 Your reminder is now active."
        )

    return

# SHOW ACTIVE REMINDERS (/show_reminder)
async def show_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user_reminders(user_id)

    active = [i for i in data if i[5] == "active"]

    if not active:
        return await update.message.reply_text("📭 কোনো Active Reminder নেই।")

    text = "📋 *Active Reminders:*\n\n"
    for rid, msg, stype, tval, rep, status in active:
        text += f"🆔 ID: {rid}\n"
        text += f"📝 Message: {msg}\n"

        if stype == "min_hour":
            text += f"⏱ Time: {tval}\n🔁 Repeat: {rep}\n"
        elif stype == "date":
            d = tval.split(" ")
            text += f"📅 {d[0]}\n⏱ {' '.join(d[1:])}\n"
        else:
            text += f"⏱ {tval.replace(';', ', ')}\n🔁 Daily\n"

        text += f"\n\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# SHOW COMPLETED (/show_completed)
async def show_completed(update, context):
    user_id = update.effective_user.id

    cursor.execute("SELECT id,message,schedule_type,time_value,repeat FROM reminders WHERE user_id=? AND status='completed'", (user_id,))
    rows = cursor.fetchall()

    if not rows:
        return await update.message.reply_text("📦 No completed reminders.")

    txt = "📦 *Completed Reminders:*\n\n"
    for rid, msg, stype, tval, rep in rows:
        txt += f"🆔 ID: {rid}\n📝 Message: {msg}\n⏱ Time:  {tval}\n🔁 Repeat: {rep}\n\n"

    await update.message.reply_text(txt, parse_mode="Markdown")

# CLEAR COMPLETED (/clear_completed)
async def clear_completed(update, context):
    user_id = update.effective_user.id
    cursor.execute("DELETE FROM reminders WHERE user_id=? AND status='completed'", (user_id,))
    conn.commit()
    await update.message.reply_text("🧹 Completed reminders cleared!")

# DELETE REMINDER (/delete_reminder_<id>)
async def delete_reminder(update, context):
    user_id = update.effective_user.id
    txt = update.message.text

    try:
        rem_id = int(txt.replace("/delete_reminder_", ""))
    except:
        return await update.message.reply_text("❌ Invalid format.")

    cursor.execute("SELECT id FROM reminders WHERE id=? AND user_id=?", (rem_id, user_id))
    if not cursor.fetchone():
        return await update.message.reply_text("❌ Reminder not found.")

    jobs = get_jobs(rem_id)
    for jid in jobs:
        try:
            scheduler.remove_job(jid)
        except:
            pass

    remove_mapping(rem_id)

    cursor.execute("DELETE FROM reminders WHERE id=?", (rem_id,))
    conn.commit()

    await update.message.reply_text("🗑 Reminder deleted!")

# HELP
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    text = (
        "🧠 *ব্যবহার করা খুব সহজ!*\n\n"
        "• `/start` → ভাষা নির্বাচন\n"
        "• `/set_reminder` → রিমাইন্ডার সেট\n"
        "• `/show_reminder` → সক্রিয় রিমাইন্ডার দেখুন\n"
        "• `/show_completed` → সম্পন্ন রিমাইন্ডার তালিকা\n"
        "• `/clear_completed` → সম্পন্ন রিমাইন্ডার ডিলেট\n"
        "• `/delete_reminder_<id>` → রিমাইন্ডার ডিলিট\n"
        "\n"
        "যেকোনো সময় সাহায্যের জন্য আবার `/help` ব্যবহার করুন।"
    )

    await update.message.reply_text(text, parse_mode="Markdown")

# Simple aiohttp ping server (for healthchecks)


# MAIN — webhook mode (with fallback)
def main():
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN is not set in environment.")
        return

    port = int(os.getenv("PORT", "8000"))
    webhook_path = f"webhook/{BOT_TOKEN}"   # path on your server
    webhook_url = f"{WEBHOOK_URL.rstrip('/')}/{webhook_path}" if WEBHOOK_URL else ""

    # build application
    application = Application.builder().token(BOT_TOKEN).build()

    # set global bot for scheduler fallback
    global GLOBAL_BOT
    GLOBAL_BOT = application.bot

    # register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set_reminder", set_reminder))
    application.add_handler(CommandHandler("show_reminder", show_reminder))
    application.add_handler(CommandHandler("show_completed", show_completed))
    application.add_handler(CommandHandler("clear_completed", clear_completed))
    application.add_handler(CommandHandler("notify_user", notify_user))
    application.add_handler(CommandHandler("help", help_command))

    application.add_handler(MessageHandler(filters.Regex(r"^/delete_reminder_\d+$"), delete_reminder))
    application.add_handler(CallbackQueryHandler(callback_handler))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # reload db jobs to scheduler
    reload_scheduled_jobs(application)

    # start a tiny ping server for healthcheck

    # If WEBHOOK_URL provided, try webhook; otherwise use polling.
    if WEBHOOK_URL:
        print(f"Starting webhook on port {port} with path /{webhook_path} and url {webhook_url}")
        try:
            # run_webhook blocks until stopped
            application.run_webhook(listen="0.0.0.0",
                                    port=port,
                                    url_path=webhook_path,
                                    webhook_url=webhook_url)
            return
        except Exception as e:
            logging.error("run_webhook failed: %s — falling back to polling", e)

    print("Starting polling (WEBHOOK skipped or failed).")
    application.run_polling()

if __name__ == "__main__":
    main()


