"""
一次性脚本：补算历史 expired 记录的 ttl_seconds 和 duration。
用法：DATABASE_URL=postgres://... python fix_ttl_history.py
"""
import os
import psycopg2
from datetime import datetime, timezone


def parse_iso(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    parts = []
    for unit, label in [(86400, "d"), (3600, "h"), (60, "m"), (1, "s")]:
        v = seconds // unit
        if v:
            parts.append(f"{v}{label}")
        seconds %= unit
    return "".join(parts)


def main():
    dsn = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()

    cur.execute("""
        SELECT env_id, created_at, expires_at
        FROM pod_history
        WHERE status = 'expired'
          AND ttl_seconds = 0
          AND created_at IS NOT NULL AND created_at != ''
          AND expires_at IS NOT NULL AND expires_at != ''
    """)
    rows = cur.fetchall()
    print(f"待修复记录数: {len(rows)}")

    updated = 0
    for env_id, created_str, expires_str in rows:
        created_dt = parse_iso(created_str)
        expires_dt = parse_iso(expires_str)
        if not created_dt or not expires_dt:
            continue
        elapsed = max(0, int((expires_dt - created_dt).total_seconds()))
        duration = format_duration(elapsed)
        cur.execute(
            "UPDATE pod_history SET ttl_seconds = %s, duration = %s WHERE env_id = %s",
            (elapsed, duration, env_id),
        )
        updated += 1

    conn.commit()
    print(f"已修复: {updated} 条")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
