"""
crawl_job.py
Chạy định kỳ (qua GitHub Actions cron) để crawl ảnh Pinterest theo từng
category trong categories.py, lưu vào MongoDB. Bot KHÔNG gọi Pinterest
trực tiếp nữa — chỉ đọc dữ liệu đã crawl sẵn từ MongoDB, giúp giảm hẳn
rủi ro bị Pinterest chặn IP của Render.

Chạy thủ công (test local qua Termux):
    MONGO_URI="mongodb+srv://..." python crawl_job.py
"""

import sys
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError

from categories import CATEGORIES
from db import get_db, COLLECTION_NAME
from pinterest_crawler import search_pinterest_images_with_retry

IMAGES_PER_CATEGORY = 30  # số ảnh tối đa lấy về mỗi lần crawl / category


def crawl_category(slug: str, keyword: str):
    """Trả về (số ảnh mới thêm, số ảnh đã trùng/đã có sẵn)."""
    db = get_db()
    collection = db[COLLECTION_NAME]

    try:
        image_urls = search_pinterest_images_with_retry(keyword, limit=IMAGES_PER_CATEGORY)
    except Exception as err:
        print(f"  ⚠️ Lỗi crawl category '{slug}': {err}")
        return 0, 0

    inserted = 0
    skipped = 0
    for url in image_urls:
        try:
            collection.insert_one({
                "image_url": url,
                "category": slug,
                "created_at": datetime.now(timezone.utc),
                "last_sent_at": None,
                "sent_count": 0,
            })
            inserted += 1
        except DuplicateKeyError:
            skipped += 1

    return inserted, skipped


def main():
    print(f"=== Bắt đầu crawl lúc {datetime.now(timezone.utc).isoformat()} ===")
    total_inserted = 0
    total_skipped = 0

    for slug, info in CATEGORIES.items():
        print(f"→ Crawl category: {info['label']} (keyword: {info['keyword']})")
        inserted, skipped = crawl_category(slug, info["keyword"])
        print(f"  + {inserted} ảnh mới, {skipped} ảnh trùng (đã có sẵn)")
        total_inserted += inserted
        total_skipped += skipped

    print(f"=== Xong. Tổng: {total_inserted} ảnh mới, {total_skipped} ảnh trùng ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ Crawl job thất bại: {e}")
        sys.exit(1)
