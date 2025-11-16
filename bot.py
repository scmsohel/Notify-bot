# bot.py
# ===========================
# Render-ready Reminder Bot with GitHub JSON Backup
# ===========================
import asyncio
from aiohttp import web
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

# ===============================================================
# Logging (only errors)
# ===============================================================
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.ERROR
)

# ===============================================================
# Load ENV (Render: set env vars in dashboard)
# ===============================================================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
FORCED_CHANNEL = os.getenv("FORCED_CHANNEL")
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)

# GitHub backup config (OPTION-1)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USER = os.getenv("GITHUB_USER")
GITHUB_REPO = os.getenv("GITHUB_REPO")
BACKUP_FILE = os.getenv("BACKUP_FILE", "backup.json")

# ===============================================================
# DB path (keep default local; for persistent render disk change to /data/bot.db)
# If you add persistent disk on Render, set env DB_PATH=/data/bot.db
DB_PATH = os.getenv("DB_PATH", "bot.db")

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
# LANGUAGE TEXTS + Helper t(uid,key)
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
        lang = "bn"
    return LANG.get(lang, LANG["bn"]).get(key, f"{{Missing:{key}}}")

# ===============================================================
# DB helper functions (they also trigger backup save)
# ===============================================================
def save_lang(uid, lang):
    cursor.execute("INSERT OR REPLACE INTO users (user_id, lang) VALUES (?,?)", (uid, lang))
    conn.commit()
    # backup async
    asyncio.create_task(save_backup_async())

def get_lang(uid):
    cursor.execute("SELECT lang FROM users WHERE user_id=?", (uid,))
    d = cursor.fetchone()
    return d[0] if d else None

def save_reminder(uid, msg, stype, tval, rep):
    cursor.execute("INSERT INTO reminders (user_id, message, schedule_type, time_value, repeat) VALUES (?,?,?,?,?)",
        (uid, msg, stype, tval, rep))
    conn.commit()
    rid = cursor.lastrowid
    asyncio.create_task(save_backup_async())
    return rid

def set_completed(rem_id):
    cursor.execute("UPDATE reminders SET status='completed' WHERE id=?", (rem_id,))
    conn.commit()
    asyncio.create_task(save_backup_async())

def add_job_map(rem_id, job_id):
    cursor.execute("INSERT INTO scheduled_jobs(reminder_id, job_id) VALUES (?,?)", (rem_id, job_id))
    conn.commit()
    asyncio.create_task(save_backup_async())

def get_jobs(rem_id):
    cursor.execute("SELECT job_id FROM scheduled_jobs WHERE reminder_id=?", (rem_id,))
    return [i[0] for i in cursor.fetchall()]

def remove_mapping(rem_id):
    cursor.execute("DELETE FROM scheduled_jobs WHERE reminder_id=?", (rem_id,))
    conn.commit()
    asyncio.create_task(save_backup_async())

def get_user_reminders(uid):
    cursor.execute("SELECT id,message,schedule_type,time_value,repeat,status FROM reminders WHERE user_id=?", (uid,))
    return cursor.fetchall()

# ===============================================================
# GitHub Backup helpers (sync calls wrapped in asyncio.to_thread)
# - load_backup_from_github(): fetch file and populate DB if DB empty
# - save_backup_to_github(content): PUT file (create/update)
# - save_backup_async(): gather DB into JSON and upload
# ===============================================================
GITHUB_API_HEADERS = None
if GITHUB_TOKEN:
    GITHUB_API_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "User-Agent": "notify-bot"}

def github_get_file():
    """GET file content & sha from GitHub (sync). Return (content_str, sha) or (None,None)."""
    if not GITHUB_TOKEN or not GITHUB_USER or not GITHUB_REPO:
        return None, None
    url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{BACKUP_FILE}"
    resp = requests.get(url, headers=GITHUB_API_HEADERS, timeout=15)
    if resp.status_code == 200:
        j = resp.json()
        content = base64.b64decode(j["content"]).decode()
        sha = j.get("sha")
        return content, sha
    return None, None

def github_put_file(content_str, sha=None):
    """PUT (create/update) file in GitHub (sync)."""
    if not GITHUB_TOKEN or not GITHUB_USER or not GITHUB_REPO:
        return False, "missing github config"
    url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{BACKUP_FILE}"
    payload = {
        "message": f"backup: update {BACKUP_FILE} by bot",
        "content": base64.b64encode(content_str.encode()).decode()
    }
    if sha:
        payload["sha"] = sha
    resp = requests.put(url, headers=GITHUB_API_HEADERS, json=payload, timeout=20)
    return (resp.status_code in (200,201)), resp.text

