"""
version.py
Số bản của bot — cập nhật mỗi khi thêm tính năng/sửa lỗi đáng chú ý.
Chỉ cần sửa 2 biến bên dưới, không cần đụng vào bot.py.
"""

__version__ = "1.10.0"
__description__ = "Thêm /mergecategory (gộp ảnh + từ khóa 2 chủ đề làm 1), /random random ảnh mới mỗi lần bấm Sau (có thể khác category), /stats hiện thêm slug/NSFW rõ ràng. Fix: defer() phải chạy TRƯỚC get_all_categories_async() ở /mergecategory, /cleanup, /showcase — gọi sai thứ tự gây 'Interaction đã hết hạn' khi cache category vừa bị invalidate (lỗi thực tế xảy ra 13/09 khi dùng /mergecategory ngay sau /removecategory)."
