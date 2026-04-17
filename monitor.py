"""
Monitor Engine — per-user background threads
Each user gets their own thread using their own API keys
"""
import time
import logging
import threading
import requests
from datetime import datetime
from difflib import SequenceMatcher
import db
import config

log = logging.getLogger("monitor")

# Active monitor threads: {user_id: threading.Thread}
_threads: dict[str, threading.Thread] = {}
_stop_flags: dict[str, threading.Event] = {}

# Per-user, per-keyword last seen tweet ID (in-memory, resets on restart)
# Structure: {uid: {keyword: since_id}}
_since_ids: dict[str, dict[str, str]] = {}


# ══════════════════════════════════════════
#  SPAM FILTER
# ══════════════════════════════════════════
def is_spam(uid: str, tweet: dict, keyword: str) -> tuple[bool, str]:
    s         = db.get_all_settings(uid)
    text      = tweet.get("text", "") or ""
    author    = tweet.get("author", {}) or tweet.get("user", {})
    username  = author.get("userName") or author.get("screen_name", "")
    followers = author.get("followers") or author.get("followers_count", 0) or 0
    likes     = tweet.get("likeCount") or tweet.get("favorite_count", 0) or 0
    rts       = tweet.get("retweetCount") or tweet.get("retweet_count", 0) or 0

    # Retweet detection — text prefix + API boolean/object fields
    is_rt = (
        text.strip().startswith("RT @")
        or tweet.get("isRetweet") is True
        or tweet.get("retweeted_status") is not None
        or tweet.get("retweetedTweet") is not None
    )
    if s.get("skip_retweets") == "1" and is_rt:
        return True, "retweet"

    # Quote tweet detection — original tweet ache but re-shared
    is_quote = (
        tweet.get("isQuote") is True
        or tweet.get("quoted_status") is not None
        or tweet.get("quotedTweet") is not None
    )
    if s.get("skip_retweets") == "1" and is_quote:
        return True, "quote_tweet"

    if s.get("skip_replies") == "1" and text.strip().startswith("@"):
        return True, "reply"
    if followers < int(s.get("min_followers", 10)):
        return True, f"low_followers({followers})"
    if likes < int(s.get("min_likes", 0)):
        return True, f"low_likes({likes})"
    if rts < int(s.get("min_retweets", 0)):
        return True, f"low_retweets({rts})"

    max_per_user = int(s.get("max_alerts_per_user", 3))
    if db.get_user_alert_count_last_hour(uid, username) >= max_per_user:
        return True, f"rate_limit({username})"

    # Near-duplicate text check
    recent = db.get_recent_tweets(uid, limit=20, keyword=keyword, hours=1)
    for prev in recent:
        ratio = SequenceMatcher(None, text[:200], (prev["text"] or "")[:200]).ratio()
        if ratio >= 0.80:
            return True, f"duplicate({ratio:.0%})"

    return False, ""


# ══════════════════════════════════════════
#  TWITTER SEARCH
# ══════════════════════════════════════════
def build_query(keyword: str, settings: dict) -> str:
    query = keyword

    # Filter out retweets and quote tweets at the API query level
    if settings.get("skip_retweets") == "1":
        query += " -is:retweet -is:quote"

    # Always English — catches USA tweets without place_country filter
    # (place_country:US drops ~80% of tweets since most users don't set location)
    query += " lang:en"

    tf = settings.get("time_filter", "realtime")
    if tf == "realtime":
        # Realtime = last 10 minutes only — ensures fresh tweets
        query += " within_time:10m"
    else:
        query += f" within_time:{tf}"

    return query


def search_tweets(twitter_key: str, keyword: str, settings: dict, since_id: str = None) -> list:
    """
    Fetch tweets for a keyword.
    since_id: only return tweets newer than this ID → avoids re-fetching old tweets → saves credits.
    """
    url     = "https://api.twitterapi.io/twitter/tweet/advanced_search"
    headers = {"X-API-Key": twitter_key}
    params  = {
        "query":     build_query(keyword, settings),
        "queryType": "Latest",
        "count":     int(settings.get("max_results", config.MAX_RESULTS_PER_KW)),
    }
    if since_id:
        params["sinceId"] = since_id
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data.get("tweets", data.get("data", []))
    except requests.HTTPError as e:
        log.error(f"HTTP {r.status_code} '{keyword}': {r.text[:120]}")
    except Exception as e:
        log.error(f"Search error '{keyword}': {e}")
    return []


