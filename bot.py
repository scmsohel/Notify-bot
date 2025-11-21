# bot.py — Final Clean Version (Webhook + Polling Fallback + Ping Server)
# ----------------------------------------------------------------------

import asyncio
import os
import logging
import sqlite3
from datetime import datetime, timedelta
from dotenv import load_dotenv
import requests
from aiohttp import web

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

# timezone
try:
    from zoneinfo import ZoneInfo
except:
    ZoneInfo = None

# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.ERROR
)

# ---------------------------------------------------------
# Env
# ---------------------------------------------------------
load_dotenv()
BOT_TOKEN      = os.getenv("BOT_TOKEN")
FORCED_CHANNEL = os.getenv("FORCED_CHANNEL")
ADMIN_ID       = int(os.getenv("ADMIN_ID") or 0)
WEBHOOK_URL    = os.getenv("WEBHOOK_URL", "").strip()
DB_PATH        = os.getenv("DB_PATH", "bot.db")
TZ             = os.getenv("TZ", "Asia/Dhaka")

# timezone load
_tzinfo = None
if ZoneInfo:
    try:
        _tzinfo = ZoneInfo(TZ)
    except:
        logging.error("Invalid TZ, using system timezone.")

# ---------------------------------------------------------
# Admin helper
# ---------------------------------------------------------
def is_admin(uid):
    return uid == ADMIN_ID

# ---------------------------------------------------------
# SQLite Init
# ---------------------------------------------------------
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

# ---------------------------------------------------------
# Languages
# ---------------------------------------------------------
LANG = {
    "bn": {
        "force_join_text": "🚫 বট ব্যবহার করতে হলে আমাদের চ্যানেলে Join করুন।",
        "select_lang_first": "🔰 প্রথমে আপনার ভাষা নির্বাচন করুন (/start)।",
        "choose_type": "🕹 রিমাইন্ডার টাইপ নির্বাচন করুন:",
        "enter_min_hour": "উদাহরণ: `2m`, `1h`",
        "wrong_format": "⚠️ ভুল ফরম্যাট। উদাহরণ: 5m / 1h",
        "enter_message": "✍ এখন Reminder-এর মেসেজ লিখুন:",
        "date_prompt": "📅 তারিখ লিখুন (15/11/25)",
        "time_prompt": "⏱ সময় লিখুন (10.15 PM)",
        "enter_message_date": "✍ রিমাইন্ডারের মেসেজ লিখুন:",
        "start_ready": "✔ এখন আপনি বট ব্যবহার করতে পারবেন।",
        "daily_single_time_prompt": "⏱ প্রতিদিন একটি সময় (10.00 AM)",
        "daily_multi_time_prompt": "⏱ একাধিক সময় (প্রতিটি নতুন লাইনে)",
        "wrong_time_format": "⚠️ সময় ফরম্যাট ভুল। উদাহরণ: 10.20 PM",
        "enter_message_daily": "✍ Daily Reminder-এর মেসেজ লিখুন:"
    },

    "en": {
        "force_join_text": "🚫 Please join our channel first.",
        "select_lang_first": "🔰 Select language first (/start).",
        "choose_type": "🕹 Choose reminder type:",
        "enter_min_hour": "Example: `2m`, `1h`",
        "wrong_format": "⚠️ Wrong format. Example: 5m / 1h",
        "enter_message": "✍ Enter reminder message:",
        "date_prompt": "📅 Enter date (15/11/25)",
        "time_prompt": "⏱ Enter time (10.15 PM)",
        "enter_message_date": "✍ Enter reminder message:",
        "start_ready": "✔ You can use the bot now.",
        "daily_single_time_prompt": "⏱ One time daily (10.00 AM)",
        "daily_multi_time_prompt": "⏱ Multiple times (each new line)",
        "wrong_time_format": "⚠️ Wrong time format.",
        "enter_message_daily": "✍ Enter daily reminder message:"
    }
}

def t(uid, key):
    lang = get_lang(uid) or "bn"
    return LANG.get(lang, LANG["bn"]).get(key, key)