async def load_backup_from_github():
    """Load backup and populate DB if DB empty (non-blocking wrapper)."""
    content, sha = await asyncio.to_thread(github_get_file)
    if not content:
        return
    try:
        data = json.loads(content)
    except:
        return

    # if DB already has reminders, skip loading to avoid duplicates
    cursor.execute("SELECT COUNT(1) FROM reminders")
    if cursor.fetchone()[0] > 0:
        return

    # load users
    for u in data.get("users", []):
        try:
            cursor.execute("INSERT OR REPLACE INTO users (user_id, lang) VALUES (?,?)", (u["user_id"], u.get("lang","bn")))
        except:
            pass

    # load reminders
    for r in data.get("reminders", []):
        try:
            cursor.execute("""
                INSERT INTO reminders (id, user_id, message, schedule_type, time_value, repeat, status)
                VALUES (?,?,?,?,?,?,?)
            """, (
                r.get("id"),
                r.get("user_id"),
                r.get("message"),
                r.get("schedule_type"),
                r.get("time_value"),
                r.get("repeat",0),
                r.get("status","active")
            ))
        except Exception:
            # fallback: insert without id
            try:
                cursor.execute("""
                    INSERT INTO reminders (user_id, message, schedule_type, time_value, repeat, status)
                    VALUES (?,?,?,?,?,?)
                """, (
                    r.get("user_id"),
                    r.get("message"),
                    r.get("schedule_type"),
                    r.get("time_value"),
                    r.get("repeat",0),
                    r.get("status","active")
                ))
            except:
                pass

    # load job mappings
    for j in data.get("scheduled_jobs", []):
        try:
            cursor.execute("INSERT INTO scheduled_jobs (reminder_id, job_id) VALUES (?,?)", (j.get("reminder_id"), j.get("job_id")))
        except:
            pass

    conn.commit()

async def save_backup_async():
    """Collect DB state to JSON and upload to GitHub (non-blocking)."""
    def build_backup():
        out = {"users": [], "reminders": [], "scheduled_jobs": []}
        cursor.execute("SELECT user_id, lang FROM users")
        for u in cursor.fetchall():
            out["users"].append({"user_id": u[0], "lang": u[1]})
        cursor.execute("SELECT id, user_id, message, schedule_type, time_value, repeat, status FROM reminders")
        for r in cursor.fetchall():
            out["reminders"].append({
                "id": r[0],
                "user_id": r[1],
                "message": r[2],
                "schedule_type": r[3],
                "time_value": r[4],
                "repeat": r[5],
                "status": r[6]
            })
        cursor.execute("SELECT reminder_id, job_id FROM scheduled_jobs")
        for s in cursor.fetchall():
            out["scheduled_jobs"].append({"reminder_id": s[0], "job_id": s[1]})
        return json.dumps(out, ensure_ascii=False, indent=2)

    content_str = await asyncio.to_thread(build_backup)
    # fetch current sha
    content, sha = await asyncio.to_thread(github_get_file)
    success, resp = await asyncio.to_thread(github_put_file, content_str, sha)
    if not success:
        logging.error("GitHub backup failed: %s", resp)

# ===============================================================
# SCHEDULER + send_reminder (uses GLOBAL_BOT fallback)
# ===============================================================
scheduler = AsyncIOScheduler()
scheduler.start()

GLOBAL_BOT = None  # assigned in main()

async def send_reminder(user_id, message, context=None, rem_id: int = None):
    """
    Send reminder safely using:
      - context.bot if available
      - context itself if Bot instance
      - GLOBAL_BOT fallback
    """
    bot = None
    if context is not None and hasattr(context, "bot"):
        bot = context.bot
    elif context is not None and context.__class__.__name__ == "Bot":
        bot = context
    elif GLOBAL_BOT is not None:
        bot = GLOBAL_BOT
    else:
        logging.error("No bot available to send reminder")
        return

    try:
        await bot.send_message(chat_id=user_id, text=f"⏰ Reminder:\n{message}")
    except Exception as e:
        logging.error("Reminder send error: %s", e)

    # mark completed if one-shot
    if rem_id:
        try:
            set_completed(rem_id)
            remove_mapping(rem_id)
        except Exception as e:
            logging.error("Mark completed error: %s", e)