# ══════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════
def escape_md(text: str) -> str:
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def send_telegram(tg_token: str, tg_chat_id: str, data: dict) -> bool:
    text = data["text"] or ""
    short = text[:config.TWEET_TEXT_LIMIT] + ("..." if len(text) > config.TWEET_TEXT_LIMIT else "")
    msg = (
        f"🔔 *New Tweet Found\\!*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 @{escape_md(data['username'])}\n"
        f"🔑 Keyword: `{escape_md(data['keyword'])}`\n"
        f"📝 {escape_md(short)}\n\n"
        f"❤️ {data.get('likes',0)}  🔁 {data.get('retweets',0)}  👥 {data.get('followers',0)}\n"
        f"🔗 [View Tweet]({data['tweet_url']})\n"
        f"⏰ {escape_md(data['created_at'][:16] if data['created_at'] else '')}"
    )
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            json={"chat_id": tg_chat_id, "text": msg,
                  "parse_mode": "MarkdownV2", "disable_web_page_preview": False},
            timeout=10
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram error uid={data.get('_uid','?')}: {e}")
        return False


def send_startup_msg(tg_token: str, tg_chat_id: str, kw_count: int):
    msg = (
        f"🚀 *TweetRadar Started\\!*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 Keywords: *{kw_count}* active\n"
        f"⏱ Poll: *Every 5 min* \\(15min 00\\-06 UTC\\)\n"
        f"🔍 Mode: *Realtime* \\(last 10 min tweets only\\)\n"
        f"✅ Monitoring is ON\\!"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            json={"chat_id": tg_chat_id, "text": msg, "parse_mode": "MarkdownV2"},
            timeout=10
        )
    except Exception:
        pass


# ══════════════════════════════════════════
#  EXTRACT
# ══════════════════════════════════════════
def extract(tweet: dict, keyword: str, uid: str) -> dict:
    author   = tweet.get("author", {}) or tweet.get("user", {})
    username = author.get("userName") or author.get("screen_name", "unknown")
    tweet_id = str(tweet.get("id") or tweet.get("id_str", ""))
    text     = tweet.get("text") or tweet.get("full_text", "")
    created  = tweet.get("createdAt") or tweet.get("created_at", "")
    return {
        "_uid":      uid,
        "tweet_id":  tweet_id,
        "username":  username,
        "text":      text,
        "tweet_url": f"https://twitter.com/{username}/status/{tweet_id}",
        "keyword":   keyword,
        "created_at": created[:19] if created else "",
        "likes":     tweet.get("likeCount") or tweet.get("favorite_count", 0) or 0,
        "retweets":  tweet.get("retweetCount") or tweet.get("retweet_count", 0) or 0,
        "followers": author.get("followers") or author.get("followers_count", 0) or 0,
    }


# ══════════════════════════════════════════
#  PER-USER MONITOR LOOP
# ══════════════════════════════════════════
def _get_interval(settings: dict) -> int:
    base  = int(settings.get("poll_interval", config.POLL_INTERVAL_SEC))
    hour  = datetime.utcnow().hour
    if 0 <= hour < 6:
        return max(base, config.NIGHT_POLL_INTERVAL_SEC)
    return base