# ---------------------------------------------------------
# DB helpers
# ---------------------------------------------------------
def save_lang(uid, lang):
    cursor.execute("INSERT OR REPLACE INTO users (user_id, lang) VALUES (?,?)", (uid, lang))
    conn.commit()

def get_lang(uid):
    cursor.execute("SELECT lang FROM users WHERE user_id=?", (uid,))
    row = cursor.fetchone()
    return row[0] if row else None

def save_reminder(uid, msg, stype, tval, rep):
    cursor.execute(
        "INSERT INTO reminders (user_id, message, schedule_type, time_value, repeat) VALUES (?,?,?,?,?)",
        (uid, msg, stype, tval, rep)
    )
    conn.commit()
    return cursor.lastrowid

def set_completed(rem_id):
    cursor.execute("UPDATE reminders SET status='completed' WHERE id=?", (rem_id,))
    conn.commit()

def add_job_map(rem_id, job_id):
    cursor.execute("INSERT INTO scheduled_jobs(reminder_id, job_id) VALUES (?,?)", (rem_id, job_id))
    conn.commit()

def get_jobs(rem_id):
    cursor.execute("SELECT job_id FROM scheduled_jobs WHERE reminder_id=?", (rem_id,))
    return [r[0] for r in cursor.fetchall()]

def remove_mapping(rem_id):
    cursor.execute("DELETE FROM scheduled_jobs WHERE reminder_id=?", (rem_id,))
    conn.commit()

def get_user_reminders(uid):
    cursor.execute("SELECT id,message,schedule_type,time_value,repeat,status FROM reminders WHERE user_id=?", (uid,))
    return cursor.fetchall()

# ---------------------------------------------------------
# Scheduler + Reload Jobs
# ---------------------------------------------------------
scheduler = AsyncIOScheduler(timezone=_tzinfo) if _tzinfo else AsyncIOScheduler()
scheduler.start()

GLOBAL_BOT = None

async def send_reminder(user_id, message, context=None, rem_id=None):
    bot = None

    if context and hasattr(context, "bot"):
        bot = context.bot
    elif context and context.__class__.__name__ == "Bot":
        bot = context
    elif GLOBAL_BOT:
        bot = GLOBAL_BOT
    else:
        logging.error("No bot instance for reminder.")
        return

    try:
        await bot.send_message(chat_id=user_id, text=f"⏰ Reminder:\n{message}")
    except Exception as e:
        logging.error(e)

    if rem_id:
        set_completed(rem_id)
        remove_mapping(rem_id)

def reload_scheduled_jobs(app=None):
    cursor.execute(
        "SELECT id,user_id,message,schedule_type,time_value,repeat FROM reminders WHERE status='active'"
    )
    rows = cursor.fetchall()

    for rem_id, uid, msg, stype, tval, rep in rows:

        # MIN/HOUR
        if stype == "min_hour":
            try:
                seconds = int(tval[:-1]) * (60 if tval.endswith("m") else 3600)
                run_time = (datetime.now(tz=_tzinfo) + timedelta(seconds=seconds)) if _tzinfo else (datetime.now() + timedelta(seconds=seconds))

                job = scheduler.add_job(
                    send_reminder,
                    trigger="date",
                    run_date=run_time,
                    kwargs={"user_id": uid, "message": msg, "rem_id": rem_id}
                )
                add_job_map(rem_id, job.id)
            except Exception as e:
                logging.error(e)

        # DATE
        elif stype == "date":
            try:
                dt_naive = datetime.strptime(tval, "%d/%m/%y %I.%M %p")
                dt = dt_naive.replace(tzinfo=_tzinfo) if _tzinfo else dt_naive

                if dt > (datetime.now(tz=_tzinfo) if _tzinfo else datetime.now()):
                    job = scheduler.add_job(
                        send_reminder,
                        trigger="date",
                        run_date=dt,
                        kwargs={"user_id": uid, "message": msg, "rem_id": rem_id}
                    )
                    add_job_map(rem_id, job.id)
            except Exception as e:
                logging.error(e)

        # DAILY
        elif stype == "daily":
            try:
                times = tval.split(";")
                for T in times:
                    dt_obj = datetime.strptime(T, "%I.%M %p")
                    job = scheduler.add_job(
                        send_reminder,
                        trigger="cron",
                        hour=dt_obj.hour,
                        minute=dt_obj.minute,
                        timezone=_tzinfo,
                        kwargs={"user_id": uid, "message": msg, "rem_id": None}
                    )
                    add_job_map(rem_id, job.id)
            except Exception as e:
                logging.error(e)
