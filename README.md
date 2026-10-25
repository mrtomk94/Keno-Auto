# KenoAuto · California Hot Spot

Tự chạy trên máy chủ của GitHub mỗi 30 phút, không cần mở máy tính.

- **Xem báo cáo:** `https://<tên-github-của-bạn>.github.io/<tên-repo>/`
- **Dữ liệu:** nhánh `data` — `keno.db` (SQLite), `draws/YYYY-MM-DD.csv` (mỗi ngày một file), `reports/report-YYYY-MM-DD.html`
- **Chạy ngay:** tab Actions → KenoAuto → Run workflow
- **Lỗi:** GitHub gửi email khi một lần chạy thất bại; xem `debug/` và `logs/` trên nhánh `data`

Mỗi kỳ quay được chấm bằng vé chỉ dựa trên các kỳ trước đó (walk-forward), nên không engine nào được "nhìn" kết quả mình bị chấm.
