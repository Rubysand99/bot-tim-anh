# bot-tim-anh

Bot Discord tìm/lấy ảnh theo chủ đề, dữ liệu được crawl sẵn từ Pinterest và
lưu vào MongoDB, có fallback cào trực tiếp khi DB hết ảnh khả dụng.

## Kiến trúc

```
┌─────────────────┐   cron mỗi 2h    ┌──────────────┐
│  GitHub Actions  │ ───────────────▶ │   MongoDB    │
│  (crawl_job.py)  │   crawl & lưu    │  (Atlas)     │
└─────────────────┘                  └──────┬───────┘
                                             │ đọc random
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
  category sắp cạn ảnh. Ghi lại thời điểm crawl gần nhất để `bot.py` biết khi
  nào nên tự crawl ngay 1 category mới thêm thay vì chờ chu kỳ tiếp theo.
- **`bot.py`** — bot Discord, deploy trên Render (xem `procfile`). Lệnh `/img`
  lấy ảnh NGẪU NHIÊN trong category đã chọn từ MongoDB (không theo thứ tự),
  chỉ fallback cào Pinterest trực tiếp khi category đó hết ảnh khả dụng
  trong DB. Nút Trước/Sau trong embed hoạt động vĩnh viễn (không hết hạn, kể
  cả sau khi bot restart) vì trạng thái được lưu trong MongoDB thay vì RAM.
  Có cooldown, giới hạn kênh/role (tuỳ chọn), và lệnh quản trị dành riêng
  cho admin.
- **`db.py`** — kết nối MongoDB dùng chung cho cả `crawl_job.py` và `bot.py`.
  Có category tuỳ chỉnh (`custom_categories`), phiên xem ảnh bền vững
  (`paginator_sessions`), metadata thời điểm crawl gần nhất (`meta`).
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
| `LOG_CHANNEL_ID` | `bot.py` (Render), tuỳ chọn | ID kênh Discord nhận log lỗi tự động (mọi `logger.warning`/`logger.error` trong bot) + heartbeat ping mỗi 10 phút. Bỏ trống = tắt tính năng này, chỉ log ra Render logs như trước. |

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
| `/img` | Slash | Chọn chủ đề (autocomplete, gõ để tìm), lấy 1 ảnh NGẪU NHIÊN trong chủ đề đó (nút Trước/Sau chuyển ảnh, hoạt động vĩnh viễn) |
| `!img <chủ_đề>` | Prefix | Vd: `!img meo`. Gõ sai/bỏ trống sẽ liệt kê chủ đề hợp lệ |
| `/random` | Slash | Lấy 1 ảnh ngẫu nhiên TRÊN TOÀN BỘ KHO (mọi chủ đề), không thiên vị chủ đề ít ảnh |
| `!random` | Prefix | Tương tự `/random` |
| `/stats` | Slash | Thống kê chi tiết: tổng/khả dụng/TB số lần gửi/ảnh mới nhất mỗi chủ đề + lần crawl gần nhất |
| `!stats` | Prefix | Tương tự `/stats` |
| `/ping` | Slash | Xem độ trễ bot |
| `!ping` | Prefix | Xem độ trễ bot |

**Cooldown:** người dùng thường bị giới hạn 8 giây/lần cho `/img`, `!img`,
`/random`, `!random`. Admin (`ADMIN_USER_IDS`) không bị giới hạn này.

**Nút Trước/Sau:** không có hạn dùng — bấm được kể cả sau 1 ngày, 1 tuần,
hay sau khi bot restart, vì trạng thái phiên xem ảnh được lưu trong MongoDB
(`paginator_sessions`) thay vì bộ nhớ RAM của tiến trình bot.

### Lệnh admin (chỉ `ADMIN_USER_IDS`)

| Lệnh | Loại | Mô tả |
|---|---|---|
| `/addcategory <slug> <label> <keyword>` | Slash | Thêm chủ đề mới, có hiệu lực ngay. Tự crawl ngay chủ đề này nếu lần crawl định kỳ gần nhất đã ≥ 1 tiếng trước |
| `!addcategory slug \| Label \| keyword` | Prefix | Tương tự, cú pháp phân cách bằng `\|` vì label/keyword có thể có khoảng trắng |
| `/editcategory <slug> [label] [keyword]` | Slash | Sửa label/keyword của chủ đề đã thêm qua `/addcategory` (bỏ trống phần nào để giữ nguyên) |
| `!editcategory slug \| Label mới \| keyword mới` | Prefix | Tương tự, để trống phần nào (giữa 2 dấu `\|`) để giữ nguyên |
| `/removecategory <slug>` | Slash | Xoá chủ đề đã thêm qua `/addcategory` (không xoá được category tĩnh trong `categories.py`) |
| `!removecategory <slug>` | Prefix | Tương tự |
| `/cleanup <chủ_đề>` | Slash | Dọn ảnh lỗi link (404...) và ảnh đã gửi ≥ 20 lần trong 1 chủ đề |
| `!cleanup <chủ_đề>` | Prefix | Tương tự |
| `/showcase <chủ_đề> [kênh]` | Slash | Đăng "bảng giới thiệu" 1 chủ đề vào kênh: ảnh mẫu + tag admin + nút "🎲 Bắt đầu" cho mọi người bấm |
| `!showcase <chủ_đề> [#kênh]` | Prefix | Tương tự, kênh bỏ trống = kênh hiện tại |

### Lệnh cho mọi người

| Lệnh | Loại | Mô tả |
|---|---|---|
| `/favorites` | Slash | Xem lại các ảnh đã lưu qua nút 💾 Lưu ảnh |
| `!favorites` | Prefix | Tương tự |

## Showcase board — bảng giới thiệu chủ đề

`/showcase` tạo 1 embed cố định trong kênh (ảnh mẫu, tên chủ đề, tag admin
quản lý ở góc embed) kèm nút **"🎲 Bắt đầu"**. Ai bấm nút này sẽ nhận 1 embed
**riêng tư** (ephemeral — chỉ người bấm thấy) chứa 1 ảnh ngẫu nhiên của chủ
đề đó, kèm 3 nút:
- **◀** / **▶** — chuyển ảnh (giống `/img`, ảnh mới lấy random từ MongoDB/fallback Pinterest khi cần)
- **💾 Lưu ảnh** — lưu ảnh vào danh sách yêu thích cá nhân (xem lại bằng `/favorites`), đồng thời bot **gửi tin nhắn riêng (DM)** cho bạn kèm ảnh + ghi chú (kèm định dạng/dung lượng ảnh nếu lấy được). Nếu bạn tắt nhận DM từ thành viên server, bot vẫn lưu ảnh bình thường và báo lý do lỗi DM ngay tại kênh (chỉ bạn thấy được).

Nút "Bắt đầu" trên showcase board hoạt động vĩnh viễn giống nút Trước/Sau
(không hết hạn, không phụ thuộc bot có restart hay không) — bạn chỉ cần đăng
1 lần, để đó lâu dài trong kênh.



## Thêm/sửa chủ đề (category)

2 cách:
1. **Qua Discord (khuyên dùng, không cần deploy lại):** admin dùng
   `/addcategory` hoặc `!addcategory`. Lưu trong MongoDB (`custom_categories`).
   Nếu lần crawl định kỳ gần nhất đã ≥ 1 tiếng trước (hoặc chưa từng crawl),
   bot sẽ **tự crawl ngay** category này để có ảnh dùng luôn, không phải chờ
   tới chu kỳ crawl tiếp theo (2 tiếng). Dùng `/editcategory` để sửa lại
   label/keyword sau này nếu cần.
2. **Sửa code:** sửa trực tiếp dict `CATEGORIES` trong `categories.py` rồi
   deploy lại. Category kiểu này không sửa/xoá được qua `/editcategory` hay
   `/removecategory`.

Lưu ý: Discord autocomplete giới hạn tối đa 25 gợi ý hiển thị cùng lúc (gõ để
lọc bớt nếu có nhiều hơn 25 category).

## Giám sát

- **Health check:** `GET /health` trên URL Render của bot trả JSON
  `{"status": "ok"|"degraded", "uptime_seconds": ..., "mongo": "ok"|"error: ..."}`.
  `GET /` (route gốc) vẫn giữ nguyên để phục vụ mục đích ping giữ bot thức.
- **Cảnh báo crawl job:** nếu set `DISCORD_WEBHOOK_URL`, bạn sẽ nhận tin nhắn
  Discord khi: (a) toàn bộ category crawl lỗi trong 1 lần chạy, hoặc (b) 1
  category còn dưới 5 ảnh khả dụng (sắp phải fallback cào trực tiếp liên tục).
- **Kênh log Discord (`LOG_CHANNEL_ID`):** nếu set, mọi `logger.warning()` /
  `logger.error()` trong `bot.py` (lỗi DB, lỗi Pinterest, lỗi đồng bộ lệnh,
  ảnh chậm bất thường...) tự động được gửi vào kênh này — kèm icon 🟡
  (warning) / 🔴 (error) — ngoài việc vẫn in ra Render logs như bình thường.
  Đây là kênh riêng cho vận hành/debug, khác với `DISCORD_WEBHOOK_URL` (chỉ
  dành cho crawl job).
- **Heartbeat ping (`LOG_CHANNEL_ID`):** bot gửi 1 embed "Bot đang hoạt động"
  kèm độ trễ vào kênh log ngay lúc khởi động, rồi lặp lại mỗi 10 phút. Mỗi
  lần gửi ping mới, tin ping CŨ sẽ bị xoá trước — kênh log chỉ luôn có đúng 1
  tin ping mới nhất, không bị trôi bởi hàng loạt tin ping cũ.
- **Hiệu năng lấy ảnh:** mỗi lần `/img`/`/random` lấy ảnh, bot đo thời gian
  đọc MongoDB và thời gian fallback cào Pinterest (nếu có), tự cảnh báo (và
  gửi vào kênh log nếu bật) khi bước nào đó chậm bất thường (> 3s cho DB,
  > 15s cho fallback Pinterest) — giúp xác định chỗ nghẽn khi bot phản hồi
  chậm. Category (bao gồm cả category admin thêm qua Discord) được cache 30
  giây để giảm số lần truy vấn MongoDB lặp lại không cần thiết.
- **Backup MongoDB:** không nằm trong code — bật ở phía MongoDB Atlas
  (Atlas → cluster → Backup) nếu cần, Atlas hỗ trợ backup tự động định kỳ.

## Deploy

- **Bot**: Render, loại **Web Service**. Start Command được set trực tiếp
  trong Render dashboard (`python3 bot.py`) — **Render không đọc `procfile`**
  (đã xác nhận thực tế), file này chỉ mang tính tài liệu/tương thích ngược,
  không ảnh hưởng gì tới runtime. `keep_alive.py` mở server Flask ở `$PORT`
  để giữ bot thức trên free-tier + phục vụ endpoint `/health`.
- **Crawl job**: GitHub Actions, tự chạy theo lịch cron trong
  `.github/workflows/main.yml` (mặc định mỗi 2 tiếng), hoặc trigger thủ công
  qua tab Actions (`workflow_dispatch`). Lưu ý: GitHub Actions cron chỉ chạy
  theo giờ đồng hồ cố định, không hỗ trợ kiểu "2 tiếng sau khi lần trước
  chạy xong" — do job chạy rất nhanh (15-40 giây) nên chênh lệch giữa 2 cách
  tính là không đáng kể trong thực tế.
