"""
db.py
Kết nối MongoDB dùng chung cho crawl_job.py (crawler) và bot.py (bot).
Dùng chung Atlas cluster với Rudeus Bot, nhưng tách riêng database
"pinterest_bot" để không đụng vào dữ liệu của bot kia.
"""

import functools
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient, ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger("db")

# Ngưỡng (ms) để coi 1 lệnh Mongo là "chậm" và log cảnh báo — giúp chẩn đoán
# khi nghi ngờ MongoDB Atlas (hoặc độ trễ mạng Render<->Atlas) là nguyên nhân
# timeout/lag ở bot, thay vì đoán mò. Chỉnh số này nếu thấy log ồn quá hoặc
# muốn bắt cả những lệnh chỉ hơi chậm.
SLOW_QUERY_THRESHOLD_MS = 500


def _timed(func):
    """Đo thời gian chạy 1 hàm DB, log WARNING nếu vượt ngưỡng SLOW_QUERY_THRESHOLD_MS."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.monotonic()
        try:
            return func(*args, **kwargs)
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            if elapsed_ms >= SLOW_QUERY_THRESHOLD_MS:
                logger.warning(f"[Mongo chậm] {func.__name__} mất {elapsed_ms:.0f}ms")
    return wrapper

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


@_timed
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


@_timed
def _mark_sent(doc_id) -> None:
    db = get_db()
    db[COLLECTION_NAME].update_one(
        {"_id": doc_id},
        {"$set": {"last_sent_at": now_utc()}, "$inc": {"sent_count": 1}},
    )


# Discord giới hạn URL trong embeds.image.url tối đa 2048 ký tự — từng gặp
# thực tế 1 URL từ Pinterest vượt giới hạn này, khiến Discord từ chối cả
# embed với lỗi 400 "Invalid Form Body" (phát hiện qua self-test soak trong
# bot.py). Dùng chung ở crawl_job.py (chặn từ gốc, không cho lưu vào DB) và
# bot.py (tự chữa lành nếu lỡ có URL xấu nào đã nằm sẵn trong DB từ trước).
MAX_IMAGE_URL_LENGTH = 2048


def is_valid_image_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    if len(url) > MAX_IMAGE_URL_LENGTH:
        return False
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
    if any(c.isspace() for c in url):
        return False
    return True


@_timed
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


@_timed
def peek_random_image(category: str):
    """
    Giống get_next_image() nhưng KHÔNG đánh dấu last_sent_at/sent_count —
    dùng riêng cho self-test/soak-test (bot tự đọc DB định kỳ mỗi 10s để đo
    hiệu năng MongoDB, xem self_test_loop trong bot.py). Nếu dùng get_next_image
    cho việc này, tự động gọi liên tục sẽ "dùng hết" ảnh thật của user
    (đánh dấu last_sent_at khiến ảnh bị coi là vừa gửi, user thật dễ bị báo
    "hết ảnh" oan). Vẫn cùng 1 kiểu truy vấn $sample -> đo đúng chi phí đọc
    thật sự của MongoDB, chỉ khác không có phần ghi (update) đi kèm.

    Trả về document (dict) hoặc None nếu category không có ảnh nào (kể cả
    ảnh đang trong cooldown cũng không tính, vì mục đích chỉ để đo tốc độ
    đọc, không cần đúng logic "khả dụng" như get_next_image).
    """
    db = get_db()
    collection = db[COLLECTION_NAME]
    results = list(collection.aggregate([
        {"$match": {"category": category}},
        {"$sample": {"size": 1}},
    ]))
    return results[0] if results else None


@_timed
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


@_timed
def count_images(category: str) -> int:
    db = get_db()
    return db[COLLECTION_NAME].count_documents({"category": category})


@_timed
def count_available_images(category: str) -> int:
    """Đếm ảnh KHẢ DỤNG ngay bây giờ (chưa gửi hoặc đã qua cooldown 1 tiếng)."""
    db = get_db()
    return db[COLLECTION_NAME].count_documents(_available_query(category=category))


@_timed
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


@_timed
def merge_category_images(from_slug: str, to_slug: str) -> int:
    """Chuyển toàn bộ ảnh đang gắn category=from_slug sang category=to_slug —
    dùng khi gộp 2 chủ đề làm 1. An toàn tuyệt đối với việc trùng lặp: index
    unique nằm trên "image_url" cho CẢ collection (không tính theo từng
    category), nên 1 URL không bao giờ tồn tại đồng thời ở 2 category khác
    nhau ngay từ đầu (crawl_job.py đã tự bỏ qua URL trùng khi insert) — đổi
    category cho ảnh đã có sẵn không bao giờ gây xung đột unique index.
    Trả về số ảnh đã chuyển thành công."""
    db = get_db()
    result = db[COLLECTION_NAME].update_many(
        {"category": from_slug},
        {"$set": {"category": to_slug}},
    )
    return result.modified_count


# ============================================================
# Category tuỳ chỉnh — cho phép admin thêm/sửa/xoá chủ đề qua lệnh Discord
# mà không cần sửa categories.py + deploy lại. Lưu riêng 1 collection,
# merge với CATEGORIES tĩnh ở tầng bot.py.
# ============================================================

@_timed
def get_custom_categories() -> dict:
    db = get_db()
    docs = db[CUSTOM_CATEGORIES_COLLECTION].find({})
    result = {}
    for d in docs:
        # Tương thích ngược: category thêm TRƯỚC khi có tính năng nhiều từ
        # khóa chỉ có field "keyword" (chuỗi đơn) trong Mongo, chưa có
        # "keywords" (danh sách). Chuẩn hoá về "keywords" ngay tại đây để
        # phần còn lại của code (bot.py, crawl_job.py) chỉ cần biết 1 format
        # duy nhất — không cần migrate dữ liệu cũ thủ công trong Mongo.
        keywords = d.get("keywords") or ([d["keyword"]] if d.get("keyword") else [])
        result[d["_id"]] = {"label": d["label"], "keywords": keywords, "nsfw": d.get("nsfw", False)}
    return result


@_timed
def custom_category_exists(slug: str) -> bool:
    db = get_db()
    return db[CUSTOM_CATEGORIES_COLLECTION].count_documents({"_id": slug}) > 0


@_timed
def add_custom_category(slug: str, label: str, keywords: list, nsfw: bool = False) -> None:
    db = get_db()
    db[CUSTOM_CATEGORIES_COLLECTION].update_one(
        {"_id": slug},
        {"$set": {"label": label, "keywords": list(keywords), "nsfw": nsfw}, "$unset": {"keyword": ""}},
        upsert=True,
    )


@_timed
def edit_custom_category(slug: str, label: str = None, keywords: list = None, nsfw: bool = None) -> bool:
    """Sửa label/keywords/nsfw của 1 category CUSTOM đã tồn tại. Trả về False nếu chưa từng thêm qua lệnh."""
    db = get_db()
    if not custom_category_exists(slug):
        return False
    update = {}
    if label:
        update["label"] = label
    if keywords:
        update["keywords"] = list(keywords)
    if nsfw is not None:
        update["nsfw"] = nsfw
    if not update:
        return True
    # $unset "keyword" (field cũ, chuỗi đơn) mỗi lần sửa keywords, để dữ liệu
    # dần chuyển hẳn sang format mới thay vì để field cũ nằm thừa mãi.
    op = {"$set": update}
    if keywords:
        op["$unset"] = {"keyword": ""}
    db[CUSTOM_CATEGORIES_COLLECTION].update_one({"_id": slug}, op)
    return True


@_timed
def remove_custom_category(slug: str) -> bool:
    """Trả về True nếu xoá được (category tồn tại và là custom)."""
    db = get_db()
    result = db[CUSTOM_CATEGORIES_COLLECTION].delete_one({"_id": slug})
    return result.deleted_count > 0


# ============================================================
# Metadata job crawl (dùng để biết lần crawl gần nhất là khi nào — phục vụ
# việc tự crawl ngay category mới thêm nếu đã lâu chưa có lần crawl nào).
# ============================================================

@_timed
def get_last_crawl_time():
    db = get_db()
    doc = db[META_COLLECTION].find_one({"_id": "last_crawl"})
    return doc["at"] if doc else None


@_timed
def set_last_crawl_time() -> None:
    db = get_db()
    db[META_COLLECTION].update_one(
        {"_id": "last_crawl"},
        {"$set": {"at": now_utc()}},
        upsert=True,
    )


@_timed
def get_category_bookmark(category_key: str):
    """
    Lấy bookmark Pinterest đã lưu cho category này (từ lần crawl trước) —
    dùng để lấy TRANG TIẾP THEO thay vì luôn lặp lại trang đầu, giảm tỉ lệ
    ảnh trùng (skip) tăng dần theo thời gian khi crawl nhiều lần cho cùng
    1 category/keyword. Trả None nếu chưa từng crawl category này, hoặc
    Pinterest đã báo hết trang (bookmark reset về đầu).
    """
    db = get_db()
    doc = db[META_COLLECTION].find_one({"_id": f"bookmark_{category_key}"})
    return doc["bookmark"] if doc else None


@_timed
def set_category_bookmark(category_key: str, bookmark) -> None:
    """bookmark=None nghĩa là Pinterest đã hết trang -> lần crawl sau bắt đầu lại từ đầu."""
    db = get_db()
    db[META_COLLECTION].update_one(
        {"_id": f"bookmark_{category_key}"},
        {"$set": {"bookmark": bookmark, "updated_at": now_utc()}},
        upsert=True,
    )


# ============================================================
# Phiên xem ảnh (paginator session) — lưu trạng thái nút Trước/Sau theo
# message_id để nút bấm hoạt động vĩnh viễn, kể cả sau khi bot restart
# (không phụ thuộc bộ nhớ RAM của tiến trình bot).
# ============================================================

@_timed
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


@_timed
def get_paginator_session(message_id: str):
    db = get_db()
    return db[PAGINATOR_SESSIONS_COLLECTION].find_one({"_id": message_id})


@_timed
def update_paginator_index(message_id: str, index: int) -> None:
    """Chỉ cập nhật vị trí đang xem (đi Trước, hoặc đi Sau vào ảnh đã có sẵn
    trong bộ đệm) — KHÔNG đụng tới mảng images, để không bao giờ đè mất ảnh
    mà tác vụ tải trước (prefetch) nền vừa $push thêm vào cùng lúc."""
    db = get_db()
    db[PAGINATOR_SESSIONS_COLLECTION].update_one(
        {"_id": message_id},
        {"$set": {"index": index, "updated_at": now_utc()}},
    )


@_timed
def append_image_and_set_index(message_id: str, url: str, index: int) -> None:
    """Thêm 1 ảnh MỚI vào cuối mảng images + di chuyển tới đúng ảnh đó, dùng
    $push (nguyên tử) thay vì ghi đè cả mảng — an toàn nếu có tác vụ prefetch
    nền khác cũng đang $push vào cùng session ngay lúc này."""
    db = get_db()
    db[PAGINATOR_SESSIONS_COLLECTION].update_one(
        {"_id": message_id},
        {"$push": {"images": url}, "$set": {"index": index, "updated_at": now_utc()}},
    )


@_timed
def append_image_to_session(message_id: str, url: str) -> bool:
    """Tải trước (prefetch): thêm 1 ảnh vào cuối mảng images mà KHÔNG đổi
    index đang xem (ảnh này chỉ để sẵn đó, chờ user thật sự bấm Sau tới nó).
    Trả về False nếu session không còn tồn tại nữa (tin nhắn đã quá cũ/bị
    dọn) — khi đó bên gọi chỉ cần bỏ qua, không phải lỗi cần xử lý gì thêm."""
    db = get_db()
    result = db[PAGINATOR_SESSIONS_COLLECTION].update_one(
        {"_id": message_id},
        {"$push": {"images": url}, "$set": {"updated_at": now_utc()}},
    )
    return result.matched_count > 0


# --- Phiên /random: khác phiên /img ở chỗ MỖI ẢNH có thể thuộc 1 category
# khác nhau (random thật trên toàn kho, không cố định 1 category), nên mỗi
# phần tử trong "items" phải tự mang theo label/category riêng của nó thay
# vì dùng chung 1 "label" cho cả session như phiên /img. "allowed_keys" lưu
# lại danh sách category được phép ở kênh đó (đã lọc NSFW) NGAY LÚC TẠO
# session, để lúc tải trước (prefetch) trong nền — không có sẵn interaction/
# channel để tự tính lại — vẫn biết chính xác phạm vi được random.

@_timed
def save_random_paginator_session(message_id: str, items: list, allowed_keys: list, index: int, author_id: int) -> None:
    db = get_db()
    db[PAGINATOR_SESSIONS_COLLECTION].update_one(
        {"_id": message_id},
        {"$set": {
            "mode": "random",
            "items": items,
            "allowed_keys": allowed_keys,
            "index": index,
            "author_id": author_id,
            "created_at": now_utc(),
            "updated_at": now_utc(),
        }},
        upsert=True,
    )


@_timed
def append_random_item_and_set_index(message_id: str, item: dict, index: int) -> None:
    db = get_db()
    db[PAGINATOR_SESSIONS_COLLECTION].update_one(
        {"_id": message_id},
        {"$push": {"items": item}, "$set": {"index": index, "updated_at": now_utc()}},
    )


@_timed
def append_random_item_to_session(message_id: str, item: dict) -> bool:
    """Tải trước (prefetch) cho phiên /random — cùng nguyên tắc $push nguyên
    tử như append_image_to_session, chỉ khác là mỗi phần tử là 1 dict
    {url, category_key, label} thay vì 1 URL đơn."""
    db = get_db()
    result = db[PAGINATOR_SESSIONS_COLLECTION].update_one(
        {"_id": message_id},
        {"$push": {"items": item}, "$set": {"updated_at": now_utc()}},
    )
    return result.matched_count > 0


# ============================================================
# Dọn dẹp kho ảnh
# ============================================================

@_timed
def delete_overused_images(category: str, min_sent_count: int) -> int:
    """Xoá ảnh đã bị gửi >= min_sent_count lần trong category. Trả về số đã xoá."""
    db = get_db()
    result = db[COLLECTION_NAME].delete_many({
        "category": category,
        "sent_count": {"$gte": min_sent_count},
    })
    return result.deleted_count


@_timed
def get_all_image_urls(category: str) -> list:
    db = get_db()
    return [d["image_url"] for d in db[COLLECTION_NAME].find({"category": category}, {"image_url": 1})]


@_timed
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

@_timed
def save_showcase_board(message_id: str, category_key: str) -> None:
    db = get_db()
    db[SHOWCASE_BOARDS_COLLECTION].update_one(
        {"_id": message_id},
        {"$set": {"category_key": category_key, "created_at": now_utc()}},
        upsert=True,
    )


@_timed
def get_showcase_board(message_id: str):
    db = get_db()
    return db[SHOWCASE_BOARDS_COLLECTION].find_one({"_id": message_id})


# ============================================================
# Cấu hình riêng theo từng server (guild) — cho phép mỗi server tự đặt
# giới hạn kênh/role dùng lệnh ảnh qua /config, thay vì dùng chung 1 cấu
# hình toàn cục (ALLOWED_CHANNEL_IDS/ALLOWED_ROLE_IDS trên Render) cho mọi
# server bot có mặt.
# ============================================================

@_timed
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


@_timed
def add_guild_allowed_channel(guild_id: int, channel_id: int) -> None:
    db = get_db()
    db[GUILD_CONFIGS_COLLECTION].update_one(
        {"_id": guild_id}, {"$addToSet": {"allowed_channel_ids": channel_id}}, upsert=True
    )


@_timed
def remove_guild_allowed_channel(guild_id: int, channel_id: int) -> None:
    db = get_db()
    db[GUILD_CONFIGS_COLLECTION].update_one({"_id": guild_id}, {"$pull": {"allowed_channel_ids": channel_id}})


@_timed
def clear_guild_allowed_channels(guild_id: int) -> None:
    db = get_db()
    db[GUILD_CONFIGS_COLLECTION].update_one(
        {"_id": guild_id}, {"$set": {"allowed_channel_ids": []}}, upsert=True
    )


@_timed
def add_guild_allowed_role(guild_id: int, role_id: int) -> None:
    db = get_db()
    db[GUILD_CONFIGS_COLLECTION].update_one(
        {"_id": guild_id}, {"$addToSet": {"allowed_role_ids": role_id}}, upsert=True
    )


@_timed
def remove_guild_allowed_role(guild_id: int, role_id: int) -> None:
    db = get_db()
    db[GUILD_CONFIGS_COLLECTION].update_one({"_id": guild_id}, {"$pull": {"allowed_role_ids": role_id}})


@_timed
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

@_timed
def get_guild_setup_category_id(guild_id: int):
    db = get_db()
    doc = db[GUILD_CONFIGS_COLLECTION].find_one({"_id": guild_id})
    return doc.get("setup_category_id") if doc else None


@_timed
def set_guild_setup_category_id(guild_id: int, category_id: int) -> None:
    db = get_db()
    db[GUILD_CONFIGS_COLLECTION].update_one(
        {"_id": guild_id}, {"$set": {"setup_category_id": category_id}}, upsert=True
    )
