"""
version.py
Số bản của bot — cập nhật mỗi khi thêm tính năng/sửa lỗi đáng chú ý.
Chỉ cần sửa 2 biến bên dưới, không cần đụng vào bot.py.
"""

__version__ = "1.11.1"
__description__ = "Sửa lỗi: không còn cache danh sách chủ đề rỗng khi MongoDB chập chờn; crawl_job báo lỗi thay vì im lặng khi không đọc được chủ đề; /cleanup chỉ xoá ảnh 404/410 và kiểm tra mẫu ngẫu nhiên; /config clear_* thật sự bỏ được giới hạn kể cả khi có biến môi trường ALLOWED_*; lấy ảnh nguyên tử (2 người không còn nhận trùng 1 ảnh); workflow crawl không còn chạy mỗi lần push và crawler nghỉ 1–3s giữa các request Pinterest."
