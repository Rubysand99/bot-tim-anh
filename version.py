"""
version.py
Số bản của bot — cập nhật mỗi khi thêm tính năng/sửa lỗi đáng chú ý.
Chỉ cần sửa 2 biến bên dưới, không cần đụng vào bot.py.
"""

__version__ = "1.7.0"
__description__ = "Thêm self-test soak: bot tự đăng ảnh vào kênh log-crawl lúc khởi động, mỗi 10s tự lấy ảnh mới (đọc thẳng MongoDB qua peek_random_image, không đánh dấu đã gửi nên không cạnh tranh ảnh với user thật) để phát hiện MongoDB chậm/lỗi bất thường theo thời gian dài."