# ===========================
# PART 2/3 — Forced Join, Start, Set Reminder, Callback Handler
# ===========================

# ---------------------------
# Forced Join Check
# ---------------------------
async def check_join_status(user_id, context):
    if not FORCED_CHANNEL:
        return True
    try:
        m = await context.bot.get_chat_member(FORCED_CHANNEL, user_id)
        return m.status in ["member", "administrator", "creator"]
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
    if msg:
        await msg.reply_text(
            t(user_id, "force_join_text"),
            reply_markup=InlineKeyboardMarkup(btn),
            parse_mode="Markdown"
        )

# ---------------------------
# Language Menu
# ---------------------------
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

# ---------------------------
# /start
# ---------------------------
async def start(update: Update, context):
    user_id = update.effective_user.id

    if not await check_join_status(user_id, context):
        return await send_force_join_message(update, context)

    lang = get_lang(user_id)

    # New user → ask language
    if not lang:
        return await send_language_menu(update, context)

    # Already has language → show menu
    txt = "আপনার বর্তমান ভাষা: বাংলা 🇧🇩\nপরিবর্তন করতে চান?" if lang == "bn" else \
          "Current language: English 🇬🇧\nDo you want to change?"

    btn = [
        [InlineKeyboardButton("🌐 Change Language", callback_data="change_lang")],
        [InlineKeyboardButton("➡️ Continue", callback_data="go_ahead")]
    ]

    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(btn))

# ---------------------------
# /set_reminder
# ---------------------------
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

# ---------------------------
# /notify_user (ADMIN ONLY)
# ---------------------------
async def notify_user(update: Update, context):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ You are not allowed.")

    await update.message.reply_text(
        "🔔 কাকে Notify করতে চান?\nUser ID বা @username দিন:"
    )
    context.user_data["mode"] = "notify_select_user"

# ---------------------------
# Callback Handler
# ---------------------------
async def callback_handler(update: Update, context):
    q = update.callback_query
    user_id = q.from_user.id
    data = q.data

    try:
        await q.answer()
    except:
        pass

    # -------- VERIFY JOIN --------
    if data == "verify_join":
        if not await check_join_status(user_id, context):
            btn = [[
                InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{FORCED_CHANNEL.replace('@','')}"),
                InlineKeyboardButton("✔ Verify", callback_data="verify_join")
            ]]
            return await q.edit_message_text("⚠️ You have not joined yet!", reply_markup=InlineKeyboardMarkup(btn))

        return await q.edit_message_text("✔ Verified! Send /start again.")

    # -------- LANGUAGE --------
    if data == "change_lang":
        return await send_language_menu(update, context)

    if data == "go_ahead":
        return await q.edit_message_text(t(user_id, "start_ready"))

    if data == "lang_bn":
        save_lang(user_id, "bn")
        return await q.edit_message_text("🇧🇩 বাংলা সেট হয়েছে ✔\n/start দিন")

    if data == "lang_en":
        save_lang(user_id, "en")
        return await q.edit_message_text("🇬🇧 English set ✔\nUse /start")

    # -------- REMINDER TYPE --------
    if data == "rem_min_hour":
        context.user_data["mode"] = "min_hour"
        return await q.edit_message_text(t(user_id, "enter_min_hour"), parse_mode="Markdown")

    if data == "rem_date":
        context.user_data["mode"] = "date_select"
        return await q.edit_message_text(t(user_id, "date_prompt"))

    if data == "rem_daily":
        btn = [
            [InlineKeyboardButton("🕛 Single Time", callback_data="daily_single")],
            [InlineKeyboardButton("🕒 Multiple Time", callback_data="daily_multi")]
        ]
        return await q.edit_message_text("🔁 Daily Reminder:", reply_markup=InlineKeyboardMarkup(btn))

    # -------- DAILY options --------
    if data == "daily_single":
        context.user_data["mode"] = "daily_single_time"
        return await q.edit_message_text(t(user_id, "daily_single_time_prompt"))

    if data == "daily_multi":
        context.user_data["mode"] = "daily_multi_time"
        return await q.edit_message_text(t(user_id, "daily_multi_time_prompt"))

    # -------- REPEAT options --------
    if data == "repeat_yes":
        context.user_data["mode"] = "repeat_count"
        return await q.edit_message_text("🔁 কয়বার Repeat করতে চান?\nউদাহরণ: 2 / 3 / 5")

    if data == "repeat_no":
        msg  = context.user_data.get("msg")
        tval = context.user_data.get("time")
        target = context.user_data.get("notify_target", user_id)

        if not msg or not tval:
            return await q.edit_message_text("⚠️ Invalid state. Try again.")

        rem_id = save_reminder(target, msg, "min_hour", tval, 0)

        seconds = int(tval[:-1]) * (60 if tval.endswith("m") else 3600)
        run_time = (datetime.now(tz=_tzinfo) + timedelta(seconds=seconds)) if _tzinfo else (datetime.now() + timedelta(seconds=seconds))

        job = scheduler.add_job(
            send_reminder,
            trigger="date",
            run_date=run_time,
            kwargs={"user_id": target, "message": msg, "context": context, "rem_id": rem_id}
        )

        add_job_map(rem_id, job.id)
        context.user_data.clear()

        return await q.edit_message_text(
            f"✅ Reminder Set!\n📝 {msg}\n⏱ {tval}\n🔁 No repeat"
        )
