import sqlite3
import pandas as pd

DB_PATH = "AI Revenue Recovery Platform (SQLite).db"
conn = sqlite3.connect(DB_PATH)

print("=" * 60)
print("1. PAYMENT METHOD DISTRIBUTION (Target ~75% UPI)")
print("=" * 60)
df_methods = pd.read_sql_query(
    "SELECT payment_method, COUNT(*) as count, "
    "ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM payment_attempts), 2) as pct "
    "FROM payment_attempts GROUP BY payment_method ORDER BY count DESC;",
    conn
)
print(df_methods.to_string(index=False))

print("\n" + "=" * 60)
print("2. TOP ERROR CODES & FAILURE REASONS")
print("=" * 60)
# Retrieve actual columns of payment_attempts
cursor = conn.cursor()
cursor.execute("PRAGMA table_info(payment_attempts);")
cols = [col[1] for col in cursor.fetchall()]

# Pick error/status column dynamically
err_col = next((c for c in ['gateway_response_code', 'error_code', 'failure_reason', 'response_code'] if c in cols), None)

if err_col:
    df_errors = pd.read_sql_query(
        f"SELECT {err_col}, COUNT(*) as count FROM payment_attempts "
        f"WHERE {err_col} IS NOT NULL AND {err_col} != '' "
        f"GROUP BY {err_col} ORDER BY count DESC LIMIT 8;",
        conn
    )
    print(df_errors.to_string(index=False))
else:
    print(f"Available columns in payment_attempts: {cols}")

print("\n" + "=" * 60)
print("3. RECOVERY CHANNEL BREAKDOWN (Customer Contact History)")
print("=" * 60)
df_channels = pd.read_sql_query(
    "SELECT channel, COUNT(*) as count, "
    "ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM customer_contact_history), 2) as pct "
    "FROM customer_contact_history GROUP BY channel ORDER BY count DESC;",
    conn
)
print(df_channels.to_string(index=False))

print("\n" + "=" * 60)
print("4. AGENT DECISIONS & ACTIONS")
print("=" * 60)
df_decisions = pd.read_sql_query(
    "SELECT COUNT(*) as total_decisions FROM recovery_agent_decisions;",
    conn
)
df_actions = pd.read_sql_query(
    "SELECT COUNT(*) as total_actions FROM recovery_agent_actions;",
    conn
)
print(f"Decisions Logged: {df_decisions.iloc[0]['total_decisions']}")
print(f"Actions Executed: {df_actions.iloc[0]['total_actions']}")

print("\n" + "=" * 60)
print("5. RECOVERY INTERVENTIONS SCHEMA & INTEGRITY")
print("=" * 60)
cursor = conn.cursor()
cursor.execute("PRAGMA table_info(recovery_interventions);")
columns = [col[1] for col in cursor.fetchall()]
print(f"Columns in recovery_interventions: {columns}")

conn.close()