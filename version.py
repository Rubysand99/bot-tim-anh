"""
version.py
Số bản của bot — cập nhật mỗi khi thêm tính năng/sửa lỗi đáng chú ý.
Chỉ cần sửa 2 biến bên dưới, không cần đụng vào bot.py.
"""

__version__ = "1.7.1"
__description__ = "Sửa bug thật self-test vừa phát hiện: URL ảnh dài hơn 2048 ký tự khiến Discord từ chối cả embed (400 Invalid Form Body). Thêm is_valid_image_url() dùng chung — crawl_job.py chặn từ gốc trước khi lưu, bot.py tự xoá + thử ảnh khác nếu lỡ bốc trúng URL xấu đã có sẵn trong DB."
