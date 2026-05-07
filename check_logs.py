import sqlite3

def check_emails():
    conn = sqlite3.connect('agency.db')
    conn.row_factory = sqlite3.Row
    # Look specifically for email-related logs
    query = "SELECT created_at, message FROM activity_log WHERE message LIKE '%email%' ORDER BY id DESC LIMIT 20"
    
    rows = conn.execute(query).fetchall()
    
    if not rows:
        print("No email logs found yet.")
    else:
        for r in rows:
            print(f"[{r['created_at']}] {r['message']}")

import sqlite3

def diagnose_db():
    try:
        conn = sqlite3.connect('agency.db')
        # Check if the table exists first
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='activity_log';")
        if not cursor.fetchone():
            print("❌ Error: Table 'activity_log' does not exist in agency.db.")
            return

        # Get column names
        cursor.execute("PRAGMA table_info(activity_log)")
        columns = [col[1] for col in cursor.fetchall()]
        print(f"✅ Found table 'activity_log' with columns: {columns}")

        # Fetch the last 5 entries of ANY type
        cursor.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 5")
        rows = cursor.fetchall()

        if not rows:
            print("⚠️ Table is completely empty. The main script isn't saving anything.")
        else:
            print("\n--- Last 5 Activity Logs ---")
            for r in rows:
                print(r)

    except Exception as e:
        print(f"❌ Database Error: {e}")

if __name__ == "__main__":
    diagnose_db()
if __name__ == "__main__":
    check_emails()