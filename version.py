"""
version.py
Số bản của bot — cập nhật mỗi khi thêm tính năng/sửa lỗi đáng chú ý.
Chỉ cần sửa 2 biến bên dưới, không cần đụng vào bot.py.
"""

__version__ = "1.9.0"
__description__ = "Fix nút bấm (Trước/Sau/Bắt đầu) im lặng không phản hồi do bug ViewStore của discord.py 2.7.1 — dispatch thủ công qua on_interaction thay vì add_view(). Thêm: tải trước (prefetch) ảnh kế tiếp để bấm Sau ra ảnh ngay; nhiều từ khóa/category (/addcategory, /editcategory); /stats hiện thêm từ khóa + NSFW mỗi category."
