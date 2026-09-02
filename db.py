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


def count_available_images(category: str) -> int:
    """Đếm ảnh KHẢ DỤNG ngay bây giờ (chưa gửi hoặc đã qua cooldown 1 tiếng)."""
    db = get_db()
    cooldown_before = now_utc() - timedelta(hours=1)
    return db[COLLECTION_NAME].count_documents({
        "category": category,
        "$or": [
            {"last_sent_at": None},
            {"last_sent_at": {"$lt": cooldown_before}},
        ],
    })


# ============================================================
# Category tuỳ chỉnh — cho phép admin thêm/xoá chủ đề qua lệnh Discord
# mà không cần sửa categories.py + deploy lại. Lưu riêng 1 collection,
# merge với CATEGORIES tĩnh ở tầng bot.py.
# ============================================================

CUSTOM_CATEGORIES_COLLECTION = "custom_categories"


def get_custom_categories() -> dict:
    db = get_db()
    docs = db[CUSTOM_CATEGORIES_COLLECTION].find({})
    return {d["_id"]: {"label": d["label"], "keyword": d["keyword"]} for d in docs}


def add_custom_category(slug: str, label: str, keyword: str) -> None:
    db = get_db()
    db[CUSTOM_CATEGORIES_COLLECTION].update_one(
        {"_id": slug},
        {"$set": {"label": label, "keyword": keyword}},
        upsert=True,
    )


def remove_custom_category(slug: str) -> bool:
    """Trả về True nếu xoá được (category tồn tại và là custom)."""
    db = get_db()
    result = db[CUSTOM_CATEGORIES_COLLECTION].delete_one({"_id": slug})
    return result.deleted_count > 0


# ============================================================
# Dọn dẹp kho ảnh
# ============================================================

def delete_overused_images(category: str, min_sent_count: int) -> int:
    """Xoá ảnh đã bị gửi >= min_sent_count lần trong category. Trả về số đã xoá."""
    db = get_db()
    result = db[COLLECTION_NAME].delete_many({
        "category": category,
        "sent_count": {"$gte": min_sent_count},
    })
    return result.deleted_count


def get_all_image_urls(category: str) -> list:
    db = get_db()
    return [d["image_url"] for d in db[COLLECTION_NAME].find({"category": category}, {"image_url": 1})]


def delete_images_by_url(urls: list) -> int:
    if not urls:
        return 0
    db = get_db()
    result = db[COLLECTION_NAME].delete_many({"image_url": {"$in": urls}})
    return result.deleted_count
