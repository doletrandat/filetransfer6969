# Relay — truyền tệp trực tiếp trong mạng nội bộ

**Relay** giúp gửi tệp giữa hai máy Windows hoặc giữa máy Windows và điện thoại trong cùng mạng Wi-Fi/Ethernet. Hai máy tính chạy Relay có thể tìm nhau và xác nhận từng lượt gửi; điện thoại chỉ cần trình duyệt và mã QR. Tệp đi trực tiếp qua mạng nội bộ bằng HTTPS, không cần tài khoản hay dịch vụ đám mây.

> **Phạm vi:** máy chạy Relay cần Windows và Python 3.11 trở lên. Android/iPhone dùng trình duyệt để kết nối với một máy Windows đang chạy Relay.

## Giao diện

Ba màn hình chính trên máy tính:

### Nhận tệp

Xem tên và địa chỉ IP nội bộ của máy, chấp nhận hoặc từ chối yêu cầu gửi từ máy tính khác, hiện QR cho điện thoại và mở tệp vừa nhận.

![Màn hình Nhận của Relay trên máy tính, gồm thông tin thiết bị, yêu cầu gửi và tệp nhận gần đây](docs/screenshots/desktop_01_receive.png)

### Gửi tệp

Kéo thả hoặc chọn tệp/thư mục, đánh dấu một hoặc nhiều máy nhận trong mạng rồi gửi đồng thời. Mỗi máy có tiến trình, kết quả và thao tác thử lại riêng. Danh sách thiết bị kết nối và các lượt đang thực hiện luôn hiện ở thanh bên.

Tệp đã chuẩn bị được giữ lại sau khi gửi để tiếp tục gửi sang máy khác hoặc thử lại máy bị lỗi. Chọn **Xóa tất cả** khi không còn cần chia sẻ các tệp này. Khi truy cập lần đầu, Relay tự hỏi bật thông báo nhận tệp; lựa chọn được ghi nhớ trên trình duyệt.

Tệp nhận mới được lưu vào `<Thư mục nhận>/<Tên máy gửi>/`, giữ nguyên cấu trúc thư mục bên trong. Tệp trùng tên nhưng khác nội dung được thêm hậu tố để tránh ghi đè. Các tệp đã nhận trước khi cập nhật vẫn nằm tại vị trí cũ.

![Màn hình Gửi của Relay trên máy tính, gồm vùng chọn tệp và danh sách thiết bị nhận](docs/screenshots/desktop_03_send.png)

### Lịch sử

Lọc các lượt đã gửi/đã nhận, xem trạng thái, mở tệp hoàn tất và đổi thư mục lưu tệp nhận.

![Màn hình Lịch sử của Relay trên máy tính, hiển thị các tệp đã nhận và trạng thái hoàn tất](docs/screenshots/desktop_04_history.png)

<details>
<summary><strong>Xem bố cục khi cửa sổ trình duyệt hẹp</strong></summary>

| Nhận | Gửi | Lịch sử |
| :---: | :---: | :---: |
| <img src="docs/screenshots/mobile_01_receive.png" alt="Bố cục màn Nhận trên cửa sổ hẹp" width="240"> | <img src="docs/screenshots/mobile_02_send.png" alt="Bố cục màn Gửi trên cửa sổ hẹp" width="240"> | <img src="docs/screenshots/mobile_03_history.png" alt="Bố cục màn Lịch sử trên cửa sổ hẹp" width="240"> |

Các ảnh này minh họa cách **giao diện quản lý Relay** co giãn theo chiều rộng cửa sổ. Trang kết nối dành riêng cho điện thoại có luồng gửi/tải tệp riêng.

</details>

## Cài đặt và chạy

Mở PowerShell trong thư mục dự án. Thực hiện trên **mỗi máy Windows** muốn tham gia truyền tệp:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m relay
```

Yêu cầu Python **3.11 trở lên**. Nếu dùng phiên bản khác, đổi `-3.11` tương ứng; nếu không có lệnh `py`, dùng `python -m venv .venv`. Điện thoại không cần cài Relay hay Python.

Relay tự mở `https://127.0.0.1:8765` trên máy đang chạy. Nếu cổng 8765 bận, xem địa chỉ thực tế trong PowerShell. Giữ cửa sổ PowerShell mở khi sử dụng; nhấn `Ctrl+C` để dừng. Nếu Windows Firewall hỏi, cho phép Relay trên mạng **Private**.

