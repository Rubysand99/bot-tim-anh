"""
db.py
Kết nối MongoDB dùng chung cho crawl_job.py (crawler) và bot.py (bot).
Dùng chung Atlas cluster với Rudeus Bot, nhưng tách riêng database
"pinterest_bot" để không đụng vào dữ liệu của bot kia.
"""

import os
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient, ASCENDING, ReturnDocument

DB_NAME = "pinterest_bot"
COLLECTION_NAME = "images"

_client = None
_db = None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def get_db():
    """Lazy-connect, chỉ tạo connection 1 lần rồi tái sử dụng."""
    global _client, _db
    if _db is None:
        uri = os.getenv("MONGO_URI")
        if not uri:
            raise RuntimeError("Chưa thiết lập biến môi trường MONGO_URI")
        _client = MongoClient(uri)
        _db = _client[DB_NAME]
        collection = _db[COLLECTION_NAME]
        # unique index: tránh crawler lưu trùng cùng 1 ảnh nhiều lần
        collection.create_index([("image_url", ASCENDING)], unique=True)
        # index hỗ trợ query theo category + trạng thái đã gửi
        collection.create_index([("category", ASCENDING), ("last_sent_at", ASCENDING)])
    return _db


def get_next_image(category: str, exclude_urls: list):
    """
    Lấy 1 ảnh khả dụng trong category, ưu tiên ảnh chưa từng gửi,
    sau đó tới ảnh đã gửi > 1 tiếng trước. Đánh dấu last_sent_at = now
    ngay khi lấy (atomic) để tránh 2 user cùng nhận trùng 1 ảnh.

    Trả về document (dict) hoặc None nếu không còn ảnh khả dụng.
    """
    db = get_db()
    collection = db[COLLECTION_NAME]
    cooldown_before = now_utc() - timedelta(hours=1)

    query = {
        "category": category,
        "$or": [
            {"last_sent_at": None},
            {"last_sent_at": {"$lt": cooldown_before}},
        ],
    }
    if exclude_urls:
        query["image_url"] = {"$nin": exclude_urls}

    doc = collection.find_one_and_update(
        query,
        {"$set": {"last_sent_at": now_utc()}, "$inc": {"sent_count": 1}},
        sort=[("last_sent_at", ASCENDING)],
        return_document=ReturnDocument.AFTER,
    )
    return doc


def count_images(category: str) -> int:
    db = get_db()
    return db[COLLECTION_NAME].count_documents({"category": category})
