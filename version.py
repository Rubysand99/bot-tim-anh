"""
version.py
Số bản của bot — cập nhật mỗi khi thêm tính năng/sửa lỗi đáng chú ý.
Chỉ cần sửa 2 biến bên dưới, không cần đụng vào bot.py.
"""

__version__ = "1.3.2"
__description__ = "Sửa cùng lỗi defer() gọi muộn ở 2 chỗ khác: nút Trước/Sau (_paginator_navigate) và nút Lưu ảnh — cả 2 đều gọi DB trước khi defer(), có thể gây 'không phản hồi kịp thời' nếu Mongo chậm."