Chứng chỉ HTTPS được tạo trên từng máy. Trình duyệt có thể cảnh báo ở lần đầu mở; chỉ tiếp tục sau khi kiểm tra đúng địa chỉ của máy mình. Trang quản lý chỉ truy cập từ chính máy chạy Relay tại `localhost`.

Sau khi đổi Wi-Fi hoặc được cấp IP mới, khởi động lại Relay trên cả hai máy. Khi khởi động, Relay tự cấp lại chứng chỉ nếu thiếu IP hiện tại hoặc sắp hết hạn, giữ nguyên mã định danh thiết bị và thư mục nhận. Chọn lại máy nhận để gửi yêu cầu mới; trình duyệt có thể yêu cầu xác nhận chứng chỉ mới. Nếu gặp lỗi chứng chỉ không khớp IP, cần cập nhật Relay trên máy nhận nữa.

## Cách sử dụng

### Giữa hai máy Windows

1. Kết nối hai máy vào cùng mạng và chạy Relay trên cả hai.
2. Trên máy **A**, vào **Gửi**, chọn máy **B** trong danh sách thiết bị, chọn tệp/thư mục rồi bấm **Gửi**. Có thể chọn nhiều máy; mỗi máy quyết định nhận riêng.
3. Máy **B** hiện yêu cầu, ví dụ **“Máy A muốn gửi 3 tệp, tổng 25 MB”**, kèm danh sách tệp và hai nút **Nhận / Từ chối**. Hộp yêu cầu xuất hiện dù đang ở tab Nhận, Gửi hay Lịch sử.
4. Bấm **Nhận** để cấp quyền cho đúng lượt gửi đó và bắt đầu truyền. Trước khi đồng ý, chưa có dữ liệu tệp được gửi hoặc thư mục được tạo ở máy B. Bấm **Từ chối**, hoặc không trả lời trong **2 phút**, sẽ dừng lượt gửi; máy A có thể gửi yêu cầu mới. A cũng có thể **Hủy gửi** khi đang chờ.
5. Theo dõi **Tiến trình gửi** ngay trên màn **Gửi**: tệp hiện tại, máy nhận, phần trăm, dung lượng đã gửi/tổng, tốc độ, thời gian còn lại ước tính và số tệp hoàn tất. Máy gửi có thể chọn **Hủy gửi**; máy nhận có thể chọn **Hủy nhận** trong danh sách tệp đang nhận. Relay đồng bộ trạng thái **Đã hủy** sang hai đầu và xóa phần tệp chưa hoàn tất. Kết quả và nút **Thử gửi lại** khi lỗi cũng hiện tại đây; **Lịch sử** lưu các lượt gửi/nhận. Tệp nhận mặc định ở `%USERPROFILE%\Downloads\Relay`; có thể đổi tại **Lịch sử → Thư mục lưu tệp nhận**.

Relay dùng Zeroconf/mDNS để tìm máy trong mạng. Nếu không thấy máy nhận, kiểm tra hai máy cùng mạng, cấu hình mạng Windows là **Private** và quyền Firewall. Hai máy cần chạy phiên bản Relay có luồng Nhận / Từ chối; máy tính không còn dùng mã ghép nối hoặc QR.

### Giữa máy Windows và điện thoại

1. Cho máy tính và điện thoại vào cùng mạng Wi-Fi. Trên máy tính, vào **Nhận** → **Hiện mã QR**.
2. Quét QR bằng điện thoại, kiểm tra địa chỉ IP trong liên kết là của máy tính mình rồi chọn **Kết nối với Relay**. Liên kết chỉ dùng một lần và hết hạn sau **10 phút**; phiên điện thoại kéo dài tối đa **1 giờ**.
3. **Điện thoại → máy tính:** Relay tự nhận diện model từ trình duyệt khi kết nối; nếu model bị ẩn, dùng tên loại thiết bị như `iPhone` hoặc `Điện thoại Android`. Trình duyệt không cung cấp tên cá nhân đã đặt trong Cài đặt điện thoại. Trên trang điện thoại, chọn **Gửi lên máy tính**, chọn tệp và gửi. Trong lúc truyền, có thể chọn **Hủy gửi** trên điện thoại hoặc **Hủy nhận** trên máy tính; phần tệp đang gửi dở sẽ không được giữ lại. Tệp hoàn tất xuất hiện ở **Tệp nhận gần đây** và **Lịch sử**, trong thư mục theo tên thiết bị bên trong nơi lưu tệp nhận.
4. **Máy tính → điện thoại:** chọn tệp trên màn **Gửi** của máy tính. Trên điện thoại, vào **Tải về điện thoại** để mở hoặc tải tệp đã chuẩn bị. Không cần bấm nút gửi tới một máy tính khác.
5. Khi xong, chọn **Ngắt kết nối** trên máy tính để thu hồi phiên điện thoại.

