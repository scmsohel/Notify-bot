# ================================
#   Notify Bot (Render Webhook)
#   Final Clean Production Build
#   No Polling – No Conflicts
# ================================

import asyncio
import os
import logging
import sqlite3
import json
import base64
from datetime import datetime, timedelta
from dotenv import load_dotenv
import requests
from aiohttp import web

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Timezone
try:
    from zoneinfo import ZoneInfo
except:
    ZoneInfo = None

# ================================
# Logging
# ================================
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.ERROR
)

# ================================
# Environment
# ================================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Render site URL
FORCED_CHANNEL = os.getenv("FORCED_CHANNEL")
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USER = os.getenv("GITHUB_USER")
GITHUB_REPO = os.getenv("GITHUB_REPO")
BACKUP_FILE = os.getenv("BACKUP_FILE", "backup.json")

DB_PATH = os.getenv("DB_PATH", "bot.db")
TZ = os.getenv("TZ", "Asia/Dhaka")

# timezone
_tzinfo = None
if ZoneInfo:
    try:
        _tzinfo = ZoneInfo(TZ)
    except:
        _tzinfo = None

# ================================
# SQLite Init
# ================================
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
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
    repeat INTEGER,
    status TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    reminder_id INTEGER,
    job_id TEXT
)
""")

conn.commit()

# ================================
# Language Text
# ================================
LANG = {
    "bn": {
        "force_join_text": "🚫 বট ব্যবহার করতে চ্যানেলে Join করুন নিচের বাটন থেকে:",
        "select_lang_first": "🔰 প্রথমে ভাষা নির্বাচন করুন (/start)।",
        "choose_type": "🕹 রিমাইন্ডার টাইপ নির্বাচন করুন:",
        "enter_min_hour": "⏱ সময় দিন (উদাহরণ: 2m / 1h)",
        "wrong_format": "⚠️ ভুল ফরম্যাট! উদাহরণ 2m / 1h",
        "enter_message": "✍ এখন মেসেজ লিখুন:",
        "date_prompt": "📅 তারিখ লিখুন (15/11/25)",
        "time_prompt": "⏱ সময় লিখুন (10.15 PM)",
        "enter_message_date": "✍ রিমাইন্ডারের মেসেজ লিখুন:",
        "start_ready": "✔ এখন আপনি বট ব্যবহার করতে পারবেন।",
        "daily_single_time_prompt": "⏱ প্রতিদিন একটি সময় দিন (10.00 AM)",
        "daily_multi_time_prompt": "⏱ প্রতিদিন একাধিক সময় দিন (প্রতিটি নতুন লাইনে)",
        "wrong_time_format": "⚠️ সময় ফরম্যাট ভুল!",
        "enter_message_daily": "✍ দৈনিক রিমাইন্ডার মেসেজ লিখুন:"
    }
}

def t(uid, key):
    try:
        lang = get_lang(uid)
        if not lang:
            lang = "bn"
        return LANG[lang][key]
    except:
        return key

# ================================
# DB Helper Functions
# ================================
def save_lang(uid, lang):
    cursor.execute("INSERT OR REPLACE INTO users (user_id, lang) VALUES (?,?)", (uid, lang))
    conn.commit()
    try:
        asyncio.create_task(save_backup_async())
    except:
        pass

def get_lang(uid):
    cursor.execute("SELECT lang FROM users WHERE user_id=?", (uid,))
    d = cursor.fetchone()
    return d[0] if d else None

def save_reminder(uid, msg, stype, tval, rep):
    cursor.execute("""
        INSERT INTO reminders (user_id, message, schedule_type, time_value, repeat, status)
        VALUES (?,?,?,?,?,?)
    """, (uid, msg, stype, tval, rep, "active"))
    conn.commit()
    r = cursor.lastrowid
    try:
        asyncio.create_task(save_backup_async())
    except:
        pass
    return r

def add_job_map(rem_id, job_id):
    cursor.execute("INSERT INTO scheduled_jobs (reminder_id, job_id) VALUES (?,?)",(rem_id, job_id))
    conn.commit()

def get_jobs(rid):
    cursor.execute("SELECT job_id FROM scheduled_jobs WHERE reminder_id=?", (rid,))
    return [i[0] for i in cursor.fetchall()]

def remove_mapping(rid):
    cursor.execute("DELETE FROM scheduled_jobs WHERE reminder_id=?", (rid,))
    conn.commit()

def set_completed(rid):
    cursor.execute("UPDATE reminders SET status='completed' WHERE id=?", (rid,))
    conn.commit()

def get_user_reminders(uid):
    cursor.execute("""
        SELECT id, message, schedule_type, time_value, repeat, status 
        FROM reminders WHERE user_id=?
    """, (uid,))
    return cursor.fetchall()

# ================================
# GitHub Backup Support
# ================================
GITHUB_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "User-Agent": "notify-bot"} if GITHUB_TOKEN else None

def github_get():
    if not GITHUB_HEADERS:
        return None, None
    url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{BACKUP_FILE}"
    r = requests.get(url, headers=GITHUB_HEADERS)
    if r.status_code == 200:
        j = r.json()
        return base64.b64decode(j["content"]).decode(), j["sha"]
    return None, None

def github_put(content, sha):
    if not GITHUB_HEADERS:
        return False
    url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{BACKUP_FILE}"
    payload = {
        "message": "backup update",
        "content": base64.b64encode(content.encode()).decode()
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=GITHUB_HEADERS, json=payload)
    return r.status_code in (200,201)

async def save_backup_async():
    data = {
        "users": [],
        "reminders": [],
        "scheduled_jobs": []
    }

    cur = conn.cursor()
    cur.execute("SELECT user_id, lang FROM users")
    for u in cur.fetchall():
        data["users"].append({"user_id": u[0], "lang": u[1]})

    cur.execute("SELECT id,user_id,message,schedule_type,time_value,repeat,status FROM reminders")
    for r in cur.fetchall():
        data["reminders"].append({
            "id": r[0],
            "user_id": r[1],
            "message": r[2],
            "schedule_type": r[3],
            "time_value": r[4],
            "repeat": r[5],
            "status": r[6]
        })

    cur.execute("SELECT reminder_id, job_id FROM scheduled_jobs")
    for j in cur.fetchall():
        data["scheduled_jobs"].append({"reminder_id": j[0], "job_id": j[1]})

    content = json.dumps(data, indent=2)
    old, sha = github_get()
    github_put(content, sha)

# ================================
# Scheduler + Sending Reminder
# ================================
scheduler = AsyncIOScheduler(timezone=_tzinfo) if _tzinfo else AsyncIOScheduler()
scheduler.start()

GLOBAL_BOT = None

async def send_reminder(uid, msg, context=None, rem_id=None):
    bot = GLOBAL_BOT
    try:
        await bot.send_message(chat_id=uid, text=f"⏰ Reminder:\n{msg}")
    except Exception as e:
        logging.error(e)

    if rem_id:
        set_completed(rem_id)
        remove_mapping(rem_id)

# ================================
# Forced Join Check
# ================================
async def check_join(uid, context):
    if not FORCED_CHANNEL:
        return True
    try:
        m = await context.bot.get_chat_member(FORCED_CHANNEL, uid)
        return m.status in ["member","administrator","creator"]
    except:
        return False

async def force_join_msg(update, context):
    uid = update.effective_user.id
    btn = [[
        InlineKeyboardButton("📢 Join", url=f"https://t.me/{FORCED_CHANNEL.replace('@','')}"),
        InlineKeyboardButton("✔ Verify", callback_data="verify_join")
    ]]
    msg = update.message or update.callback_query.message
    await msg.reply_text(t(uid, "force_join_text"), reply_markup=InlineKeyboardMarkup(btn))

# ================================
# Language Menu
# ================================
async def lang_menu(update, context):
    msg = update.message or update.callback_query.message
    btn = [
        [InlineKeyboardButton("🇧🇩 বাংলা", callback_data="lang_bn")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")]
    ]
    await msg.reply_text("🌐 Choose Language:", reply_markup=InlineKeyboardMarkup(btn))

# ================================
# Commands
# ================================
async def start(update, context):
    uid = update.effective_user.id
    if not await check_join(uid, context):
        return await force_join_msg(update, context)

    lang = get_lang(uid)
    if not lang:
        return await lang_menu(update, context)

    btn = [
        [InlineKeyboardButton("🌐 Change Language", callback_data="change_lang")],
        [InlineKeyboardButton("➡️ Continue", callback_data="go_ahead")]
    ]
    text = "আপনার ভাষা বাংলা 🇧🇩" if lang=="bn" else "Your language: English"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(btn))

async def help_cmd(update, context):
    uid = update.effective_user.id
    txt = (
        "🧠 Commands:\n\n"
        "/start\n"
        "/set_reminder\n"
        "/show_reminder\n"
        "/show_completed\n"
        "/clear_completed\n"
        "/delete_reminder_<id>\n"
    )
    await update.message.reply_text(txt)

async def set_reminder(update, context):
    uid = update.effective_user.id
    if not await check_join(uid, context):
        return await force_join_msg(update, context)
    if not get_lang(uid):
        return await update.message.reply_text(t(uid, "select_lang_first"))

    btn = [
        [InlineKeyboardButton("⏱ Minutes/Hours", callback_data="rem_min_hour")],
        [InlineKeyboardButton("📅 Date", callback_data="rem_date")],
        [InlineKeyboardButton("🔁 Daily", callback_data="rem_daily")]
    ]
    await update.message.reply_text(t(uid, "choose_type"), reply_markup=InlineKeyboardMarkup(btn))

# ================================
# Callback Handler
# ================================
async def callback(update, context):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()

    # verify join
    if q.data == "verify_join":
        if not await check_join(uid, context):
            return await q.edit_message_text("❌ Not joined!")
        return await q.edit_message_text("✔ Verified! Send /start")

    # Language
    if q.data == "change_lang":
        return await lang_menu(update, context)
    if q.data == "lang_bn":
        save_lang(uid, "bn")
        return await q.edit_message_text("✔ বাংলা সেট হয়েছে\n/start দিন")
    if q.data == "lang_en":
        save_lang(uid, "en")
        return await q.edit_message_text("✔ English set\n/start")

    if q.data == "go_ahead":
        return await q.edit_message_text(t(uid, "start_ready"))

    # =============================
    # Reminder Type
    # =============================
    if q.data == "rem_min_hour":
        context.user_data["mode"] = "min_hour"
        return await q.edit_message_text(t(uid, "enter_min_hour"))

    if q.data == "rem_date":
        context.user_data["mode"] = "date_select"
        return await q.edit_message_text(t(uid, "date_prompt"))

    if q.data == "rem_daily":
        btn = [
            [InlineKeyboardButton("🕛 Single Time", callback_data="daily_single")],
            [InlineKeyboardButton("🕒 Multi Time", callback_data="daily_multi")]
        ]
        return await q.edit_message_text("Daily Reminder:", reply_markup=InlineKeyboardMarkup(btn))

    if q.data == "daily_single":
        context.user_data["mode"] = "daily_single"
        return await q.edit_message_text(t(uid, "daily_single_time_prompt"))

    if q.data == "daily_multi":
        context.user_data["mode"] = "daily_multi"
        return await q.edit_message_text(t(uid, "daily_multi_time_prompt"))

# ================================
# TEXT Handler
# ================================
async def text_handler(update, context):
    uid = update.effective_user.id
    txt = update.message.text.strip()

    # ============= Minutes/Hours ============
    if context.user_data.get("mode") == "min_hour":
        if not (txt.endswith("m") or txt.endswith("h")):
            return await update.message.reply_text(t(uid, "wrong_format"))
        context.user_data["time"] = txt
        context.user_data["mode"] = "min_msg"
        return await update.message.reply_text(t(uid, "enter_message"))

    if context.user_data.get("mode") == "min_msg":
        msg = txt
        tval = context.user_data["time"]
        rem = save_reminder(uid, msg, "min_hour", tval, 0)
        sec = int(tval[:-1]) * (60 if tval.endswith("m") else 3600)
        run = datetime.now(tz=_tzinfo) + timedelta(seconds=sec)
        job = scheduler.add_job(send_reminder, trigger="date", run_date=run,
                                kwargs={"uid": uid, "msg": msg, "rem_id": rem})
        add_job_map(rem, job.id)
        context.user_data.clear()
        return await update.message.reply_text(f"✔ Reminder set\n{msg}\n{tval}")

    # ============= Date Reminder ============
    if context.user_data.get("mode") == "date_select":
        try:
            datetime.strptime(txt, "%d/%m/%y")
        except:
            return await update.message.reply_text("❌ Wrong date")
        context.user_data["date"] = txt
        context.user_data["mode"] = "date_time"
        return await update.message.reply_text(t(uid, "time_prompt"))

    if context.user_data.get("mode") == "date_time":
        try:
            datetime.strptime(txt, "%I.%M %p")
        except:
            return await update.message.reply_text("❌ Wrong time")
        context.user_data["time"] = txt
        context.user_data["mode"] = "date_msg"
        return await update.message.reply_text(t(uid, "enter_message_date"))

    if context.user_data.get("mode") == "date_msg":
        msg = txt
        d = context.user_data["date"]
        t = context.user_data["time"]
        dt = datetime.strptime(f"{d} {t}", "%d/%m/%y %I.%M %p")
        dt = dt.replace(tzinfo=_tzinfo) if _tzinfo else dt
        rem = save_reminder(uid, msg, "date", f"{d} {t}", 0)
        job = scheduler.add_job(send_reminder, trigger="date", run_date=dt,
                                kwargs={"uid": uid,"msg": msg,"rem_id": rem})
        add_job_map(rem, job.id)
        context.user_data.clear()
        return await update.message.reply_text("✔ Date reminder added")

    # ============= Daily Reminder ============
    if context.user_data.get("mode") == "daily_single":
        try:
            datetime.strptime(txt, "%I.%M %p")
        except:
            return await update.message.reply_text("❌ Wrong time")
        context.user_data["times"] = [txt]
        context.user_data["mode"] = "daily_msg"
        return await update.message.reply_text(t(uid, "enter_message_daily"))

    if context.user_data.get("mode") == "daily_multi":
        times = [i.strip() for i in txt.split("\n") if i.strip()]
        for t in times:
            try:
                datetime.strptime(t, "%I.%M %p")
            except:
                return await update.message.reply_text("❌ Wrong time!")
        context.user_data["times"] = times
        context.user_data["mode"] = "daily_msg"
        return await update.message.reply_text(t(uid, "enter_message_daily"))

    if context.user_data.get("mode") == "daily_msg":
        msg = txt
        times = context.user_data["times"]
        rem = save_reminder(uid, msg, "daily", ";".join(times), 0)

        for t in times:
            dt = datetime.strptime(t, "%I.%M %p")
            job = scheduler.add_job(send_reminder, trigger="cron",
                                    hour=dt.hour, minute=dt.minute,
                                    kwargs={"uid": uid, "msg": msg})
            add_job_map(rem, job.id)

        context.user_data.clear()
        return await update.message.reply_text("✔ Daily reminder set")

# ================================
# Show Reminders
# ================================
async def show_reminder(update, context):
    uid = update.effective_user.id
    data = get_user_reminders(uid)
    data = [i for i in data if i[5] == "active"]
    if not data:
        return await update.message.reply_text("📭 No active reminder.")

    out = "📋 Active Reminders:\n\n"
    for i in data:
        rid,msg,typ,tv,rep,st = i
        out += f"ID: {rid}\n{msg}\n{tv}\n\n"
    await update.message.reply_text(out)

async def show_completed(update, context):
    uid = update.effective_user.id
    cursor.execute("SELECT id,message FROM reminders WHERE user_id=? AND status='completed'",(uid,))
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("No completed reminders.")
    txt = "📦 Completed:\n\n"
    for r in rows:
        txt += f"{r[0]} — {r[1]}\n"
    await update.message.reply_text(txt)

async def clear_completed(update, context):
    uid = update.effective_user.id
    cursor.execute("DELETE FROM reminders WHERE user_id=? AND status='completed'", (uid,))
    conn.commit()
    await update.message.reply_text("✔ Cleared completed reminders!")

async def delete_reminder(update, context):
    uid = update.effective_user.id
    txt = update.message.text
    try:
        rid = int(txt.replace("/delete_reminder_", ""))
    except:
        return await update.message.reply_text("❌ Wrong ID")

    cursor.execute("SELECT id FROM reminders WHERE id=? AND user_id=?", (rid,uid))
    if not cursor.fetchone():
        return await update.message.reply_text("❌ Not found")

    jobs = get_jobs(rid)
    for j in jobs:
        try:
            scheduler.remove_job(j)
        except:
            pass

    remove_mapping(rid)
    cursor.execute("DELETE FROM reminders WHERE id=?", (rid,))
    conn.commit()

    await update.message.reply_text("🗑 Deleted")

# ================================
# Reload Jobs at Startup
# ================================
def reload_jobs():
    cursor.execute("SELECT id,user_id,message,schedule_type,time_value,repeat FROM reminders WHERE status='active'")
    for rid,uid,msg,typ,tv,rep in cursor.fetchall():
        try:
            if typ == "min_hour":
                sec = int(tv[:-1]) * (60 if tv.endswith("m") else 3600)
                run = datetime.now(tz=_tzinfo) + timedelta(seconds=sec)
                job = scheduler.add_job(send_reminder, trigger="date", run_date=run,
                                        kwargs={"uid":uid,"msg":msg,"rem_id":rid})
                add_job_map(rid, job.id)

            elif typ == "date":
                dt = datetime.strptime(tv, "%d/%m/%y %I.%M %p")
                dt = dt.replace(tzinfo=_tzinfo) if _tzinfo else dt
                if dt > (datetime.now(tz=_tzinfo) if _tzinfo else datetime.now()):
                    job = scheduler.add_job(send_reminder, trigger="date", run_date=dt,
                                            kwargs={"uid":uid,"msg":msg,"rem_id":rid})
                    add_job_map(rid, job.id)

            elif typ == "daily":
                times = tv.split(";")
                for t in times:
                    dt = datetime.strptime(t, "%I.%M %p")
                    job = scheduler.add_job(send_reminder, trigger="cron",
                                            hour=dt.hour, minute=dt.minute,
                                            kwargs={"uid":uid,"msg":msg})
                    add_job_map(rid, job.id)

        except Exception as e:
            logging.error(e)

# ================================
# Webhook Server (no polling)
# ================================
async def ping(request):
    return web.Response(text="ok")

def main():
    if not BOT_TOKEN:
        print("BOT_TOKEN missing!")
        return

    port = int(os.getenv("PORT", "8000"))
    webhook_path = f"webhook/{BOT_TOKEN}"
    webhook_url = f"{WEBHOOK_URL}/{webhook_path}"

    app = Application.builder().token(BOT_TOKEN).build()
    global GLOBAL_BOT
    GLOBAL_BOT = app.bot

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("set_reminder", set_reminder))
    app.add_handler(CommandHandler("show_reminder", show_reminder))
    app.add_handler(CommandHandler("show_completed", show_completed))
    app.add_handler(CommandHandler("clear_completed", clear_completed))
    app.add_handler(MessageHandler(filters.Regex(r"^/delete_reminder_\d+$"), delete_reminder))

    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Reload scheduled jobs
    reload_jobs()

    # aiohttp ping server
    async def run_web():
        wapp = web.Application()
        wapp.router.add_get("/ping", ping)
        runner = web.AppRunner(wapp)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()

    asyncio.get_event_loop().create_task(run_web())

    print("Webhook starting:", webhook_url)
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=webhook_url
    )

if __name__ == "__main__":
    main()
