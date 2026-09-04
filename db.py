"""
db.py
Kết nối MongoDB dùng chung cho crawl_job.py (crawler) và bot.py (bot).
Dùng chung Atlas cluster với Rudeus Bot, nhưng tách riêng database
"pinterest_bot" để không đụng vào dữ liệu của bot kia.
"""

import os
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient, ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

DB_NAME = "pinterest_bot"
COLLECTION_NAME = "images"
CUSTOM_CATEGORIES_COLLECTION = "custom_categories"
META_COLLECTION = "meta"
PAGINATOR_SESSIONS_COLLECTION = "paginator_sessions"
SHOWCASE_BOARDS_COLLECTION = "showcase_boards"
GUILD_CONFIGS_COLLECTION = "guild_configs"

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


def get_random_image(exclude_urls: list = None, category_keys: list = None):
    """
    Lấy NGẪU NHIÊN 1 ảnh khả dụng bất kỳ CATEGORY NÀO (dùng cho /random —
    random thẳng trên toàn bộ ảnh, không phải random category rồi mới chọn
    ảnh trong đó, tránh thiên vị category ít ảnh).

    category_keys: nếu truyền vào, chỉ random trong các category này (dùng
    để loại category NSFW khi kênh gọi lệnh không được đánh dấu Age-Restricted).

    Trả về document (dict, có field "category") hoặc None nếu không có ảnh khả dụng.
    """
    db = get_db()
    collection = db[COLLECTION_NAME]
    query = _available_query(exclude_urls=exclude_urls)
    if category_keys is not None:
        if not category_keys:
            return None
        query["category"] = {"$in": category_keys}

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

def get_custom_categories() -> dict:
    db = get_db()
    docs = db[CUSTOM_CATEGORIES_COLLECTION].find({})
    return {
        d["_id"]: {"label": d["label"], "keyword": d["keyword"], "nsfw": d.get("nsfw", False)}
        for d in docs
    }


def custom_category_exists(slug: str) -> bool:
    db = get_db()
    return db[CUSTOM_CATEGORIES_COLLECTION].count_documents({"_id": slug}) > 0


def add_custom_category(slug: str, label: str, keyword: str, nsfw: bool = False) -> None:
    db = get_db()
    db[CUSTOM_CATEGORIES_COLLECTION].update_one(
        {"_id": slug},
        {"$set": {"label": label, "keyword": keyword, "nsfw": nsfw}},
        upsert=True,
    )


def edit_custom_category(slug: str, label: str = None, keyword: str = None, nsfw: bool = None) -> bool:
    """Sửa label/keyword/nsfw của 1 category CUSTOM đã tồn tại. Trả về False nếu chưa từng thêm qua lệnh."""
    db = get_db()
    if not custom_category_exists(slug):
        return False
    update = {}
    if label:
        update["label"] = label
    if keyword:
        update["keyword"] = keyword
    if nsfw is not None:
        update["nsfw"] = nsfw
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


# ============================================================
# Bảng giới thiệu chủ đề (showcase board) — admin đăng 1 embed cố định vào
# kênh, ai bấm nút "Bắt đầu" cũng nhận 1 phiên xem ảnh riêng (ephemeral).
# Lưu category_key theo message_id để nút hoạt động vĩnh viễn (giống
# paginator_sessions), không cần nhúng dữ liệu vào custom_id.
# ============================================================

def save_showcase_board(message_id: str, category_key: str) -> None:
    db = get_db()
    db[SHOWCASE_BOARDS_COLLECTION].update_one(
        {"_id": message_id},
        {"$set": {"category_key": category_key, "created_at": now_utc()}},
        upsert=True,
    )


def get_showcase_board(message_id: str):
    db = get_db()
    return db[SHOWCASE_BOARDS_COLLECTION].find_one({"_id": message_id})


# ============================================================
# Cấu hình riêng theo từng server (guild) — cho phép mỗi server tự đặt
# giới hạn kênh/role dùng lệnh ảnh qua /config, thay vì dùng chung 1 cấu
# hình toàn cục (ALLOWED_CHANNEL_IDS/ALLOWED_ROLE_IDS trên Render) cho mọi
# server bot có mặt.
# ============================================================

def get_guild_config(guild_id: int) -> dict:
    """Trả về {"allowed_channel_ids": [...], "allowed_role_ids": [...]}. Rỗng nếu server chưa cấu hình gì."""
    db = get_db()
    doc = db[GUILD_CONFIGS_COLLECTION].find_one({"_id": guild_id})
    if not doc:
        return {"allowed_channel_ids": [], "allowed_role_ids": []}
    return {
        "allowed_channel_ids": doc.get("allowed_channel_ids", []),
        "allowed_role_ids": doc.get("allowed_role_ids", []),
    }


def add_guild_allowed_channel(guild_id: int, channel_id: int) -> None:
    db = get_db()
    db[GUILD_CONFIGS_COLLECTION].update_one(
        {"_id": guild_id}, {"$addToSet": {"allowed_channel_ids": channel_id}}, upsert=True
    )


def remove_guild_allowed_channel(guild_id: int, channel_id: int) -> None:
    db = get_db()
    db[GUILD_CONFIGS_COLLECTION].update_one({"_id": guild_id}, {"$pull": {"allowed_channel_ids": channel_id}})


def clear_guild_allowed_channels(guild_id: int) -> None:
    db = get_db()
    db[GUILD_CONFIGS_COLLECTION].update_one(
        {"_id": guild_id}, {"$set": {"allowed_channel_ids": []}}, upsert=True
    )


def add_guild_allowed_role(guild_id: int, role_id: int) -> None:
    db = get_db()
    db[GUILD_CONFIGS_COLLECTION].update_one(
        {"_id": guild_id}, {"$addToSet": {"allowed_role_ids": role_id}}, upsert=True
    )


def remove_guild_allowed_role(guild_id: int, role_id: int) -> None:
    db = get_db()
    db[GUILD_CONFIGS_COLLECTION].update_one({"_id": guild_id}, {"$pull": {"allowed_role_ids": role_id}})


def clear_guild_allowed_roles(guild_id: int) -> None:
    db = get_db()
    db[GUILD_CONFIGS_COLLECTION].update_one(
        {"_id": guild_id}, {"$set": {"allowed_role_ids": []}}, upsert=True
    )


# ============================================================
# Category kênh (Discord Channel Category / "nhóm kênh") dùng để chứa các
# kênh text được /setup tạo mới — lưu ID lại theo guild để lần sau tái sử
# dụng, không tạo trùng nhiều category kênh mỗi lần chạy /setup.
# ============================================================

def get_guild_setup_category_id(guild_id: int):
    db = get_db()
    doc = db[GUILD_CONFIGS_COLLECTION].find_one({"_id": guild_id})
    return doc.get("setup_category_id") if doc else None


def set_guild_setup_category_id(guild_id: int, category_id: int) -> None:
    db = get_db()
    db[GUILD_CONFIGS_COLLECTION].update_one(
        {"_id": guild_id}, {"$set": {"setup_category_id": category_id}}, upsert=True
    )
