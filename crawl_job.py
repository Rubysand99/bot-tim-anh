"""
crawl_job.py
Chạy định kỳ (qua GitHub Actions cron, xem .github/workflows/main.yml) để
crawl ảnh Pinterest theo từng category trong categories.py, lưu vào MongoDB.

Bot (bot.py) ưu tiên đọc ảnh đã crawl sẵn ở đây trước, chỉ cào Pinterest
trực tiếp khi DB hết ảnh khả dụng cho category đó (xem hàm
_fetch_next_image_url trong bot.py) — giúp giảm hẳn tần suất gọi Pinterest
trực tiếp và rủi ro bị chặn IP của Render.

Chạy thủ công (test local qua Termux):
    MONGO_URI="mongodb+srv://..." python crawl_job.py
"""

import logging
import sys
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError

from categories import CATEGORIES
from db import get_db, COLLECTION_NAME
from pinterest_crawler import search_pinterest_images_with_retry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("crawl_job")

IMAGES_PER_CATEGORY = 30  # số ảnh tối đa lấy về mỗi lần crawl / category


def crawl_category(slug: str, keyword: str):
    """Trả về (số ảnh mới thêm, số ảnh đã trùng/đã có sẵn, có lỗi hay không)."""
    db = get_db()
    collection = db[COLLECTION_NAME]

    try:
        image_urls = search_pinterest_images_with_retry(keyword, limit=IMAGES_PER_CATEGORY)
    except Exception as err:
        logger.warning(f"Lỗi crawl category '{slug}': {err}")
        return 0, 0, True

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

    return inserted, skipped, False


def main():
    logger.info(f"=== Bắt đầu crawl lúc {datetime.now(timezone.utc).isoformat()} ===")
    total_inserted = 0
    total_skipped = 0
    failed_categories = []

    for slug, info in CATEGORIES.items():
        logger.info(f"→ Crawl category: {info['label']} (keyword: {info['keyword']})")
        inserted, skipped, had_error = crawl_category(slug, info["keyword"])
        logger.info(f"  + {inserted} ảnh mới, {skipped} ảnh trùng (đã có sẵn)")
        total_inserted += inserted
        total_skipped += skipped
        if had_error:
            failed_categories.append(slug)

    logger.info(f"=== Xong. Tổng: {total_inserted} ảnh mới, {total_skipped} ảnh trùng ===")

    if failed_categories:
        logger.warning(f"Các category bị lỗi khi crawl: {', '.join(failed_categories)}")

    # Nếu TOÀN BỘ category đều lỗi (thường do Pinterest chặn IP runner) thì
    # coi đây là job thất bại thay vì âm thầm "thành công" với 0 ảnh mới —
    # để GitHub Actions báo đỏ, bạn dễ nhận ra ngay thay vì phát hiện muộn.
    if len(failed_categories) == len(CATEGORIES):
        raise RuntimeError(
            "Tất cả category đều crawl lỗi — có thể Pinterest đang chặn IP của GitHub Actions runner."
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Crawl job thất bại: {e}")
        sys.exit(1)
