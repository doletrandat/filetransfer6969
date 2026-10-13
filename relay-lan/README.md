# Relay LAN — tự tìm máy và gửi nhiều file

Bản dòng lệnh độc lập, chỉ dùng thư viện chuẩn **Python 3.11+**.
Sao chép toàn bộ thư mục `relay-lan` sang hai máy, mở PowerShell trong thư mục này.
Không cần cài thư viện để chạy chương trình.

## 1. Máy nhận

```powershell
python relay.py receive --name "May cua Dat"
```

Máy xuất hiện trong danh sách tìm kiếm khi đang chạy chế độ nhận. File được lưu
vào thư mục `received` tại nơi chạy lệnh. Đổi thư mục lưu bằng:

```powershell
python relay.py receive --directory "D:\FileNhan" --name "May cua Dat"
```

## 2. Máy gửi

```powershell
python relay.py send "C:\TaiLieu\bao-cao.pdf" "C:\Anh\anh.jpg"
```

Chương trình tìm máy trong khoảng 3 giây, hiển thị tên/IP/cổng. Nhập số máy muốn gửi.
Trên máy nhận, xem tên máy gửi, IP, danh sách file và tổng dung lượng rồi nhập **y**
để đồng ý. Nhập khác `y` để từ chối. Yêu cầu hết hạn sau 120 giây; máy nhận tiếp tục
chờ lượt khác. Chỉ sau khi đồng ý, chương trình mới gửi nội dung và tạo file nhận.

Hai đầu hiển thị phần trăm, số byte và file thứ mấy trong lượt gửi. Máy gửi chỉ báo
hoàn tất sau khi máy nhận đã lưu và xác nhận từng file.

Chỉ xem danh sách máy:

```powershell
python relay.py discover
```

Nếu không tìm thấy máy, lấy IPv4 của máy nhận bằng `ipconfig` và nhập trực tiếp:

```powershell
python relay.py send "C:\TaiLieu\bao-cao.pdf" --host 192.168.1.10
```

## Kết nối và giới hạn

- Hai máy cùng LAN/Wi-Fi, cho phép Python qua Windows Firewall trên mạng Private.
- Tìm máy bằng **UDP broadcast cổng 8766**; truyền file bằng **TCP cổng 8765**.
  Wi-Fi khách, mạng chặn broadcast hoặc cách ly thiết bị có thể khiến tìm máy thất bại.
- Có thể đổi bằng `--discovery-port 9001` trên cả hai máy và `--port 9000` ở máy nhận.
  Khi tìm tự động, máy gửi dùng cổng TCP được thông báo. Khi dùng `--host`, thêm
  `--port 9000` ở máy gửi nếu máy nhận đổi cổng. Một máy nhận cho mỗi cổng UDP.
- Một lượt tối đa 1000 file và thông tin tên/kích thước tối đa 64 KiB;
  tên file tối đa 240 byte UTF-8. Truyền từng khối 1 MiB, không nạp cả file vào RAM.
- Không gửi thư mục, không kiểm tra SHA-256. Có kiểm tra nhận đủ số byte.
- Tên trùng tự đổi thành `bao-cao (1).pdf`, không ghi đè file cũ.
- Timeout dữ liệu 60 giây. Mất kết nối hoặc hủy sẽ xóa file đang nhận dở;
  file đã hoàn tất trước đó vẫn được giữ. Không có tiếp tục từ byte bị ngắt.
- Nhấn `Ctrl+C` để dừng. Nếu dừng máy nhận, cả dịch vụ tìm máy cũng dừng.
- Một lượt nhận tại một thời điểm. Máy vẫn trả lời tìm kiếm trong lúc đang truyền;
  nên đợi lượt hiện tại hoàn tất trước khi gửi lượt khác.
- TCP thuần, không mã hóa/xác thực danh tính; tên máy do phía gửi cung cấp.
  Phù hợp giữa các máy tin cậy trong LAN. Hai đầu cần chạy bản `relay-lan` này;
  giao thức khác với bản `relay-minimal` và bản Relay web.

## Kiểm thử

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
python -m mypy relay.py tests
```

Kiểm thử sử dụng UDP/TCP thực qua loopback, kiểm tra nội dung file ở hai đầu và
các trường hợp lỗi. Kết quả trên một máy không thay thế thử hai máy LAN thật,
đặc biệt với broadcast và Firewall.
