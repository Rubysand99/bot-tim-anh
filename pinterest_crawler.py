"""
pinterest_crawler.py
Crawl ảnh từ Pinterest bằng endpoint nội bộ (undocumented) mà chính trang
pinterest.com dùng khi bạn search trên web. Không cần login, không cần
Selenium/Playwright vì đây là API trả JSON thẳng (không phải HTML render JS).

LƯU Ý QUAN TRỌNG:
- Đây là API không chính thức, Pinterest có thể đổi cấu trúc bất kỳ lúc nào
  khiến crawler này ngừng hoạt động. Nếu gặp lỗi parse, cần kiểm tra lại
  response JSON thực tế và cập nhật lại đường dẫn key cho phù hợp.
- Dùng nhiều/liên tục có thể bị Pinterest chặn IP tạm thời. Nên có retry
  + backoff, tránh spam nhiều request cùng lúc.
"""

import json
import random
import time

import requests

BASE_URL = "https://www.pinterest.com/resource/BaseSearchResource/get/"
SEARCH_PAGE_URL = "https://www.pinterest.com/search/pins/"

# Xoay vòng User-Agent để giảm khả năng bị nhận diện là bot
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
]

# Thứ tự ưu tiên độ phân giải ảnh, cao xuống thấp
RESOLUTION_PRIORITY = ("orig", "736x", "564x", "474x", "236x")


def _build_headers(query: str) -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/javascript, */*, q=0.01",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": f"{SEARCH_PAGE_URL}?q={query}",
        "X-Requested-With": "XMLHttpRequest",
        "X-Pinterest-AppState": "active",
        "X-Pinterest-PWS-Handler": "www/search/[scope].js",
    }


def _build_params(query: str) -> dict:
    options = {
        "applied_unified_filters": None,
        "appliedProductFilters": "---",
        "article": None,
        "auto_correction_disabled": False,
        "corpus": None,
        "customized_rerank_type": None,
        "domains": None,
        "filters": None,
        "journey_depth": None,
        "page_size": 25,
        "price_max": None,
        "price_min": None,
        "query_pin_sigs": None,
        "query": query,
        "redux_normalize_feed": True,
        "rs": "typed",
        "scope": "pins",
        "selected_one_bar_modules": None,
        "source_id": None,
        "source_module_id": None,
        "top_pin_id": None,
        "top_pin_ids": None,
    }
    payload = {"options": options, "context": {}}
    return {
        "source_url": f"/search/pins/?q={query}",
        "data": json.dumps(payload),
        "_": str(int(time.time() * 1000)),
    }


def search_pinterest_images(query: str, limit: int = 20, timeout: int = 10) -> list:
    """
    Tìm ảnh trên Pinterest theo từ khóa.

    Trả về: list[str] các URL ảnh (độ phân giải cao nhất có sẵn cho mỗi pin).
    Ném Exception nếu request lỗi mạng hoặc Pinterest đổi cấu trúc response
    (khi đó payload sẽ không còn key như code đang mong đợi).
    """
    session = requests.Session()
    session.headers.update(_build_headers(query))

    response = session.get(BASE_URL, params=_build_params(query), timeout=timeout)
    response.raise_for_status()

    payload = response.json()
    results = (
        payload.get("resource_response", {})
        .get("data", {})
        .get("results", [])
    )

    if not results:
        return []

    image_urls = []
    for pin in results:
        images = pin.get("images")
        if not images:
            continue
        for key in RESOLUTION_PRIORITY:
            candidate = images.get(key)
            if candidate and candidate.get("url"):
                image_urls.append(candidate["url"])
                break
        if len(image_urls) >= limit:
            break

    return image_urls


def search_pinterest_images_with_retry(query: str, limit: int = 20, retries: int = 3) -> list:
    """Bản có retry + backoff, dùng an toàn hơn trong bot (chạy trong executor)."""
    last_error = None
    for attempt in range(retries):
        try:
            images = search_pinterest_images(query, limit=limit)
            if images:
                return images
            last_error = None
        except Exception as err:  # noqa: BLE001 - muốn bắt mọi lỗi để retry
            last_error = err

        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))

    if last_error:
        raise last_error
    return []
