"""
version.py
Số bản của bot — cập nhật mỗi khi thêm tính năng/sửa lỗi đáng chú ý.
Chỉ cần sửa 2 biến bên dưới, không cần đụng vào bot.py.
"""

__version__ = "1.8.0"
__description__ = "Thêm _timed_defer() dùng chung, đo timing THẬT SỰ của defer() ở toàn bộ 12 lệnh gọi interaction trong bot (log mọi lần gọi, cảnh báo nếu >1s) — self-test soak trước đây chỉ đo được message.edit() thường, không đo được đúng API defer() thật, đây là lỗ hổng trong chẩn đoán trước đó."