# ===============================================================
# FORCED JOIN CHECK & UI helpers
# ===============================================================
async def check_join_status(user_id, context):
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
    msg = update.message or update.callback_query.message
    await msg.reply_text(t(user_id, "force_join_text"), reply_markup=InlineKeyboardMarkup(btn), parse_mode="Markdown")

async def send_language_menu(update: Update, context):
    msg = update.message or update.callback_query.message
    btn = [[InlineKeyboardButton("🇧🇩 বাংলা", callback_data="lang_bn"), InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")]]
    await msg.reply_text("🌐 Select your language:", reply_markup=InlineKeyboardMarkup(btn))

# ===============================================================
# /start handler
# ===============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_join_status(user_id, context):
        return await send_force_join_message(update, context)
    lang = get_lang(user_id)
    if not lang:
        return await send_language_menu(update, context)
    text = "আপনার বর্তমান ভাষা: বাংলা 🇧🇩\nআপনি কি পরিবর্তন করতে চান?" if lang == "bn" else "Your current language is English 🇬🇧\nDo you want to change it?"
    btn = [[InlineKeyboardButton("🌐 Change Language", callback_data="change_lang")], [InlineKeyboardButton("➡️ Continue", callback_data="go_ahead")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(btn))

# ===============================================================
# /set_reminder handler (shows menu)
# ===============================================================
async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_join_status(user_id, context):
        return await send_force_join_message(update, context)
    if not get_lang(user_id):
        return await update.message.reply_text(t(user_id, "select_lang_first"))
    btn = [
        [InlineKeyboardButton("⏱ Minutes / Hours", callback_data="rem_min_hour")],
        [InlineKeyboardButton("📅 Date", callback_data="rem_date")],
        [InlineKeyboardButton("🔁 Daily", callback_data="rem_daily")]
    ]
    await update.message.reply_text(t(user_id, "choose_type"), reply_markup=InlineKeyboardMarkup(btn))

# ===============================================================
# /notify_user (admin proxy start)
# ===============================================================
async def notify_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return await update.message.reply_text("❌ You are not allowed.")
    await update.message.reply_text("🔔 কাকে Notify করতে চান?\nUser ID দিন অথবা @username লিখুন:")
    context.user_data["mode"] = "notify_select_user"

# ===============================================================
# CALLBACK handler (inline buttons)
# ===============================================================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = q.from_user.id
    try:
        await q.answer()
    except:
        pass

    # verify join
    if q.data == "verify_join":
        if not await check_join_status(user_id, context):
            btn = [[InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{FORCED_CHANNEL.replace('@','')}"), InlineKeyboardButton("✔ Verify", callback_data="verify_join")]]
            return await q.edit_message_text("⚠️ You have not joined yet!", reply_markup=InlineKeyboardMarkup(btn))
        return await q.edit_message_text("✔ Verified! Now send /start")

    # change language flow
    if q.data == "change_lang":
        return await send_language_menu(update, context)
    if q.data == "go_ahead":
        return await q.edit_message_text(t(user_id, "start_ready"))
    if q.data == "lang_bn":
        save_lang(user_id, "bn")
        return await q.edit_message_text("🇧🇩 বাংলা সেট হয়েছে ✔\n/start দিন")
    if q.data == "lang_en":
        save_lang(user_id, "en")
        return await q.edit_message_text("🇬🇧 English set ✔\nUse /start")

    # Reminder type selections
    if q.data == "rem_min_hour":
        context.user_data["mode"] = "min_hour"
        return await q.edit_message_text(t(user_id, "enter_min_hour"), parse_mode="Markdown")
    if q.data == "rem_date":
        context.user_data["mode"] = "date_select"
        return await q.edit_message_text(t(user_id, "date_prompt"))
    if q.data == "rem_daily":
        btn = [[InlineKeyboardButton("🕛 Single Time", callback_data="daily_single")], [InlineKeyboardButton("🕒 Multiple Time", callback_data="daily_multi")]]
        return await q.edit_message_text("🔁 Daily Reminder:", reply_markup=InlineKeyboardMarkup(btn))

    if q.data == "daily_single":
        context.user_data["mode"] = "daily_single_time"
        return await q.edit_message_text(t(user_id, "daily_single_time_prompt"))
    if q.data == "daily_multi":
        context.user_data["mode"] = "daily_multi_time"
        return await q.edit_message_text(t(user_id, "daily_multi_time_prompt"))

    # Repeat buttons
    if q.data == "repeat_yes":
        context.user_data["mode"] = "repeat_count"
        return await q.edit_message_text("🔁 কয়বার Repeat করতে চান?\nউদাহরণ: 2 / 3 / 5")

    if q.data == "repeat_no":
        target_id = context.user_data.get("notify_target", user_id)
        msg = context.user_data.get("msg")
        tval = context.user_data.get("time")
        if not msg or not tval:
            return await q.edit_message_text("⚠️ Invalid state. Please set reminder again.")
        rem_id = save_reminder(target_id, msg, "min_hour", tval, 0)
        seconds = int(tval[:-1]) * (60 if tval.endswith("m") else 3600)
        run_time = datetime.now() + timedelta(seconds=seconds)
        job = scheduler.add_job(send_reminder, trigger="date", run_date=run_time, kwargs={"user_id": target_id, "message": msg, "context": context, "rem_id": rem_id})
        try:
            add_job_map(rem_id, job.id)
        except Exception as e:
            logging.error(f"Job mapping error: {e}")
        context.user_data.clear()
        return await q.edit_message_text(f"✅ Reminder Successfully Set!\n📝 Message: {msg}\n⏱ Time: {tval}\n🔁 Repeat: No\n📌 Your reminder is now active.")

# ===============================================================
# TEXT handler (handles interactive flows)
# ===============================================================
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # ADMIN notify select user
    if context.user_data.get("mode") == "notify_select_user":
        raw = text.strip()
        target_id = None
        if raw.startswith("@"):
            username = raw[1:]
            try:
                chat = await context.bot.get_chat(username)
                target_id = chat.id
            except:
                return await update.message.reply_text("❌ User not found or username invalid.")
        else:
            if not raw.isdigit():
                return await update.message.reply_text("❌ Invalid ID. Use numeric ID or @username.")
            target_id = int(raw)

        context.user_data["notify_target"] = target_id
        context.user_data["mode"] = "notify_type"
        btn = [
            [InlineKeyboardButton("⏱ Minutes/Hours", callback_data="rem_min_hour")],
            [InlineKeyboardButton("📅 Date", callback_data="rem_date")],
            [InlineKeyboardButton("🔁 Daily", callback_data="rem_daily")]
        ]
        return await update.message.reply_text("রিমাইন্ডার টাইপ নির্বাচন করুন:", reply_markup=InlineKeyboardMarkup(btn))

    # MINUTES/HOURS - step1 time
    if context.user_data.get("mode") == "min_hour" and "time" not in context.user_data:
        if not (text.endswith("m") or text.endswith("h")):
            return await update.message.reply_text(t(user_id, "wrong_format"))
        context.user_data["time"] = text
        context.user_data["mode"] = "min_hour_msg"
        return await update.message.reply_text(t(user_id, "enter_message"))

    # MINUTES/HOURS - step2 msg -> ask repeat
    if context.user_data.get("mode") == "min_hour_msg":
        context.user_data["msg"] = text
        btn = [[InlineKeyboardButton("✔ YES", callback_data="repeat_yes"), InlineKeyboardButton("✖ NO", callback_data="repeat_no")]]
        return await update.message.reply_text("🔁 আপনি কি Repeat করতে চান?", reply_markup=InlineKeyboardMarkup(btn))

    # MINUTES/HOURS - step3 repeat count
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
            run_time = datetime.now() + timedelta(seconds=seconds * (i + 1))
            job = scheduler.add_job(send_reminder, trigger="date", run_date=run_time, kwargs={"user_id": target, "message": msg, "context": context, "rem_id": rem_id})
            add_job_map(rem_id, job.id)
        context.user_data.clear()
        return await update.message.reply_text(f"✅ Reminder Successfully Set!\n📝 Message: {msg}\n⏱ Time: {tval}\n🔁 Repeat: {repeat_count} times\n📌 Your reminder is now active.")

    # DATE - step1 date
    if context.user_data.get("mode") == "date_select":
        try:
            datetime.strptime(text, "%d/%m/%y")
        except:
            return await update.message.reply_text("⚠️ তারিখ ঠিক ফরম্যাটে দিন (15/11/25)")
        context.user_data["date"] = text
        context.user_data["mode"] = "date_time"
        return await update.message.reply_text(t(user_id, "time_prompt"))

    # DATE - step2 time
    if context.user_data.get("mode") == "date_time":
        try:
            datetime.strptime(text, "%I.%M %p")
        except:
            return await update.message.reply_text("⚠️ সময় ঠিক ফরম্যাট (10.15 PM)")
        context.user_data["time"] = text
        context.user_data["mode"] = "date_message"
        return await update.message.reply_text(t(user_id, "enter_message_date"))

    # DATE - step3 message -> schedule & summary
    if context.user_data.get("mode") == "date_message":
        msg = text
        date_str = context.user_data.get("date")
        time_str = context.user_data.get("time")
        target = context.user_data.get("notify_target", user_id)
        try:
            dt = datetime.strptime(f"{date_str} {time_str}", "%d/%m/%y %I.%M %p")
        except:
            return await update.message.reply_text("⚠️ Date/time parsing failed. Check formats.")
        rem_id = save_reminder(target, msg, "date", f"{date_str} {time_str}", 0)
        job = scheduler.add_job(send_reminder, trigger="date", run_date=dt, kwargs={"user_id": target, "message": msg, "context": context, "rem_id": rem_id})
        add_job_map(rem_id, job.id)
        context.user_data.clear()
        return await update.message.reply_text(f"✅ Reminder Successfully Set!\n📝 Message: {msg}\n📅 Date: {date_str}\n⏱ Time: {time_str}\n🔁 Repeat: No\n📌 Your reminder is now active.")

    # DAILY single time
    if context.user_data.get("mode") == "daily_single_time":
        try:
            datetime.strptime(text, "%I.%M %p")
        except:
            return await update.message.reply_text(t(user_id, "wrong_time_format"))
        context.user_data["daily_times"] = [text]
        context.user_data["mode"] = "daily_msg"
        return await update.message.reply_text(t(user_id, "enter_message_daily"))

    # DAILY multi times
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

    # DAILY message -> schedule cron jobs
    if context.user_data.get("mode") == "daily_msg":
        msg = text
        times = context.user_data.get("daily_times", [])
        target = context.user_data.get("notify_target", user_id)
        rem_id = save_reminder(target, msg, "daily", ";".join(times), 0)
        for tstr in times:
            dt_obj = datetime.strptime(tstr, "%I.%M %p")
            hour, minute = dt_obj.hour, dt_obj.minute
            job = scheduler.add_job(send_reminder, trigger="cron", hour=hour, minute=minute, kwargs={"user_id": target, "message": msg, "context": context, "rem_id": None})
            add_job_map(rem_id, job.id)
        context.user_data.clear()
        return await update.message.reply_text(f"✅ Reminder Successfully Set!\n📝 Message: {msg}\n⏱ Time: {', '.join(times)}\n🔁 Repeat: Daily\n📌 Your reminder is now active.")

    # nothing matched
    return

# ===============================================================
# show active reminders
# ===============================================================
async def show_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user_reminders(user_id)
    active = [i for i in data if i[5] == "active"]
    if not active:
        return await update.message.reply_text("📭 কোনো Active Reminder নেই।")
    text = "📋 *Active Reminders:*\n\n"
    for rid, msg, stype, tval, rep, status in active:
        text += f"🆔 ID: {rid}\n📝 Message: {msg}\n"
        if stype == "min_hour":
            text += f"⏱ Time: {tval}\n🔁 Repeat: {rep}\n"
        elif stype == "date":
            d = tval.split(" ")
            text += f"📅 {d[0]}\n⏱ {' '.join(d[1:])}\n"
        else:
            text += f"⏱ {tval.replace(';', ', ')}\n🔁 Daily\n"
        text += "\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")

# ===============================================================
# show completed reminders
# ===============================================================
async def show_completed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cursor.execute("SELECT id,message,schedule_type,time_value,repeat FROM reminders WHERE user_id=? AND status='completed'", (user_id,))
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("📦 No completed reminders.")
    txt = "📦 *Completed Reminders:*\n\n"
    for rid, msg, stype, tval, rep in rows:
        txt += f"🆔 ID: {rid}\n📝 Message: {msg}\n⏱ Time:  {tval}\n🔁 Repeat: {rep}\n\n"
    await update.message.reply_text(txt, parse_mode="Markdown")

# ===============================================================
# clear completed
# ===============================================================
async def clear_completed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cursor.execute("DELETE FROM reminders WHERE user_id=? AND status='completed'", (user_id,))
    conn.commit()
    asyncio.create_task(save_backup_async())
    await update.message.reply_text("🧹 Completed reminders cleared!")

# ===============================================================
# delete single reminder via /delete_reminder_<id>
# ===============================================================
async def delete_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    asyncio.create_task(save_backup_async())
    await update.message.reply_text("🗑 Reminder deleted!")

# ===============================================================
# help command
# ===============================================================
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🧠 *ব্যবহার করা খুব সহজ!*\n\n"
        "• `/start` → ভাষা নির্বাচন\n"
        "• `/set_reminder` → রিমাইন্ডার সেট\n"
        "• `/show_reminder` → সক্রিয় রিমাইন্ডার দেখুন\n"
        "• `/show_completed` → সম্পন্ন রিমাইন্ডার তালিকা\n"
        "• `/delete_reminder` → রিমাইন্ডার ডিলিট\n"
        "\n"
        "যেকোনো সময় সাহায্যের জন্য আবার `/help` ব্যবহার করুন।"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ---------------------------
# Simple aiohttp ping server (Render keep alive)
# ---------------------------
async def handle_ping(request):
    return web.Response(text="ok")

async def run_ping_server(host="0.0.0.0", port=8000):
    app = web.Application()
    app.router.add_get("/ping", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"Ping server running on http://{host}:{port}/ping")

# ===============================================================
# reload scheduled jobs from DB on startup
# ===============================================================
def reload_scheduled_jobs(app):
    cursor.execute("SELECT id, user_id, message, schedule_type, time_value, repeat FROM reminders WHERE status='active'")
    rows = cursor.fetchall()
    for rem_id, uid, msg, stype, tval, rep in rows:
        try:
            if stype == "min_hour":
                seconds = int(tval[:-1]) * (60 if tval.endswith("m") else 3600)
                run_time = datetime.now() + timedelta(seconds=seconds)
                job = scheduler.add_job(send_reminder, trigger="date", run_date=run_time, kwargs={"user_id": uid, "message": msg, "rem_id": rem_id})
                add_job_map(rem_id, job.id)
            elif stype == "date":
                dt = datetime.strptime(tval, "%d/%m/%y %I.%M %p")
                if dt > datetime.now():
                    job = scheduler.add_job(send_reminder, trigger="date", run_date=dt, kwargs={"user_id": uid, "message": msg, "rem_id": rem_id})
                    add_job_map(rem_id, job.id)
            elif stype == "daily":
                times = tval.split(";")
                for tstr in times:
                    dt_obj = datetime.strptime(tstr, "%I.%M %p")
                    hour, minute = dt_obj.hour, dt_obj.minute
                    job = scheduler.add_job(send_reminder, trigger="cron", hour=hour, minute=minute, kwargs={"user_id": uid, "message": msg, "rem_id": None})
                    add_job_map(rem_id, job.id)
        except Exception as e:
            logging.error("Reload job error: %s", e)

# ===============================================================
# MAIN - build app, start ping server, load backup, reload jobs, run polling
# ===============================================================
def main():
    global GLOBAL_BOT

    app = Application.builder().token(BOT_TOKEN).build()
    GLOBAL_BOT = app.bot

    # start ping server using Render's PORT env
    loop = asyncio.get_event_loop()
    port = int(os.getenv("PORT", "8000"))
    loop.create_task(run_ping_server(host="0.0.0.0", port=port))

    # load backup from GitHub (if configured)
    if GITHUB_TOKEN and GITHUB_USER and GITHUB_REPO:
        # run sync load in background to avoid blocking
        loop.run_until_complete(load_backup_from_github())

    # add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set_reminder", set_reminder))
    app.add_handler(CommandHandler("show_reminder", show_reminder))
    app.add_handler(CommandHandler("show_completed", show_completed))
    app.add_handler(CommandHandler("clear_completed", clear_completed))
    app.add_handler(CommandHandler("notify_user", notify_user))
    app.add_handler(MessageHandler(filters.Regex(r"^/delete_reminder_\d+$"), delete_reminder))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CommandHandler("help", help_command))

    # reload scheduled jobs from DB
    reload_scheduled_jobs(app)

    print("Reminder Bot Running...")
    app.run_polling()

if __name__ == "__main__":
    main()
