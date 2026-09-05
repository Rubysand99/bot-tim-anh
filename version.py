"""
version.py
Số bản của bot — cập nhật mỗi khi thêm tính năng/sửa lỗi đáng chú ý.
Chỉ cần sửa 2 biến bên dưới, không cần đụng vào bot.py.
"""

__version__ = "1.4.1"
__description__ = "Giảm worst-case thời gian chờ fallback Pinterest từ 36s xuống ~17s (retries 3→2, timeout 10s→7s, backoff cố định 3s) — nghi ngờ là nguyên nhân các ca 'không phản hồi kịp thời' còn sót lại dù defer() đã đúng và Mongo đã nhanh."