Trình duyệt điện thoại quyết định nơi lưu tệp tải xuống. Nếu trang không mở được, kiểm tra hai thiết bị cùng mạng, IP trong QR, Firewall và cảnh báo chứng chỉ HTTPS. Trang điện thoại không cần mDNS.

## Cách Relay hoạt động

```mermaid
flowchart LR
    A[Máy Windows gửi] -->|HTTPS, truyền theo khối| B[Máy Windows nhận]
    A <-->|Tìm thiết bị qua mDNS, yêu cầu Nhận hoặc Từ chối| B
    C[Trình duyệt điện thoại] <-->|HTTPS, gửi hoặc tải từng tệp| A
    B -->|Kiểm tra kích thước và SHA-256| D[Thư mục nhận]
```

- **Đồng ý từng lượt:** quyền truyền gắn với đúng danh sách tệp, kích thước và SHA-256 đã được máy nhận chấp nhận. Quyền của một lượt không dùng được cho lượt khác. Yêu cầu chờ hết hạn sau 2 phút; chỉ giao diện trên máy nhận có thể chấp nhận. HTTPS kiểm tra fingerprint chứng chỉ từ thông tin khám phá thiết bị. Tên máy gửi do máy gửi cung cấp; chỉ nhận từ máy bạn nhận ra.
- **Toàn vẹn tệp:** truyền giữa hai máy Windows theo từng khối; chỉ đánh dấu hoàn tất khi kích thước và SHA-256 của tệp nhận khớp. Đường dẫn nhận được kiểm tra để không ghi ra ngoài thư mục đích.
- **Khôi phục:** Relay lưu trạng thái hàng chờ và phần tệp đã nhận. Sau khi khởi động lại, chọn thử gửi lại để tạo yêu cầu mới và được máy nhận đồng ý lần nữa. Tệp đã hoàn tất được kiểm tra để tránh truyền lại; lượt mới truyền lại các tệp chưa hoàn tất.
- **Quyền truy cập:** giao diện điều khiển chỉ mở trên `localhost`; trang điện thoại yêu cầu liên kết mời và phiên kết nối còn hiệu lực.

Mã nguồn chính nằm trong [`relay/app.py`](relay/app.py) (API và giao diện), [`relay/transfers.py`](relay/transfers.py) (hàng chờ và truyền tệp), [`relay/discovery.py`](relay/discovery.py) (tìm thiết bị) và [`relay/phone.py`](relay/phone.py) (kết nối điện thoại). Các phụ thuộc được khai báo trong [`pyproject.toml`](pyproject.toml).

## Kiểm thử

Sau khi tạo môi trường ảo, cài công cụ phát triển và chạy:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy relay tests
```

Bộ kiểm thử bao gồm đường dẫn tệp, chấp nhận/từ chối/hết hạn, giới hạn quyền từng lượt, truyền và khôi phục, API, khám phá thiết bị và luồng điện thoại. Kiểm thử tự động trên một máy không thay thế việc thử thực tế với hai thiết bị cùng mạng, đặc biệt với Firewall và mDNS.

## Giới hạn hiện tại

- Máy chạy dịch vụ Relay được hỗ trợ trên Windows; chưa có bộ cài hoặc chế độ chạy nền.
- Sau khi khởi động lại, lượt gửi cần được máy nhận đồng ý lại; tệp đang được trình duyệt đưa vào hàng chờ dở dang cần chọn lại.
- Thư mục rỗng không được truyền. Trang điện thoại chỉ gửi/tải từng tệp, chưa chuyển cả thư mục hoặc tự đồng bộ.
- Phiên điện thoại mất khi Relay khởi động lại. Chưa xác nhận thủ công toàn bộ luồng trên Android/iPhone thật.

Mã nguồn: [github.com/doletrandat/filetransfer6969](https://github.com/doletrandat/filetransfer6969).