def _user_loop(uid: str, stop_event: threading.Event):
    log.info(f"[{uid[:8]}] Monitor thread started")
    user = db.get_user(uid)
    if not user:
        log.error(f"[{uid[:8]}] User not found — stopping")
        return

    kws = db.get_keywords(uid, active_only=True)
    try:
        send_startup_msg(user["tg_token"], user["tg_chat_id"], len(kws))
    except Exception:
        pass

    # Init since_id store for this user
    if uid not in _since_ids:
        _since_ids[uid] = {}

    while not stop_event.is_set():
        cycle_start = time.time()

        # Reload user & settings each cycle (may have changed via dashboard)
        user     = db.get_user(uid)
        settings = db.get_all_settings(uid)

        if not user or settings.get("monitor_active") == "0":
            log.info(f"[{uid[:8]}] Paused — sleeping 60s")
            stop_event.wait(60)
            continue

        twitter_key = user.get("twitter_key", "")
        tg_token    = user.get("tg_token", "")
        tg_chat_id  = user.get("tg_chat_id", "")

        if not twitter_key or not tg_token or not tg_chat_id:
            log.warning(f"[{uid[:8]}] Missing keys — sleeping 60s")
            stop_event.wait(60)
            continue

        keywords  = db.get_keywords(uid, active_only=True)
        new_count = 0

        for kw_row in keywords:
            if stop_event.is_set():
                break
            keyword  = kw_row["keyword"]
            since_id = _since_ids[uid].get(keyword)  # last seen tweet ID

            tweets = search_tweets(twitter_key, keyword, settings, since_id=since_id)

            # Track highest tweet ID seen this cycle to use as since_id next cycle
            max_id_this_cycle = since_id

            for tweet in tweets:
                tweet_id = str(tweet.get("id") or tweet.get("id_str", ""))
                if not tweet_id:
                    continue

                # Update max_id — integer comparison (string compare breaks on "9" > "10")
                if not max_id_this_cycle or int(tweet_id) > int(max_id_this_cycle):
                    max_id_this_cycle = tweet_id

                # Skip already processed tweets (belt-and-suspenders with since_id)
                if db.tweet_exists(uid, tweet_id):
                    continue

                spammy, reason = is_spam(uid, tweet, keyword)
                data = extract(tweet, keyword, uid)
                db.save_tweet(uid, data)

                if spammy:
                    log.debug(f"[{uid[:8]}] spam[{reason}] @{data['username']}")
                    continue

                db.increment_hit(uid, keyword)
                if kw_row["alert_on"]:
                    if send_telegram(tg_token, tg_chat_id, data):
                        db.log_user_alert(uid, data["username"])
                        new_count += 1
                        stop_event.wait(0.5)

            # Save the latest tweet ID for next cycle — avoids re-fetching old tweets
            if max_id_this_cycle:
                _since_ids[uid][keyword] = max_id_this_cycle
                log.debug(f"[{uid[:8]}] '{keyword}' since_id updated → {max_id_this_cycle}")

            stop_event.wait(1.2)

        elapsed  = time.time() - cycle_start
        interval = _get_interval(settings)
        sleep    = max(0, interval - elapsed)
        log.info(f"[{uid[:8]}] cycle done — {new_count} alerts | sleep {sleep:.0f}s")
        stop_event.wait(sleep)

    log.info(f"[{uid[:8]}] Monitor thread stopped")


# ══════════════════════════════════════════
#  PUBLIC API — start / stop / restart
# ══════════════════════════════════════════
def start_monitor(uid: str):
    """Start background monitor thread for a user (idempotent)"""
    if uid in _threads and _threads[uid].is_alive():
        return  # already running

    stop_event = threading.Event()
    t = threading.Thread(
        target=_user_loop,
        args=(uid, stop_event),
        daemon=True,
        name=f"monitor-{uid[:8]}"
    )
    _stop_flags[uid] = stop_event
    _threads[uid]    = t
    t.start()
    log.info(f"[{uid[:8]}] Monitor started")


def stop_monitor(uid: str):
    if uid in _stop_flags:
        _stop_flags[uid].set()


def is_running(uid: str) -> bool:
    return uid in _threads and _threads[uid].is_alive()


def start_all_configured_users():
    """Called on app boot — restart monitors for all configured users"""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM users WHERE twitter_key IS NOT NULL AND tg_token IS NOT NULL"
        ).fetchall()
    for row in rows:
        uid      = row[0]
        settings = db.get_all_settings(uid)
        if settings.get("monitor_active") == "1":
            start_monitor(uid)
            log.info(f"[{uid[:8]}] Auto-started on boot")
