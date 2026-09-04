# Danh sách category ảnh + từ khóa dùng để crawl trên Pinterest.
# "label" = tên hiển thị trong Discord (dropdown chọn chủ đề).
# "keyword" = từ khóa search Pinterest khi crawl.
# "nsfw" = True nếu chủ đề này chỉ nên hiển thị ở kênh Discord đã đánh dấu
#          Age-Restricted (nsfw). Mặc định False cho mọi category bên dưới —
#          tự sửa lại True nếu bạn thấy chủ đề nào cần giới hạn kênh.
#
# Muốn thêm/sửa/xoá category: chỉnh trực tiếp dict bên dưới.
# Lưu ý: Discord slash command chỉ cho tối đa 25 lựa chọn (choices).

CATEGORIES = {
    "sylphiette": {"label": "Sylphiette>_<", "keyword": "sylphiette greyrat", "nsfw": False},
    "gaixinh": {"label": "Hot girl vn", "keyword": "khoe dáng", "nsfw": False},
    "gaicute": {"label": "bổ sung vitamin A 😋", "keyword": "gái xinh", "nsfw": False},
    "meo": {"label": "Mèo méo meo mèo meo", "keyword": "cute cats", "nsfw": False},
    "cho": {"label": "gâu gâu ẳng ẳng", "keyword": "cute dogs", "nsfw": False},
    "canhdep": {"label": "Cảnh đẹp thiên nhiên 🌠", "keyword": "natural scenery", "nsfw": False},
}
