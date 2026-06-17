import sqlite3

try:
    conn = sqlite3.connect("academic_bot.db")
    cursor = conn.cursor()
    cursor.execute("ALTER TABLE academic_items ADD COLUMN raw_text TEXT;")
    print("Added raw_text column.")
except Exception as e:
    print("Error adding raw_text:", e)

try:
    cursor.execute("ALTER TABLE academic_items ADD COLUMN updated_at DATETIME;")
    print("Added updated_at column.")
except Exception as e:
    print("Error adding updated_at:", e)

conn.commit()
conn.close()
