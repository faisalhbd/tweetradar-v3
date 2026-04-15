"""Shared configuration — TweetRadar v3"""
import os
from pathlib import Path

# ─── Paths ───
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH  = DATA_DIR / "tweetradar.db"

# ─── App ───
MAX_USERS            = 5
DASHBOARD_PORT       = int(os.getenv("PORT", 8080))
SECRET_KEY           = os.getenv("SECRET_KEY", "tweetradar-secret-change-me-xyz")
SESSION_COOKIE_DAYS  = 30

# ─── Monitor defaults ───
POLL_INTERVAL_SEC       = 15 * 60   # 15 min (was 5 min — reduced to save credits)
NIGHT_POLL_INTERVAL_SEC = 60 * 60   # 60 min (00-06 UTC)
MAX_RESULTS_PER_KW      = 5         # was 10 — fewer results = fewer credits
TWEET_TEXT_LIMIT        = 200

# ─── Spam filter defaults ───
DEFAULT_SETTINGS = {
    "monitor_active":      "1",
    "poll_interval":       "900",      # 15 min (was 300 = 5 min)
    "time_filter":         "realtime", # realtime = no within_time filter (cheaper)
    "location_filter":     "USA",
    "min_followers":       "10",
    "skip_retweets":       "1",
    "skip_replies":        "1",
    "max_alerts_per_user": "3",
    "min_likes":           "0",
    "min_retweets":        "0",
}
