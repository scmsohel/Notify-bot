import sqlite3

conn = sqlite3.connect("bot.db")
cursor = conn.cursor()

try:
    cursor.execute("ALTER TABLE reminders ADD COLUMN status TEXT DEFAULT 'active';")
    conn.commit()
    print("✔ Status column added successfully!")
except Exception as e:
    print("⚠️ Error:", e)

conn.close()
