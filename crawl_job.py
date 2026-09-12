"""
crawl_job.py
Chạy định kỳ (qua GitHub Actions cron, xem .github/workflows/main.yml) để
crawl ảnh Pinterest theo từng category (categories.py + category admin thêm
qua Discord, lưu trong MongoDB), lưu vào MongoDB.

Bot (bot.py) chỉ đọc ảnh đã crawl sẵn ở đây — không tự cào Pinterest trực
tiếp nữa (đã bỏ hẳn cơ chế fallback, xem README mục "Khi 1 category hết ảnh
khả dụng"). Nếu 1 category hết ảnh giữa 2 lần crawl, bot báo user chờ tới
lần crawl kế tiếp thay vì tự cào ngay.

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

from categories import CATEGORIES, get_keywords
from db import (
    get_db,
    COLLECTION_NAME,
    get_custom_categories,
    count_available_images,
    set_last_crawl_time,
    get_category_bookmark,
    set_category_bookmark,
    is_valid_image_url,
)
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


# Discord giới hạn URL trong embeds.image.url tối đa 2048 ký tự — từng gặp
# thực tế 1 URL từ Pinterest vượt giới hạn này (nguyên nhân chưa rõ, có thể
# lỗi lạ từ phía Pinterest), khiến Discord từ chối cả embed với lỗi 400
# "Invalid Form Body" khi bot cố gửi/sửa tin nhắn chứa ảnh đó — phát hiện
# qua self-test soak trong bot.py. Validate trước khi lưu để chặn từ gốc,
# không bao giờ để lọt vào DB nữa (dùng chung is_valid_image_url từ db.py).


def _crawl_one_keyword(slug: str, keyword: str, collection):
    """Crawl 1 (category, từ khóa) — bookmark phân trang lưu riêng theo
    TỪNG CẶP slug+keyword (không dùng chung 1 bookmark cho cả category),
    vì mỗi từ khóa có kết quả tìm kiếm Pinterest khác nhau, cần phân trang
    độc lập với nhau. Trả về (inserted, skipped, had_error)."""
    bookmark_key = f"{slug}::{keyword}"
    bookmark = get_category_bookmark(bookmark_key)
    try:
        image_urls, next_bookmark = search_pinterest_images_with_retry(
            keyword, limit=IMAGES_PER_CATEGORY, bookmark=bookmark
        )
    except Exception as err:
        logger.warning(f"Lỗi crawl category '{slug}' (từ khóa '{keyword}'): {err}")
        return 0, 0, True

    set_category_bookmark(bookmark_key, next_bookmark)

    inserted = 0
    skipped = 0
    invalid = 0
    for url in image_urls:
        if not is_valid_image_url(url):
            invalid += 1
            logger.warning(f"Bỏ qua URL không hợp lệ khi crawl '{slug}' (dài {len(url) if url else 0} ký tự): {str(url)[:100]}...")
            continue
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

    if invalid:
        logger.warning(f"Category '{slug}' (từ khóa '{keyword}'): bỏ qua {invalid} URL không hợp lệ trong lần crawl này.")

    return inserted, skipped, False


def crawl_category(slug: str, keywords: list):
    """
    Crawl LẦN LƯỢT từng từ khóa trong danh sách cho category này (1 category
    giờ có thể gộp nhiều từ khóa — ảnh crawl từ mọi từ khóa đều lưu chung
    dưới cùng 1 category, không phân biệt từ khóa nào tìm ra). Mỗi từ khóa
    có bookmark phân trang Pinterest riêng (xem _crawl_one_keyword).

    Trả về (tổng số ảnh mới thêm, tổng số ảnh đã trùng/đã có sẵn, có lỗi
    hay không). had_error chỉ True nếu TẤT CẢ từ khóa đều lỗi — 1 từ khóa
    lỗi nhưng từ khóa khác vẫn crawl được thì vẫn coi là category thành
    công (một phần), không chặn ảnh mới từ các từ khóa còn lại.
    """
    db = get_db()
    collection = db[COLLECTION_NAME]

    total_inserted = 0
    total_skipped = 0
    error_count = 0
    for keyword in keywords:
        inserted, skipped, had_error = _crawl_one_keyword(slug, keyword, collection)
        total_inserted += inserted
        total_skipped += skipped
        if had_error:
            error_count += 1

    had_error = error_count > 0 and error_count == len(keywords)
    return total_inserted, total_skipped, had_error


def main():
    logger.info(f"=== Bắt đầu crawl lúc {datetime.now(timezone.utc).isoformat()} ===")
    all_categories = get_all_categories()

    total_inserted = 0
    total_skipped = 0
    failed_categories = []

    for slug, info in all_categories.items():
        keywords = get_keywords(info)
        logger.info(f"→ Crawl category: {info['label']} (từ khóa: {', '.join(keywords)})")
        inserted, skipped, had_error = crawl_category(slug, keywords)
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
