"""
crawl_job.py
Chạy định kỳ (qua GitHub Actions cron, xem .github/workflows/main.yml) để
crawl ảnh Pinterest theo từng category (categories.py + category admin thêm
qua Discord, lưu trong MongoDB), lưu vào MongoDB.

Bot (bot.py) ưu tiên đọc ảnh đã crawl sẵn ở đây trước, chỉ cào Pinterest
trực tiếp khi DB hết ảnh khả dụng cho category đó (xem hàm
_fetch_next_image_url trong bot.py) — giúp giảm hẳn tần suất gọi Pinterest
trực tiếp và rủi ro bị chặn IP của Render.

Biến môi trường tuỳ chọn:
    DISCORD_WEBHOOK_URL - nếu set, job sẽ gửi cảnh báo qua webhook này khi:
        - toàn bộ category đều crawl lỗi
        - có category sắp cạn ảnh khả dụng (dưới LOW_STOCK_THRESHOLD)

Chạy thủ công (test local qua Termux):
    MONGO_URI="mongodb+srv://..." python crawl_job.py
"""

import logging
import os
import sys
from datetime import datetime, timezone

import requests
from pymongo.errors import DuplicateKeyError

from categories import CATEGORIES
from db import get_db, COLLECTION_NAME, get_custom_categories, count_available_images, set_last_crawl_time
from pinterest_crawler import search_pinterest_images_with_retry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("crawl_job")

IMAGES_PER_CATEGORY = 30  # số ảnh tối đa lấy về mỗi lần crawl / category
LOW_STOCK_THRESHOLD = 5   # cảnh báo nếu 1 category còn dưới ngần này ảnh khả dụng


def send_discord_alert(message: str) -> None:
    """Gửi cảnh báo qua Discord webhook (nếu đã cấu hình DISCORD_WEBHOOK_URL)."""
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logger.info("DISCORD_WEBHOOK_URL chưa được set, bỏ qua gửi cảnh báo.")
        return
    try:
        requests.post(webhook_url, json={"content": message}, timeout=10)
    except Exception as e:
        logger.warning(f"Gửi cảnh báo Discord webhook thất bại: {e}")


def get_all_categories() -> dict:
    """Gộp category tĩnh (categories.py) + category admin thêm qua Discord (MongoDB)."""
    merged = dict(CATEGORIES)
    try:
        merged.update(get_custom_categories())
    except Exception as e:
        logger.warning(f"Không đọc được custom categories từ DB: {e}")
    return merged


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
    all_categories = get_all_categories()

    total_inserted = 0
    total_skipped = 0
    failed_categories = []

    for slug, info in all_categories.items():
        logger.info(f"→ Crawl category: {info['label']} (keyword: {info['keyword']})")
        inserted, skipped, had_error = crawl_category(slug, info["keyword"])
        logger.info(f"  + {inserted} ảnh mới, {skipped} ảnh trùng (đã có sẵn)")
        total_inserted += inserted
        total_skipped += skipped
        if had_error:
            failed_categories.append(slug)

    logger.info(f"=== Xong. Tổng: {total_inserted} ảnh mới, {total_skipped} ảnh trùng ===")

    try:
        set_last_crawl_time()
    except Exception as e:
        logger.warning(f"Không ghi được thời điểm crawl gần nhất: {e}")

    if failed_categories:
        logger.warning(f"Các category bị lỗi khi crawl: {', '.join(failed_categories)}")

    # Nếu TOÀN BỘ category đều lỗi (thường do Pinterest chặn IP runner) thì
    # coi đây là job thất bại thay vì âm thầm "thành công" với 0 ảnh mới —
    # để GitHub Actions báo đỏ, bạn dễ nhận ra ngay thay vì phát hiện muộn.
    if failed_categories and len(failed_categories) == len(all_categories):
        send_discord_alert(
            "🔴 **crawl_job thất bại toàn bộ** — tất cả category đều crawl lỗi, "
            "có thể Pinterest đang chặn IP của GitHub Actions runner."
        )
        raise RuntimeError(
            "Tất cả category đều crawl lỗi — có thể Pinterest đang chặn IP của GitHub Actions runner."
        )

    # Cảnh báo category sắp cạn ảnh khả dụng (không tính là lỗi job)
    low_stock = []
    for slug, info in all_categories.items():
        if slug in failed_categories:
            continue
        try:
            available = count_available_images(slug)
        except Exception as e:
            logger.warning(f"Không đếm được ảnh khả dụng cho '{slug}': {e}")
            continue
        if available < LOW_STOCK_THRESHOLD:
            low_stock.append(f"- {info['label']} (`{slug}`): còn {available} ảnh khả dụng")

    if low_stock:
        alert_lines = "\n".join(low_stock)
        logger.warning(f"Các category sắp cạn ảnh:\n{alert_lines}")
        send_discord_alert(f"🟡 **Cảnh báo: sắp cạn ảnh khả dụng**\n{alert_lines}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Crawl job thất bại: {e}")
        sys.exit(1)