# ===========================
# PART 3/3 — Text Handler, Show/Delete, MAIN()
# ===========================

# TEXT HANDLER
async def text_handler(update: Update, context):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # ---------------- ADMIN Notify STEP-1 ----------------
    if context.user_data.get("mode") == "notify_select_user":
        target = text.replace("@", "")
        context.user_data["notify_target"] = target
        context.user_data["mode"] = "notify_type"

        btn = [
            [InlineKeyboardButton("⏱ Minutes/Hours", callback_data="rem_min_hour")],
            [InlineKeyboardButton("📅 Date", callback_data="rem_date")],
            [InlineKeyboardButton("🔁 Daily", callback_data="rem_daily")]
        ]
        return await update.message.reply_text("রিমাইন্ডার টাইপ নির্বাচন করুন:", reply_markup=InlineKeyboardMarkup(btn))

    # ---------------- MIN/HOUR STEP-1 (time) ----------------
    if context.user_data.get("mode") == "min_hour" and "time" not in context.user_data:
        if not (text.endswith("m") or text.endswith("h")):
            return await update.message.reply_text(t(user_id, "wrong_format"))

        context.user_data["time"] = text
        context.user_data["mode"] = "min_hour_msg"
        return await update.message.reply_text(t(user_id, "enter_message"))

    # ---------------- MIN/HOUR STEP-2 (msg) ----------------
    if context.user_data.get("mode") == "min_hour_msg":
        context.user_data["msg"] = text

        btn = [[
            InlineKeyboardButton("✔ YES", callback_data="repeat_yes"),
            InlineKeyboardButton("✖ NO", callback_data="repeat_no")
        ]]

        return await update.message.reply_text(
            "🔁 আপনি কি Repeat করতে চান?",
            reply_markup=InlineKeyboardMarkup(btn)
        )

    # ---------------- MIN/HOUR STEP-3 (repeat count) ----------------
    if context.user_data.get("mode") == "repeat_count":
        if not text.isdigit():
            return await update.message.reply_text("⚠️ শুধু সংখ্যা লিখুন (যেমন 2 / 5)")

        repeat_count = int(text)
        msg   = context.user_data["msg"]
        tval  = context.user_data["time"]
        target = context.user_data.get("notify_target", user_id)

        rem_id = save_reminder(target, msg, "min_hour", tval, repeat_count)

        seconds = int(tval[:-1]) * (60 if tval.endswith("m") else 3600)

        for i in range(repeat_count):
            run_time = (datetime.now(tz=_tzinfo) + timedelta(seconds=seconds*(i+1))) if _tzinfo \
                       else (datetime.now() + timedelta(seconds=seconds*(i+1)))

            job = scheduler.add_job(
                send_reminder,
                trigger="date",
                run_date=run_time,
                kwargs={"user_id": target, "message": msg, "context": context, "rem_id": rem_id}
            )

            add_job_map(rem_id, job.id)

        context.user_data.clear()

        return await update.message.reply_text(
            f"✅ Reminder Set!\n📝 {msg}\n⏱ {tval}\n🔁 {repeat_count} times"
        )

    # ---------------- DATE STEP-1 (date) ----------------
    if context.user_data.get("mode") == "date_select":
        try:
            datetime.strptime(text, "%d/%m/%y")
        except:
            return await update.message.reply_text("⚠️ তারিখ ভুল (Format: 15/11/25)")

        context.user_data["date"] = text
        context.user_data["mode"] = "date_time"
        return await update.message.reply_text(t(user_id, "time_prompt"))

    # ---------------- DATE STEP-2 (time) ----------------
    if context.user_data.get("mode") == "date_time":
        try:
            datetime.strptime(text, "%I.%M %p")
        except:
            return await update.message.reply_text("⚠️ সময় ভুল (Format: 10.15 PM)")

        context.user_data["time"] = text
        context.user_data["mode"] = "date_message"
        return await update.message.reply_text(t(user_id, "enter_message_date"))

    # ---------------- DATE STEP-3 (message) ----------------
    if context.user_data.get("mode") == "date_message":
        msg = text
        date_str = context.user_data["date"]
        time_str = context.user_data["time"]
        target = context.user_data.get("notify_target", user_id)

        dt_naive = datetime.strptime(f"{date_str} {time_str}", "%d/%m/%y %I.%M %p")
        dt = dt_naive.replace(tzinfo=_tzinfo) if _tzinfo else dt_naive

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
            f"✅ Reminder Created!\n📅 {date_str}\n⏱ {time_str}"
        )

    # ---------------- DAILY SINGLE TIME ----------------
    if context.user_data.get("mode") == "daily_single_time":
        try:
            datetime.strptime(text, "%I.%M %p")
        except:
            return await update.message.reply_text(t(user_id, "wrong_time_format"))

        context.user_data["daily_times"] = [text]
        context.user_data["mode"] = "daily_msg"
        return await update.message.reply_text(t(user_id, "enter_message_daily"))

    # ---------------- DAILY MULTI TIME ----------------
    if context.user_data.get("mode") == "daily_multi_time":
        times = [i.strip() for i in text.split("\n") if i.strip()]

        for tstr in times:
            try:
                datetime.strptime(tstr, "%I.%M %p")
            except:
                return await update.message.reply_text(t(user_id, "wrong_time_format"))

        context.user_data["daily_times"] = times
        context.user_data["mode"] = "daily_msg"
        return await update.message.reply_text(t(user_id, "enter_message_daily"))

    # ---------------- DAILY FINAL STEP (msg) ----------------
    if context.user_data.get("mode") == "daily_msg":
        msg = text
        times = context.user_data["daily_times"]
        target = context.user_data.get("notify_target", user_id)

        rem_id = save_reminder(target, msg, "daily", ";".join(times), 0)

        for tstr in times:
            dt = datetime.strptime(tstr, "%I.%M %p")
            hour, minute = dt.hour, dt.minute

            job = scheduler.add_job(
                send_reminder,
                trigger="cron",
                hour=hour,
                minute=minute,
                timezone=_tzinfo,
                kwargs={"user_id": target, "message": msg, "context": context, "rem_id": None}
            )

            add_job_map(rem_id, job.id)

        context.user_data.clear()

        return await update.message.reply_text(
            f"✅ Daily Reminder Added!\n⏱ {', '.join(times)}"
        )

