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
        _client = MongoClient(uri, tz_aware=True, tzinfo=timezone.utc)
        _db = _client[DB_NAME]
        collection = _db[COLLECTION_NAME]
        # unique index: tránh crawler lưu trùng cùng 1 ảnh nhiều lần
        collection.create_index([("image_url", ASCENDING)], unique=True)
        # index hỗ trợ query theo category + trạng thái đã gửi
        collection.create_index([("category", ASCENDING), ("last_sent_at", ASCENDING)])
    return _db


COOLDOWN_HOURS = 1


def _available_query(category: str = None, exclude_urls: list = None) -> dict:
    cooldown_before = now_utc() - timedelta(hours=COOLDOWN_HOURS)
    query = {
        "$or": [
            {"last_sent_at": None},
            {"last_sent_at": {"$lt": cooldown_before}},
        ],
    }
    if category is not None:
        query["category"] = category
    if exclude_urls:
        query["image_url"] = {"$nin": exclude_urls}
    return query


def _mark_sent(doc_id) -> None:
    db = get_db()
    db[COLLECTION_NAME].update_one(
        {"_id": doc_id},
        {"$set": {"last_sent_at": now_utc()}, "$inc": {"sent_count": 1}},
    )


def get_next_image(category: str, exclude_urls: list):
    """
    Lấy NGẪU NHIÊN 1 ảnh khả dụng trong category (chưa từng gửi hoặc đã gửi
    > 1 tiếng trước) — không theo thứ tự cố định. Đánh dấu last_sent_at = now
    ngay khi lấy để tránh 2 user cùng nhận trùng 1 ảnh trong cùng cooldown.

    Trả về document (dict) hoặc None nếu không còn ảnh khả dụng.
    """
    db = get_db()
    collection = db[COLLECTION_NAME]
    query = _available_query(category=category, exclude_urls=exclude_urls)

    results = list(collection.aggregate([
        {"$match": query},
        {"$sample": {"size": 1}},
    ]))
    if not results:
        return None

    doc = results[0]
    _mark_sent(doc["_id"])
    doc["last_sent_at"] = now_utc()
    doc["sent_count"] = doc.get("sent_count", 0) + 1
    return doc


def get_random_image(exclude_urls: list = None):
    """
    Lấy NGẪU NHIÊN 1 ảnh khả dụng bất kỳ CATEGORY NÀO (dùng cho /random —
    random thẳng trên toàn bộ ảnh, không phải random category rồi mới chọn
    ảnh trong đó, tránh thiên vị category ít ảnh).

    Trả về document (dict, có field "category") hoặc None nếu DB trống ảnh khả dụng.
    """
    db = get_db()
    collection = db[COLLECTION_NAME]
    query = _available_query(exclude_urls=exclude_urls)

    results = list(collection.aggregate([
        {"$match": query},
        {"$sample": {"size": 1}},
    ]))
    if not results:
        return None

    doc = results[0]
    _mark_sent(doc["_id"])
    doc["last_sent_at"] = now_utc()
    doc["sent_count"] = doc.get("sent_count", 0) + 1
    return doc


def count_images(category: str) -> int:
    db = get_db()
    return db[COLLECTION_NAME].count_documents({"category": category})


def count_available_images(category: str) -> int:
    """Đếm ảnh KHẢ DỤNG ngay bây giờ (chưa gửi hoặc đã qua cooldown 1 tiếng)."""
    db = get_db()
    return db[COLLECTION_NAME].count_documents(_available_query(category=category))


def get_category_stats(category: str) -> dict:
    """Thống kê chi tiết 1 category: tổng, khả dụng, TB số lần gửi, ảnh cũ/mới nhất."""
    db = get_db()
    collection = db[COLLECTION_NAME]

    total = collection.count_documents({"category": category})
    available = count_available_images(category)

    agg = list(collection.aggregate([
        {"$match": {"category": category}},
        {"$group": {
            "_id": None,
            "avg_sent_count": {"$avg": "$sent_count"},
            "max_sent_count": {"$max": "$sent_count"},
            "oldest_created_at": {"$min": "$created_at"},
            "newest_created_at": {"$max": "$created_at"},
        }},
    ]))

    if agg:
        a = agg[0]
        avg_sent_count = round(a.get("avg_sent_count") or 0, 1)
        max_sent_count = a.get("max_sent_count") or 0
        oldest_created_at = a.get("oldest_created_at")
        newest_created_at = a.get("newest_created_at")
    else:
        avg_sent_count = 0
        max_sent_count = 0
        oldest_created_at = None
        newest_created_at = None

    return {
        "total": total,
        "available": available,
        "avg_sent_count": avg_sent_count,
        "max_sent_count": max_sent_count,
        "oldest_created_at": oldest_created_at,
        "newest_created_at": newest_created_at,
    }


