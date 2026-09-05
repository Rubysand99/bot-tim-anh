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
from urllib.parse import quote

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
    # QUAN TRỌNG: header HTTP chỉ chấp nhận ASCII/latin-1 — query tiếng Việt
    # có dấu (vd "cảnh đẹp", "học sinh") PHẢI được URL-encode bằng quote()
    # trước khi nhét vào Referer, nếu không sẽ crash với
    # "'latin-1' codec can't encode character..." ngay khi gửi request.
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/javascript, */*, q=0.01",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": f"{SEARCH_PAGE_URL}?q={quote(query)}",
        "X-Requested-With": "XMLHttpRequest",
        "X-Pinterest-AppState": "active",
        "X-Pinterest-PWS-Handler": "www/search/[scope].js",
    }


def _build_params(query: str, bookmark: str = None) -> dict:
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
    # QUAN TRỌNG - phân trang: nếu có bookmark từ lần gọi trước (lấy từ
    # response["resource_response"]["bookmark"]), truyền vào đây để Pinterest
    # trả về TRANG TIẾP THEO thay vì luôn lặp lại đúng trang đầu tiên. Đã xác
    # nhận thực tế field này tồn tại và hoạt động (test qua Termux 09/2026).
    # Không có phân trang -> crawl nhiều lần cho cùng 1 category/keyword sẽ
    # luôn lấy lại gần như đúng bộ ảnh cũ, tỉ lệ trùng (skip) tăng dần theo
    # thời gian dù DB càng lúc càng có nhiều ảnh.
    if bookmark:
        options["bookmarks"] = [bookmark]

    payload = {"options": options, "context": {}}
    return {
        "source_url": f"/search/pins/?q={query}",
        "data": json.dumps(payload),
        "_": str(int(time.time() * 1000)),
    }


def search_pinterest_images(query: str, limit: int = 20, timeout: int = 10, bookmark: str = None) -> tuple:
    """
    Tìm ảnh trên Pinterest theo từ khóa.

    bookmark: nếu truyền vào (lấy từ bookmark trả về của lần gọi TRƯỚC), lấy
    TRANG TIẾP THEO thay vì luôn lặp lại trang đầu tiên — quan trọng khi
    crawl_job.py chạy nhiều lần cho cùng 1 category, tránh tỉ lệ trùng
    (skip vì DuplicateKeyError) tăng dần theo thời gian.

    Trả về: (list[str] URL ảnh, next_bookmark hoặc None nếu hết trang).
    Ném Exception nếu request lỗi mạng hoặc Pinterest đổi cấu trúc response
    (khi đó payload sẽ không còn key như code đang mong đợi).
    """
    session = requests.Session()
    session.headers.update(_build_headers(query))

    response = session.get(BASE_URL, params=_build_params(query, bookmark=bookmark), timeout=timeout)
    response.raise_for_status()

    payload = response.json()
    resource_response = payload.get("resource_response", {})
    results = resource_response.get("data", {}).get("results", [])
    # "-end-" là giá trị Pinterest trả khi đã hết trang, không phải bookmark
    # thật để dùng tiếp — coi như None (không còn trang nào nữa).
    next_bookmark = resource_response.get("bookmark")
    if next_bookmark == "-end-":
        next_bookmark = None

    if not results:
        return [], next_bookmark

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

    return image_urls, next_bookmark


def search_pinterest_images_with_retry(query: str, limit: int = 20, retries: int = 2, bookmark: str = None) -> tuple:
    """
    Bản có retry + backoff, dùng an toàn hơn trong bot (chạy trong executor).

    QUAN TRỌNG về thời gian chờ: retries=2, timeout=7s/lần -> worst case
    2*7 + 3 (nghỉ giữa 2 lần) = 17s. Trước đây retries=3, timeout=10s có
    worst case lên tới 36 GIÂY — quá lâu để người dùng chờ 1 cú bấm nút,
    và khiến ứng dụng Discord (đặc biệt bản mobile) có thể tự hiển thị lỗi
    "không phản hồi kịp thời" dù bot vẫn đang xử lý bình thường phía sau,
    do các nút bấm defer() không có chỉ báo "đang tải" trực quan như lệnh
    slash command thông thường.

    Trả về: (list[str] URL ảnh, next_bookmark hoặc None).
    """
    last_error = None
    for attempt in range(retries):
        try:
            images, next_bookmark = search_pinterest_images(query, limit=limit, timeout=7, bookmark=bookmark)
            if images:
                return images, next_bookmark
            last_error = None
        except Exception as err:  # noqa: BLE001 - muốn bắt mọi lỗi để retry
            last_error = err

        if attempt < retries - 1:
            time.sleep(3)

    if last_error:
        raise last_error
    return [], None