# ===============================================================
# SHOW REMINDERS
# ===============================================================
async def show_reminder(update, context):
    uid = update.effective_user.id
    rows = get_user_reminders(uid)

    active = [i for i in rows if i[5] == "active"]

    if not active:
        return await update.message.reply_text("📭 কোনো Active Reminder নেই।")

    txt = "📋 *Active Reminders:*\n\n"
    for rid, msg, stype, tval, rep, status in active:
        txt += f"🆔 ID: {rid}\n📝 {msg}\n"

        if stype == "min_hour":
            txt += f"⏱ {tval}\n🔁 {rep}\n\n"
        elif stype == "date":
            d = tval.split(" ")
            txt += f"📅 {d[0]}\n⏱ {' '.join(d[1:])}\n\n"
        else:
            txt += f"⏱ {tval.replace(';', ', ')}\n🔁 Daily\n\n"

    await update.message.reply_text(txt, parse_mode="Markdown")

# ===============================================================
# DELETE REMINDER
# ===============================================================
async def delete_reminder(update, context):
    uid = update.effective_user.id
    txt = update.message.text

    try:
        rem_id = int(txt.replace("/delete_reminder_", ""))
    except:
        return await update.message.reply_text("❌ Invalid format.")

    cursor.execute("SELECT id FROM reminders WHERE id=? AND user_id=?", (rem_id, uid))
    if not cursor.fetchone():
        return await update.message.reply_text("❌ Reminder not found.")

    # Remove scheduled jobs
    for jid in get_jobs(rem_id):
        try:
            scheduler.remove_job(jid)
        except:
            pass

    remove_mapping(rem_id)

    cursor.execute("DELETE FROM reminders WHERE id=?", (rem_id,))
    conn.commit()

    await update.message.reply_text("🗑 Reminder deleted!")

