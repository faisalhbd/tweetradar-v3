"""
Multi-user SQLite database layer
Each user is isolated by user_id (UUID stored in cookie)
"""
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
import config

def get_conn():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # safe for multi-thread
    return conn

def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id           TEXT PRIMARY KEY,
            twitter_key  TEXT,
            tg_token     TEXT,
            tg_chat_id   TEXT,
            created_at   TEXT DEFAULT (datetime('now')),
            last_seen    TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS keywords (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT NOT NULL,
            keyword    TEXT NOT NULL,
            active     INTEGER DEFAULT 1,
            alert_on   INTEGER DEFAULT 1,
            hit_count  INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, keyword)
        );

        CREATE TABLE IF NOT EXISTS tweets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT NOT NULL,
            tweet_id   TEXT NOT NULL,
            username   TEXT,
            text       TEXT,
            tweet_url  TEXT,
            keyword    TEXT,
            created_at TEXT,
            likes      INTEGER DEFAULT 0,
            retweets   INTEGER DEFAULT 0,
            followers  INTEGER DEFAULT 0,
            logged_at  TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, tweet_id)
        );

        CREATE TABLE IF NOT EXISTS user_alert_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT NOT NULL,
            username   TEXT NOT NULL,
            alerted_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS settings (
            user_id TEXT NOT NULL,
            key     TEXT NOT NULL,
            value   TEXT,
            PRIMARY KEY (user_id, key)
        );
        """)
    print("✅ DB initialized")

# ══════════════════════════════════════════
#  USERS
# ══════════════════════════════════════════
def count_users() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

def create_user() -> str:
    uid = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute("INSERT INTO users (id) VALUES (?)", (uid,))
        # Insert default settings
        for k, v in config.DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (user_id, key, value) VALUES (?,?,?)",
                (uid, k, v)
            )
    return uid

def user_exists(uid: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone()
        return row is not None

def touch_user(uid: str):
    with get_conn() as conn:
        conn.execute("UPDATE users SET last_seen=datetime('now') WHERE id=?", (uid,))

def get_user(uid: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None

def update_user_keys(uid: str, twitter_key: str, tg_token: str, tg_chat_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET twitter_key=?, tg_token=?, tg_chat_id=? WHERE id=?",
            (twitter_key.strip(), tg_token.strip(), tg_chat_id.strip(), uid)
        )

def user_is_configured(uid: str) -> bool:
    u = get_user(uid)
    if not u:
        return False
    return bool(u.get("twitter_key") and u.get("tg_token") and u.get("tg_chat_id"))

# ══════════════════════════════════════════
#  SETTINGS
# ══════════════════════════════════════════
def get_setting(uid: str, key: str, default=None):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE user_id=? AND key=?", (uid, key)
        ).fetchone()
        return row[0] if row else default

def set_setting(uid: str, key: str, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?,?,?)",
            (uid, key, str(value))
        )

def get_all_settings(uid: str) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE user_id=?", (uid,)
        ).fetchall()
        base = dict(config.DEFAULT_SETTINGS)
        base.update({r[0]: r[1] for r in rows})
        return base

def save_settings_bulk(uid: str, data: dict):
    allowed = list(config.DEFAULT_SETTINGS.keys())
    with get_conn() as conn:
        for key in allowed:
            if key in data:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?,?,?)",
                    (uid, key, str(data[key]))
                )

# ══════════════════════════════════════════
#  KEYWORDS
# ══════════════════════════════════════════
def get_keywords(uid: str, active_only=False) -> list:
    with get_conn() as conn:
        if active_only:
            rows = conn.execute(
                "SELECT * FROM keywords WHERE user_id=? AND active=1 ORDER BY id",
                (uid,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM keywords WHERE user_id=? ORDER BY id", (uid,)
            ).fetchall()
        return [dict(r) for r in rows]

def add_keyword(uid: str, keyword: str) -> bool:
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO keywords (user_id, keyword) VALUES (?,?)",
                (uid, keyword.strip())
            )
        return True
    except sqlite3.IntegrityError:
        return False

def delete_keyword(uid: str, kid: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM keywords WHERE id=? AND user_id=?", (kid, uid))

def toggle_keyword(uid: str, kid: int, field: str):
    if field not in ("active", "alert_on"):
        return
    with get_conn() as conn:
        conn.execute(
            f"UPDATE keywords SET {field}=1-{field} WHERE id=? AND user_id=?",
            (kid, uid)
        )

def increment_hit(uid: str, keyword: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE keywords SET hit_count=hit_count+1 WHERE user_id=? AND keyword=?",
            (uid, keyword)
        )

def bulk_import_keywords(uid: str, keywords: list[str]) -> tuple[int, int]:
    added = skipped = 0
    for kw in keywords:
        if add_keyword(uid, kw):
            added += 1
        else:
            skipped += 1
    return added, skipped

# ══════════════════════════════════════════
#  TWEETS
# ══════════════════════════════════════════
def tweet_exists(uid: str, tweet_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM tweets WHERE user_id=? AND tweet_id=?", (uid, tweet_id)
        ).fetchone()
        return row is not None

def save_tweet(uid: str, data: dict):
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO tweets
            (user_id, tweet_id, username, text, tweet_url, keyword,
             created_at, likes, retweets, followers)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            uid, data["tweet_id"], data["username"], data["text"],
            data["tweet_url"], data["keyword"], data["created_at"],
            data.get("likes", 0), data.get("retweets", 0), data.get("followers", 0)
        ))

def get_recent_tweets(uid: str, limit=50, keyword=None, hours=None) -> list:
    with get_conn() as conn:
        conds  = ["user_id=?"]
        params = [uid]
        if keyword:
            conds.append("keyword=?")
            params.append(keyword)
        if hours:
            since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
            conds.append("logged_at >= ?")
            params.append(since)
        where = "WHERE " + " AND ".join(conds)
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM tweets {where} ORDER BY logged_at DESC LIMIT ?",
            params
        ).fetchall()
        return [dict(r) for r in rows]

def get_stats(uid: str) -> dict:
    with get_conn() as conn:
        total   = conn.execute("SELECT COUNT(*) FROM tweets WHERE user_id=?", (uid,)).fetchone()[0]
        today   = conn.execute(
            "SELECT COUNT(*) FROM tweets WHERE user_id=? AND logged_at>=date('now')", (uid,)
        ).fetchone()[0]
        kw_tot  = conn.execute("SELECT COUNT(*) FROM keywords WHERE user_id=?", (uid,)).fetchone()[0]
        kw_act  = conn.execute("SELECT COUNT(*) FROM keywords WHERE user_id=? AND active=1", (uid,)).fetchone()[0]
        top_kws = conn.execute(
            "SELECT keyword, hit_count FROM keywords WHERE user_id=? ORDER BY hit_count DESC LIMIT 5",
            (uid,)
        ).fetchall()
        return {
            "total_tweets":    total,
            "today_tweets":    today,
            "total_keywords":  kw_tot,
            "active_keywords": kw_act,
            "top_keywords":    [dict(r) for r in top_kws],
        }

def get_analytics(uid: str) -> dict:
    with get_conn() as conn:
        per_kw = conn.execute("""
            SELECT keyword, COUNT(*) as count FROM tweets
            WHERE user_id=? GROUP BY keyword ORDER BY count DESC LIMIT 10
        """, (uid,)).fetchall()
        per_day = conn.execute("""
            SELECT DATE(logged_at) as day, COUNT(*) as count FROM tweets
            WHERE user_id=? AND logged_at>=DATE('now','-7 days')
            GROUP BY day ORDER BY day
        """, (uid,)).fetchall()
        top_users = conn.execute("""
            SELECT username, COUNT(*) as count FROM tweets
            WHERE user_id=? GROUP BY username ORDER BY count DESC LIMIT 5
        """, (uid,)).fetchall()
        return {
            "per_keyword": [dict(r) for r in per_kw],
            "per_day":     [dict(r) for r in per_day],
            "top_users":   [dict(r) for r in top_users],
        }

# ══════════════════════════════════════════
#  SPAM — Alert log
# ══════════════════════════════════════════
def get_user_alert_count_last_hour(uid: str, username: str) -> int:
    since = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM user_alert_log WHERE user_id=? AND username=? AND alerted_at>=?",
            (uid, username, since)
        ).fetchone()[0]

def log_user_alert(uid: str, username: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO user_alert_log (user_id, username) VALUES (?,?)", (uid, username)
        )
    cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    with get_conn() as conn:
        conn.execute("DELETE FROM user_alert_log WHERE alerted_at<?", (cutoff,))
