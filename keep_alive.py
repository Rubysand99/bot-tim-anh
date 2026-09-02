import threading
import time

from flask import Flask, jsonify

import db

app = Flask(__name__)
START_TIME = time.time()


@app.route('/')
def home():
    return "Bot Discord đang hoạt động trực tuyến 24/7!"


@app.route('/health')
def health():
    """Health-check chi tiết: uptime + trạng thái kết nối MongoDB."""
    uptime_seconds = int(time.time() - START_TIME)

    try:
        db.get_db().command("ping")
        mongo_status = "ok"
        mongo_ok = True
    except Exception as e:
        mongo_status = f"error: {e}"
        mongo_ok = False

    payload = {
        "status": "ok" if mongo_ok else "degraded",
        "uptime_seconds": uptime_seconds,
        "mongo": mongo_status,
    }
    return jsonify(payload), 200 if mongo_ok else 503


def run():
    # Render tự động cấp cổng thông qua biến môi trường PORT
    import os
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)


def keep_alive():
    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
