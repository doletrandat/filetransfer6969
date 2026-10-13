# Relay — gửi file tối giản qua mạng LAN

Chương trình dòng lệnh gửi **tên file, kích thước và nội dung theo từng khối 1 MiB**
qua TCP. Máy nhận lưu file và gửi xác nhận khi đã nhận đủ dữ liệu.
Toàn bộ bản tối giản nằm trong [`relay.py`](relay.py),
chỉ dùng thư viện chuẩn Python 3.11+, không cần `pip install` để chạy từ thư mục dự án.

## Chạy trên hai máy

Hai máy cùng mạng LAN, có Python và bản mã nguồn này. Chỉ cần sao chép thư mục `relay-minimal` sang mỗi máy và mở PowerShell trong thư mục đó.

**Máy nhận:**

```powershell
python relay.py receive
```

File được lưu trong thư mục `received` tại nơi chạy lệnh. Có thể chọn nơi lưu:

```powershell
python relay.py receive --directory "D:\FileNhan"
```

Chạy `ipconfig` trên máy nhận để xem IPv4 của card Wi-Fi/Ethernet đang sử dụng.
Nếu Windows Firewall hỏi, cho phép Python truy cập mạng Private.

**Máy gửi:** thay IP ví dụ dưới đây bằng IPv4 của máy nhận:

```powershell
python relay.py send 192.168.1.10 "C:\TaiLieu\bao-cao.pdf"
```

Cổng mặc định là `8765`; để đổi cổng, thêm `--port 9000` vào lệnh trên **cả hai máy**.
Nhấn `Ctrl+C` để dừng máy nhận. Máy nhận xử lý lần lượt từng kết nối và tiếp tục chờ
file tiếp theo sau mỗi lượt.

## Phạm vi

- Một file mỗi lệnh gửi, giữ nguyên tên file, hỗ trợ tên tiếng Việt và file rỗng.
- Đọc/ghi từng khối, không giữ toàn bộ file trong RAM.
- Từ chối ghi đè file trùng tên; cần đổi tên hoặc chuyển file cũ trước khi gửi lại.
- Nếu nhận thiếu hoặc kết nối lỗi, xóa file nhận dở; hết 60 giây không có dữ liệu thì ngắt.
- Đây là TCP thuần, nhận tự động, không mã hóa hay xác thực; dùng giữa các máy tin cậy
  trong mạng LAN. Không có giao diện web, tự tìm máy, truyền thư mục hay khôi phục lượt gửi.
- Kiểm tra đủ số byte; không có bước đối chiếu SHA-256 trong giao thức.

## Kiểm tra bản tối giản

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_minimal_transfer.py -q
```
