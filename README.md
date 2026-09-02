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
  cào ảnh Pinterest theo từng category (tĩnh trong `categories.py` + category
  admin thêm qua Discord), lưu vào MongoDB (bỏ qua ảnh trùng URL nhờ unique
  index). Gửi cảnh báo qua Discord webhook nếu toàn bộ category lỗi hoặc có
  category sắp cạn ảnh.
- **`bot.py`** — bot Discord, deploy trên Render (xem `procfile`). Lệnh `/img`
  và `!img` đọc ảnh từ MongoDB trước (nhanh, không phụ thuộc mạng ngoài mỗi lần
  bấm), chỉ fallback cào Pinterest trực tiếp khi category đó hết ảnh khả dụng
  trong DB. Có cooldown, giới hạn kênh/role (tuỳ chọn), và lệnh quản trị dành
  riêng cho admin.
- **`db.py`** — kết nối MongoDB dùng chung cho cả `crawl_job.py` và `bot.py`.
  Có thêm category tuỳ chỉnh (`custom_categories`) để admin thêm chủ đề qua
  Discord mà không cần sửa code.
- **`pinterest_crawler.py`** — logic cào Pinterest (dùng endpoint nội bộ,
  không chính thức — xem cảnh báo trong docstring của file).
- **`keep_alive.py`** — mở 1 server Flask nhỏ để giữ bot "thức" trên các nền
  tảng free-tier cần ping HTTP định kỳ, có endpoint `/health` trả JSON
  (uptime + trạng thái kết nối MongoDB).

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
| `ADMIN_USER_IDS` | `bot.py` (Render), tuỳ chọn | Danh sách Discord user ID admin, cách nhau bởi dấu phẩy. Mặc định sẵn 1 admin (`846332174734983219`) kể cả khi không set biến này. Admin bỏ qua cooldown + giới hạn kênh/role, và dùng được `/addcategory`, `/removecategory`, `/cleanup`. |
| `ALLOWED_CHANNEL_IDS` | `bot.py` (Render), tuỳ chọn | Nếu set, lệnh ảnh (`/img`, `!img`, `/random`, `!random`) chỉ dùng được ở các kênh này (ID, cách nhau bởi dấu phẩy). Để trống = không giới hạn kênh. Admin luôn bypass. |
| `ALLOWED_ROLE_IDS` | `bot.py` (Render), tuỳ chọn | Nếu set, chỉ member có 1 trong các role này mới dùng được lệnh ảnh (ID, cách nhau bởi dấu phẩy). Để trống = không giới hạn role. Admin luôn bypass. |
| `DISCORD_WEBHOOK_URL` | `crawl_job.py` (GitHub Actions Secret), tuỳ chọn | Webhook Discord để nhận cảnh báo khi crawl lỗi toàn bộ hoặc 1 category sắp cạn ảnh. Bỏ trống thì chỉ ghi log, không gửi cảnh báo. |

Tạo `MONGO_URI` ở 2 nơi riêng biệt vì đây là 2 môi trường chạy khác nhau:
- Render → Environment variables (cho `bot.py`)
- GitHub repo → Settings → Secrets and variables → Actions (cho `crawl_job.py`)

`DISCORD_WEBHOOK_URL` (tuỳ chọn): tạo trong Discord ở kênh muốn nhận cảnh báo →
Server Settings → Integrations → Webhooks → New Webhook → copy URL → thêm vào
GitHub Actions Secret (không cần set trên Render, chỉ `crawl_job.py` dùng).

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
| `/img` | Slash | Chọn chủ đề (autocomplete, gõ để tìm), lấy ảnh (có nút Trước/Sau) |
| `!img <chủ_đề>` | Prefix | Vd: `!img meo`. Gõ sai/bỏ trống sẽ liệt kê chủ đề hợp lệ |
| `/random` | Slash | Lấy ảnh từ 1 chủ đề bất kỳ (random) |
| `!random` | Prefix | Tương tự `/random` |
| `/stats` | Slash | Xem số ảnh (tổng / khả dụng ngay) trong kho theo từng chủ đề |
| `!stats` | Prefix | Tương tự `/stats` |
| `/ping` | Slash | Xem độ trễ bot |
| `!ping` | Prefix | Xem độ trễ bot |

**Cooldown:** người dùng thường bị giới hạn 8 giây/lần cho `/img`, `!img`,
`/random`, `!random`. Admin (`ADMIN_USER_IDS`) không bị giới hạn này.

### Lệnh admin (chỉ `ADMIN_USER_IDS`)

| Lệnh | Loại | Mô tả |
|---|---|---|
| `/addcategory <slug> <label> <keyword>` | Slash | Thêm chủ đề mới, có hiệu lực ngay (không cần deploy lại) |
| `!addcategory slug \| Label \| keyword` | Prefix | Tương tự, cú pháp phân cách bằng `\|` vì label/keyword có thể có khoảng trắng |
| `/removecategory <slug>` | Slash | Xoá chủ đề đã thêm qua `/addcategory` (không xoá được category tĩnh trong `categories.py`) |
| `!removecategory <slug>` | Prefix | Tương tự |
| `/cleanup <chủ_đề>` | Slash | Dọn ảnh lỗi link (404...) và ảnh đã gửi ≥ 20 lần trong 1 chủ đề |
| `!cleanup <chủ_đề>` | Prefix | Tương tự |

## Thêm/sửa chủ đề (category)

2 cách:
1. **Qua Discord (khuyên dùng, không cần deploy lại):** admin dùng
   `/addcategory` hoặc `!addcategory`. Lưu trong MongoDB (`custom_categories`),
   `crawl_job.py` sẽ tự crawl luôn category này ở lần chạy tiếp theo.
2. **Sửa code:** sửa trực tiếp dict `CATEGORIES` trong `categories.py` rồi
   deploy lại. Category kiểu này không xoá được qua `/removecategory`.

Lưu ý: Discord autocomplete giới hạn tối đa 25 gợi ý hiển thị cùng lúc (gõ để
lọc bớt nếu có nhiều hơn 25 category).

## Giám sát

- **Health check:** `GET /health` trên URL Render của bot trả JSON
  `{"status": "ok"|"degraded", "uptime_seconds": ..., "mongo": "ok"|"error: ..."}`.
  `GET /` (route gốc) vẫn giữ nguyên để phục vụ mục đích ping giữ bot thức.
- **Cảnh báo crawl job:** nếu set `DISCORD_WEBHOOK_URL`, bạn sẽ nhận tin nhắn
  Discord khi: (a) toàn bộ category crawl lỗi trong 1 lần chạy, hoặc (b) 1
  category còn dưới 5 ảnh khả dụng (sắp phải fallback cào trực tiếp liên tục).
- **Backup MongoDB:** không nằm trong code — bật ở phía MongoDB Atlas
  (Atlas → cluster → Backup) nếu cần, Atlas hỗ trợ backup tự động định kỳ.

## Deploy

- **Bot**: Render, loại **Web Service**. Start Command được set trực tiếp
  trong Render dashboard (`python3 bot.py`) — **Render không đọc `procfile`**
  (đã xác nhận thực tế), file này chỉ mang tính tài liệu/tương thích ngược,
  không ảnh hưởng gì tới runtime. `keep_alive.py` mở server Flask ở `$PORT`
  để giữ bot thức trên free-tier + phục vụ endpoint `/health`.
- **Crawl job**: GitHub Actions, tự chạy theo lịch cron trong
  `.github/workflows/main.yml` (mặc định mỗi 6 tiếng), hoặc trigger thủ công
  qua tab Actions (`workflow_dispatch`).
