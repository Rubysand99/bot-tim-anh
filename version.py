"""
version.py
Số bản của bot — cập nhật mỗi khi thêm tính năng/sửa lỗi đáng chú ý.
Chỉ cần sửa 2 biến bên dưới, không cần đụng vào bot.py.
"""

__version__ = "1.0.1"
__description__ = "Sửa lỗi crash 'RuntimeError: Session is closed' khi bị Discord Rate Limit 429 lúc khởi động — không tự retry trong cùng process nữa, thoát để Render tự khởi động lại process mới."