# ============================================================
# Category tuỳ chỉnh — cho phép admin thêm/sửa/xoá chủ đề qua lệnh Discord
# mà không cần sửa categories.py + deploy lại. Lưu riêng 1 collection,
# merge với CATEGORIES tĩnh ở tầng bot.py.
# ============================================================

CUSTOM_CATEGORIES_COLLECTION = "custom_categories"


def get_custom_categories() -> dict:
    db = get_db()
    docs = db[CUSTOM_CATEGORIES_COLLECTION].find({})
    return {d["_id"]: {"label": d["label"], "keyword": d["keyword"]} for d in docs}


def custom_category_exists(slug: str) -> bool:
    db = get_db()
    return db[CUSTOM_CATEGORIES_COLLECTION].count_documents({"_id": slug}) > 0


def add_custom_category(slug: str, label: str, keyword: str) -> None:
    db = get_db()
    db[CUSTOM_CATEGORIES_COLLECTION].update_one(
        {"_id": slug},
        {"$set": {"label": label, "keyword": keyword}},
        upsert=True,
    )


def edit_custom_category(slug: str, label: str = None, keyword: str = None) -> bool:
    """Sửa label/keyword của 1 category CUSTOM đã tồn tại. Trả về False nếu chưa từng thêm qua lệnh."""
    db = get_db()
    if not custom_category_exists(slug):
        return False
    update = {}
    if label:
        update["label"] = label
    if keyword:
        update["keyword"] = keyword
    if not update:
        return True
    db[CUSTOM_CATEGORIES_COLLECTION].update_one({"_id": slug}, {"$set": update})
    return True


def remove_custom_category(slug: str) -> bool:
    """Trả về True nếu xoá được (category tồn tại và là custom)."""
    db = get_db()
    result = db[CUSTOM_CATEGORIES_COLLECTION].delete_one({"_id": slug})
    return result.deleted_count > 0


# ============================================================
# Metadata job crawl (dùng để biết lần crawl gần nhất là khi nào — phục vụ
# việc tự crawl ngay category mới thêm nếu đã lâu chưa có lần crawl nào).
# ============================================================

META_COLLECTION = "meta"


def get_last_crawl_time():
    db = get_db()
    doc = db[META_COLLECTION].find_one({"_id": "last_crawl"})
    return doc["at"] if doc else None


def set_last_crawl_time() -> None:
    db = get_db()
    db[META_COLLECTION].update_one(
        {"_id": "last_crawl"},
        {"$set": {"at": now_utc()}},
        upsert=True,
    )


# ============================================================
# Phiên xem ảnh (paginator session) — lưu trạng thái nút Trước/Sau theo
# message_id để nút bấm hoạt động vĩnh viễn, kể cả sau khi bot restart
# (không phụ thuộc bộ nhớ RAM của tiến trình bot).
# ============================================================

PAGINATOR_SESSIONS_COLLECTION = "paginator_sessions"


def save_paginator_session(message_id: str, category_key: str, label: str, keyword: str,
                            images: list, index: int, author_id: int) -> None:
    db = get_db()
    db[PAGINATOR_SESSIONS_COLLECTION].update_one(
        {"_id": message_id},
        {"$set": {
            "category_key": category_key,
            "label": label,
            "keyword": keyword,
            "images": images,
            "index": index,
            "author_id": author_id,
            "updated_at": now_utc(),
        }},
        upsert=True,
    )


def get_paginator_session(message_id: str):
    db = get_db()
    return db[PAGINATOR_SESSIONS_COLLECTION].find_one({"_id": message_id})


def update_paginator_session(message_id: str, images: list, index: int) -> None:
    db = get_db()
    db[PAGINATOR_SESSIONS_COLLECTION].update_one(
        {"_id": message_id},
        {"$set": {"images": images, "index": index, "updated_at": now_utc()}},
    )


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
