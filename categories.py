# Danh sách category ảnh + từ khóa dùng để crawl trên Pinterest.
# "label" = tên hiển thị trong Discord (dropdown chọn chủ đề).
# "keywords" = DANH SÁCH từ khóa search Pinterest khi crawl (1 category có
#              thể gộp nhiều từ khóa, vd category "cá" có thể crawl bằng
#              cả "cá heo" lẫn "cá mập" — ảnh crawl được từ mọi từ khóa đều
#              lưu chung dưới 1 category, không phân biệt từ khóa nào tìm ra).
# "nsfw" = True nếu chủ đề này chỉ nên hiển thị ở kênh Discord đã đánh dấu
#          Age-Restricted (nsfw). Mặc định False cho mọi category bên dưới —
#          tự sửa lại True nếu bạn thấy chủ đề nào cần giới hạn kênh.
#
# Muốn thêm/sửa/xoá category: chỉnh trực tiếp dict bên dưới.
# Lưu ý: Discord slash command chỉ cho tối đa 25 lựa chọn (choices).

CATEGORIES = {
    "sylphiette": {"label": "Sylphiette>_<", "keywords": ["sylphiette greyrat"], "nsfw": False},
    "gaixinh": {"label": "Hot girl vn", "keywords": ["khoe dáng"], "nsfw": False},
    "gaicute": {"label": "bổ sung vitamin A 😋", "keywords": ["gái xinh"], "nsfw": False},
    "meo": {"label": "Mèo méo meo mèo meo", "keywords": ["cute cats"], "nsfw": False},
    "cho": {"label": "gâu gâu ẳng ẳng", "keywords": ["cute dogs"], "nsfw": False},
    "canhdep": {"label": "Cảnh đẹp thiên nhiên 🌠", "keywords": ["natural scenery"], "nsfw": False},
}


def get_keywords(info: dict) -> list:
    """
    Chuẩn hoá danh sách từ khóa của 1 category — dùng CHUNG cho bot.py và
    crawl_job.py để 2 nơi không bị lệch cách đọc. Ưu tiên "keywords" (danh
    sách, format mới), fallback về "keyword" (chuỗi đơn, format cũ) để
    tương thích ngược với category custom đã lưu trong MongoDB TỪ TRƯỚC khi
    có tính năng nhiều từ khóa — không cần migrate dữ liệu cũ thủ công.
    Luôn trả về list không rỗng (trừ khi category thật sự thiếu cả 2 field).
    """
    keywords = info.get("keywords")
    if keywords:
        return list(keywords)
    single = info.get("keyword")
    if single:
        return [single]
    return []


def keywords_display(info: dict) -> str:
    """Chuỗi hiển thị các từ khóa, dùng cho /stats, xác nhận /addcategory..."""
    return ", ".join(get_keywords(info)) or "(chưa có từ khóa)"
