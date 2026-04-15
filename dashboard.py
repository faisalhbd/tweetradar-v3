"""
TweetRadar v3 — Flask Dashboard
Multi-user, cookie-based sessions, setup wizard
"""
import io
import csv
import requests
from functools import wraps
from flask import (Flask, render_template, request, jsonify,
                   redirect, url_for, session, Response, make_response)
import db
import monitor
import config

app = Flask(__name__)
app.secret_key = config.SECRET_KEY


# ══════════════════════════════════════════
#  SESSION HELPERS
# ══════════════════════════════════════════
def get_uid() -> str | None:
    return session.get("uid")

def require_user(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = get_uid()
        if not uid or not db.user_exists(uid):
            return redirect(url_for("setup"))
        db.touch_user(uid)
        return f(*args, **kwargs)
    return decorated

def require_configured(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = get_uid()
        if not uid or not db.user_exists(uid):
            return redirect(url_for("setup"))
        if not db.user_is_configured(uid):
            return redirect(url_for("setup"))
        db.touch_user(uid)
        return f(*args, **kwargs)
    return decorated


# ══════════════════════════════════════════
#  SETUP WIZARD
# ══════════════════════════════════════════
@app.route("/setup", methods=["GET", "POST"])
def setup():
    uid = get_uid()

    # Existing user just updating keys
    existing_user = uid and db.user_exists(uid)

    if request.method == "POST":
        twitter_key = request.form.get("twitter_key", "").strip()
        tg_token    = request.form.get("tg_token", "").strip()
        tg_chat_id  = request.form.get("tg_chat_id", "").strip()
        time_filter = request.form.get("time_filter", "1h")

        if not twitter_key or not tg_token or not tg_chat_id:
            return render_template("setup.html",
                error="All fields are required.",
                existing=existing_user,
                prefill=request.form)

        # Create user if new
        if not existing_user:
            if db.count_users() >= config.MAX_USERS:
                return render_template("setup.html",
                    error=f"Maximum {config.MAX_USERS} users reached. Contact admin.",
                    existing=False, prefill={})
            uid = db.create_user()
            session["uid"] = uid

        db.update_user_keys(uid, twitter_key, tg_token, tg_chat_id)
        db.set_setting(uid, "time_filter", time_filter)

        # Start/restart monitor
        monitor.stop_monitor(uid)
        import time; time.sleep(0.5)
        monitor.start_monitor(uid)

        return redirect(url_for("index"))

    # GET — prefill if existing user
    prefill = {}
    if existing_user:
        u = db.get_user(uid)
        prefill = {
            "twitter_key": u.get("twitter_key", ""),
            "tg_token":    u.get("tg_token", ""),
            "tg_chat_id":  u.get("tg_chat_id", ""),
            "time_filter": db.get_setting(uid, "time_filter", "1h"),
        }

    return render_template("setup.html", error=None,
                           existing=existing_user, prefill=prefill)


# ══════════════════════════════════════════
#  MAIN DASHBOARD
# ══════════════════════════════════════════
@app.route("/")
@require_configured
def index():
    return render_template("index.html")


# ══════════════════════════════════════════
#  API — TEST CONNECTIONS
# ══════════════════════════════════════════
@app.route("/api/test/twitter", methods=["POST"])
@require_user
def test_twitter():
    key = (request.json or {}).get("key", "").strip()
    if not key:
        return jsonify({"ok": False, "error": "No key provided"})
    try:
        r = requests.get(
            "https://api.twitterapi.io/twitter/tweet/advanced_search",
            headers={"X-API-Key": key},
            params={"query": "test lang:en", "queryType": "Latest", "count": 1},
            timeout=10
        )
        if r.status_code == 200:
            return jsonify({"ok": True, "msg": "✅ Twitter API key works!"})
        elif r.status_code == 401:
            return jsonify({"ok": False, "error": "❌ Invalid API key"})
        else:
            return jsonify({"ok": False, "error": f"HTTP {r.status_code}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/test/telegram", methods=["POST"])
@require_user
def test_telegram():
    data     = request.json or {}
    token    = data.get("token", "").strip()
    chat_id  = data.get("chat_id", "").strip()
    if not token or not chat_id:
        return jsonify({"ok": False, "error": "Token and Chat ID required"})
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id,
                  "text": "✅ TweetRadar connected! Your Telegram alerts are working."},
            timeout=10
        )
        d = r.json()
        if d.get("ok"):
            return jsonify({"ok": True, "msg": "✅ Telegram test message sent!"})
        else:
            return jsonify({"ok": False, "error": d.get("description", "Unknown error")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ══════════════════════════════════════════
#  API — STATS & ANALYTICS
# ══════════════════════════════════════════
@app.route("/api/stats")
@require_configured
def api_stats():
    uid = get_uid()
    stats = db.get_stats(uid)
    stats["monitor_running"] = monitor.is_running(uid)
    stats["monitor_active"]  = db.get_setting(uid, "monitor_active", "1")
    return jsonify(stats)

@app.route("/api/analytics")
@require_configured
def api_analytics():
    return jsonify(db.get_analytics(get_uid()))


# ══════════════════════════════════════════
#  API — SETTINGS
# ══════════════════════════════════════════
@app.route("/api/settings", methods=["GET"])
@require_configured
def api_get_settings():
    uid = get_uid()
    s   = db.get_all_settings(uid)
    u   = db.get_user(uid)
    # Mask keys for display
    s["twitter_key_masked"] = ("●" * 8 + u["twitter_key"][-4:]) if u.get("twitter_key") else ""
    s["tg_token_masked"]    = ("●" * 8 + u["tg_token"][-4:]) if u.get("tg_token") else ""
    s["tg_chat_id"]         = u.get("tg_chat_id", "")
    return jsonify(s)

@app.route("/api/settings", methods=["POST"])
@require_configured
def api_save_settings():
    db.save_settings_bulk(get_uid(), request.json or {})
    return jsonify({"ok": True})

@app.route("/api/monitor/toggle", methods=["POST"])
@require_configured
def api_toggle_monitor():
    uid     = get_uid()
    current = db.get_setting(uid, "monitor_active", "1")
    new_val = "0" if current == "1" else "1"
    db.set_setting(uid, "monitor_active", new_val)
    if new_val == "1":
        monitor.start_monitor(uid)
    return jsonify({"active": new_val == "1", "running": monitor.is_running(uid)})


# ══════════════════════════════════════════
#  API — KEYWORDS
# ══════════════════════════════════════════
@app.route("/api/keywords", methods=["GET"])
@require_configured
def api_get_keywords():
    return jsonify(db.get_keywords(get_uid()))

@app.route("/api/keywords", methods=["POST"])
@require_configured
def api_add_keyword():
    kw = (request.json or {}).get("keyword", "").strip()
    if not kw:
        return jsonify({"ok": False, "error": "Empty"}), 400
    ok = db.add_keyword(get_uid(), kw)
    return jsonify({"ok": ok, "error": None if ok else "Duplicate keyword"})

@app.route("/api/keywords/<int:kid>", methods=["DELETE"])
@require_configured
def api_delete_keyword(kid):
    db.delete_keyword(get_uid(), kid)
    return jsonify({"ok": True})

@app.route("/api/keywords/<int:kid>/toggle/<field>", methods=["POST"])
@require_configured
def api_toggle_keyword(kid, field):
    db.toggle_keyword(get_uid(), kid, field)
    return jsonify({"ok": True})

@app.route("/api/keywords/import", methods=["POST"])
@require_configured
def api_import_keywords():
    uid      = get_uid()
    keywords = []
    if "file" in request.files:
        content = request.files["file"].read().decode("utf-8", errors="ignore")
        for line in content.splitlines():
            kw = line.split(",")[0].strip()
            if kw and not kw.startswith("#"):
                keywords.append(kw)
    elif request.json and "keywords" in request.json:
        for line in request.json["keywords"].splitlines():
            kw = line.strip()
            if kw and not kw.startswith("#"):
                keywords.append(kw)
    added, skipped = db.bulk_import_keywords(uid, keywords)
    return jsonify({"ok": True, "added": added, "skipped": skipped})


# ══════════════════════════════════════════
#  API — TWEETS
# ══════════════════════════════════════════
@app.route("/api/tweets")
@require_configured
def api_tweets():
    uid     = get_uid()
    keyword = request.args.get("keyword")
    hours   = request.args.get("hours", type=int)
    limit   = request.args.get("limit", 50, type=int)
    return jsonify(db.get_recent_tweets(uid, limit=limit, keyword=keyword, hours=hours))

@app.route("/api/export/csv")
@require_configured
def export_csv():
    tweets = db.get_recent_tweets(get_uid(), limit=5000)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "tweet_id","username","text","tweet_url","keyword",
        "created_at","likes","retweets","followers","logged_at"
    ])
    writer.writeheader()
    for t in tweets:
        writer.writerow({k: t.get(k, "") for k in writer.fieldnames})
    return Response(output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=tweets_export.csv"})


# ══════════════════════════════════════════
#  BOOT
# ══════════════════════════════════════════
if __name__ == "__main__":
    db.init_db()
    monitor.start_all_configured_users()
    app.run(host="0.0.0.0", port=config.DASHBOARD_PORT, debug=False)
