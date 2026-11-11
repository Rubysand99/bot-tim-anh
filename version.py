"""
version.py
Số bản của bot — cập nhật mỗi khi thêm tính năng/sửa lỗi đáng chú ý.
Chỉ cần sửa 2 biến bên dưới, không cần đụng vào bot.py.
"""

__version__ = "1.11.1"
__description__ = "Sửa lỗi: không còn cache danh sách chủ đề rỗng khi MongoDB chập chờn; crawl_job báo lỗi thay vì im lặng khi không đọc được chủ đề; /cleanup chỉ xoá ảnh 404/410 (không xoá nhầm do lỗi mạng) và kiểm tra mẫu ngẫu nhiên; /config clear_* giờ thật sự bỏ được giới hạn kể cả khi có biến môi trường ALLOWED_*."