# ===============================================================
# HELP
# ===============================================================
async def help_command(update, context):
    t = (
        "🧠 বট ব্যবহার করার নিয়ম:\n\n"
        "/start – ভাষা নির্বাচন\n"
        "/set_reminder – রিমাইন্ডার সেট\n"
        "/show_reminder – Active Reminder\n"
        "/show_completed – Completed List\n"
        "/clear_completed – Completed ডিলিট\n"
        "/delete_reminder_<id> – Reminder ডিলিট\n"
    )
    await update.message.reply_text(t, parse_mode="Markdown")

# ===============================================================
# MAIN (Webhook + Polling + Ping Server)
# ===============================================================
def main():
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN missing.")
        return

    port = int(os.getenv("PORT", "8000"))
    webhook_path = f"webhook/{BOT_TOKEN}"
    webhook_url = f"{WEBHOOK_URL.rstrip('/')}/{webhook_path}" if WEBHOOK_URL else ""

    app = Application.builder().token(BOT_TOKEN).build()

    global GLOBAL_BOT
    GLOBAL_BOT = app.bot

    # ---- Register Handlers ----
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set_reminder", set_reminder))
    app.add_handler(CommandHandler("show_reminder", show_reminder))
    app.add_handler(CommandHandler("show_completed", show_completed))
    app.add_handler(CommandHandler("clear_completed", clear_completed))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("notify_user", notify_user))

    app.add_handler(MessageHandler(filters.Regex(r"^/delete_reminder_\d+$"), delete_reminder))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Reload previous jobs
    reload_scheduled_jobs(app)

    # ---- Ping Server ----
    async def ping_route(request):
        return web.Response(text="ok")

    async def start_ping(port):
        papp = web.Application()
        papp.router.add_get("/ping", ping_route)

        runner = web.AppRunner(papp)
        await runner.setup()

        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"[PING] Serving at http://0.0.0.0:{port}/ping")

    try:
        asyncio.get_event_loop().create_task(start_ping(port))
    except:
        pass

    # ---- Webhook Mode ----
    if WEBHOOK_URL:
        print(f"Webhook on port {port}: /{webhook_path}")
        try:
            app.run_webhook(
                listen="0.0.0.0",
                port=port,
                url_path=webhook_path,
                webhook_url=webhook_url
            )
            return
        except Exception as e:
            logging.error(f"Webhook failed: {e} — falling back to polling")

    # ---- Polling Mode ----
    print("Polling started...")
    app.run_polling()

if __name__ == "__main__":
    main()
