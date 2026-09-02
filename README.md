# bot-tim-anh

Bot Discord tìm/lấy ảnh theo chủ đề, dữ liệu được crawl sẵn từ Pinterest và
lưu vào MongoDB, có fallback cào trực tiếp khi DB hết ảnh khả dụng.

## Kiến trúc

```
┌─────────────────┐   cron mỗi 6h    ┌──────────────┐
│  GitHub Actions  │ ───────────────▶ │   MongoDB    │
│  (crawl_job.py)  │   crawl & lưu    │  (Atlas)     │
└─────────────────┘                  └──────┬───────┘
                                             │ đọc trước
                                             ▼
                                      ┌──────────────┐      hết ảnh khả dụng
                                      │   bot.py     │ ────────────────────▶ cào trực
                                      │  (Render)    │                       tiếp Pinterest
                                      └──────────────┘
```

- **`crawl_job.py`** — chạy định kỳ qua GitHub Actions (`.github/workflows/main.yml`),
  cào ảnh Pinterest theo từng category trong `categories.py`, lưu vào MongoDB
  (bỏ qua ảnh trùng URL nhờ unique index).
- **`bot.py`** — bot Discord, deploy trên Render (xem `procfile`). Lệnh `/img`
  và `!img` đọc ảnh từ MongoDB trước (nhanh, không phụ thuộc mạng ngoài mỗi lần
  bấm), chỉ fallback cào Pinterest trực tiếp khi category đó hết ảnh khả dụng
  trong DB.
- **`db.py`** — kết nối MongoDB dùng chung cho cả `crawl_job.py` và `bot.py`.
- **`pinterest_crawler.py`** — logic cào Pinterest (dùng endpoint nội bộ,
  không chính thức — xem cảnh báo trong docstring của file).
- **`keep_alive.py`** — mở 1 server Flask nhỏ để giữ bot "thức" trên các nền
  tảng free-tier cần ping HTTP định kỳ.

## Cài đặt local

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Biến môi trường

| Biến | Bắt buộc ở | Mô tả |
|---|---|---|
| `DISCORD_TOKEN` | `bot.py` (Render) | Token bot Discord |
| `MONGO_URI` | `bot.py` (Render) **và** GitHub Actions Secret | Connection string MongoDB Atlas |
| `PORT` | `keep_alive.py` (Render tự cấp) | Port cho server Flask giữ bot thức |

Tạo `MONGO_URI` ở 2 nơi riêng biệt vì đây là 2 môi trường chạy khác nhau:
- Render → Environment variables (cho `bot.py`)
- GitHub repo → Settings → Secrets and variables → Actions (cho `crawl_job.py`)

## Chạy crawl job thủ công (test local qua Termux)

```bash
MONGO_URI="mongodb+srv://..." python crawl_job.py
```

## Chạy bot local

```bash
DISCORD_TOKEN="..." MONGO_URI="mongodb+srv://..." python bot.py
```

## Danh sách lệnh Discord

| Lệnh | Loại | Mô tả |
|---|---|---|
| `/img` | Slash | Chọn chủ đề từ dropdown, lấy ảnh (có nút Trước/Sau) |
| `!img <chủ_đề>` | Prefix | Vd: `!img meo`. Gõ sai/bỏ trống sẽ liệt kê chủ đề hợp lệ |
| `/random` | Slash | Lấy ảnh từ 1 chủ đề bất kỳ (random) |
| `!random` | Prefix | Tương tự `/random` |
| `/stats` | Slash | Xem số ảnh còn lại trong kho (MongoDB) theo từng chủ đề |
| `!stats` | Prefix | Tương tự `/stats` |
| `/ping` | Slash | Xem độ trễ bot |
| `!ping` | Prefix | Xem độ trễ bot |

## Thêm/sửa chủ đề (category)

Sửa trực tiếp dict `CATEGORIES` trong `categories.py`. Lưu ý: Discord slash
command chỉ cho tối đa 25 lựa chọn (choices) trong dropdown.

## Deploy

- **Bot**: Render (xem `procfile`). File hiện khai kiểu service **Worker**,
  còn `keep_alive.py` lại mở 1 server Flask lắng nghe `$PORT` — mục đích
  thường là để giữ bot thức trên free-tier. **Lưu ý:** 2 khai báo này có thể
  không khớp nhau tuỳ loại service bạn thực sự chọn khi tạo trên Render
  (Worker không nhận traffic HTTP, nên `keep_alive` có thể không phát huy tác
  dụng). Đây là điểm cần rà soát riêng, chưa xử lý trong lần dọn dẹp này.
- **Crawl job**: GitHub Actions, tự chạy theo lịch cron trong
  `.github/workflows/main.yml` (mặc định mỗi 6 tiếng), hoặc trigger thủ công
  qua tab Actions (`workflow_dispatch`).
