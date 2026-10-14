# Hướng dẫn bảo vệ bài tập lớn Relay cho nhóm 3 người

Relay truyền tệp trực tiếp trong mạng LAN giữa hai máy Windows hoặc giữa Windows và trình duyệt điện thoại. Nhóm giữ bản đầy đủ có giao diện web, tìm thiết bị, xác nhận nhận tệp, QR điện thoại, tiến trình, hủy, thử lại và lịch sử.

**Cả 3 người phải giải thích được toàn bộ luồng.** Phân công bên dưới xác định người chuẩn bị tài liệu và dẫn phần trình bày; khi ôn, mọi người đổi phần cho nhau. Không chia theo kiểu một người chỉ biết frontend, một người chỉ biết backend.

## Cách chia code

Code được tách theo công việc thực tế, dùng hàm và lớp có tên rõ ràng. Các file xử lý chính khoảng 150–500 dòng; file dữ liệu hoặc hàm dùng chung có thể ngắn hơn. Không thêm chức năng để tăng số dòng, cũng không nén nhiều thao tác vào một dòng cho ngắn.

### Backend

| File | Việc cần giải thích |
| --- | --- |
| `relay/__main__.py` | Đọc tham số, chọn cổng, chạy HTTPS và mở trình duyệt. |
| `relay/app.py` | Tạo `AppContext`, nối các thành phần, bảo vệ API điều khiển và đăng ký các nhóm API. |
| `relay/config.py` | Thư mục nhận, mã định danh thiết bị, chứng chỉ HTTPS và fingerprint. |
| `relay/discovery.py` | Công bố và tìm thiết bị qua mDNS/Zeroconf. |
| `relay/network.py` | Kiểm tra địa chỉ mạng nội bộ và kết nối HTTPS tới thiết bị có fingerprint mong đợi. |
| `relay/staging.py` | Nhận tệp từ trình duyệt vào vùng chuẩn bị, tính kích thước và SHA-256. |
| `relay/transfers.py` | `TransferManager` nối các dịch vụ; tạo lượt gửi, chờ đồng ý, gửi từng khối, cập nhật tiến trình, hủy và thử lại. |
| `relay/receiving.py` | Tạo yêu cầu nhận, cấp quyền từng lượt, nhận khối, kiểm tra nội dung, xử lý tên trùng và hủy nhận. |
| `relay/transfer_store.py` | Lưu hàng chờ/lịch sử vào JSON và khôi phục khi khởi động lại. |
| `relay/transfer_types.py` | Dữ liệu nội bộ như `StagedItem`, `OutgoingTransfer`, `IncomingTransfer`, `ReceiveOffer` và các lỗi. |
| `relay/transfer_paths.py` | Chuẩn hóa tên, chặn đường dẫn ra ngoài thư mục nhận và tính SHA-256 của tệp trên đĩa. |
| `relay/models.py` | Kiểm tra cấu trúc dữ liệu gửi qua API bằng Pydantic. |
| `relay/phone.py` | Lời mời QR, phiên điện thoại, tên thiết bị, nhận tệp và lịch sử điện thoại. |
| `relay/routes/phone.py` | API nối giao diện điện thoại với `PhoneAccess`. |
| `relay/routes/remote.py` | API máy tính gửi dùng để trao đổi với máy tính nhận; nút đồng ý/từ chối của máy nhận cũng nằm ở đây. |
| `relay/routes/received.py` | API mở, xem trước hoặc tải tệp đã nhận. |
| `relay/file_preview.py` | Chọn kiểu nội dung và chính sách an toàn khi trình duyệt mở tệp. |

`TransferManager` giữ các danh sách dữ liệu và một khóa `RLock`. `StagingService`, `ReceivingService` và `TransferStore` dùng chung các dictionary này, không tạo bản sao. Khi một dịch vụ cập nhật tệp hoặc lượt truyền, phần còn lại nhìn thấy cùng dữ liệu. Hàm `save_state` được truyền cho các dịch vụ để chúng yêu cầu lưu sau khi thay đổi trạng thái. API vẫn gọi qua `TransferManager`, nên có một điểm rõ ràng để lần theo luồng xử lý.

`relay/security.py` và một số model ghép nối là mã của cơ chế mã ghép nối trước đây, có kiểm thử riêng. Luồng hai máy tính hiện tại dùng `ReceiveOffer` trong `receiving.py`, không gọi `PairingRegistry`. Nếu được hỏi về file này, cần phân biệt cơ chế cũ với cơ chế đồng ý từng lượt đang chạy. `relay-minimal/` là bản TCP dòng lệnh độc lập, không phải đường chạy của bản web.

### Frontend

| File | Việc cần giải thích |
| --- | --- |
| `relay/static/state.js` | Trạng thái trang, token điều khiển và các phần tử HTML cần thao tác. |
| `relay/static/ui.js` | Gọi API, định dạng dung lượng/tốc độ, đổi tab và thông báo. |
| `relay/static/render.js` | Biến trạng thái thành giao diện thiết bị, hàng chờ, yêu cầu nhận, tiến trình và lịch sử. |
| `relay/static/actions.js` | Tải trạng thái từ server, chuẩn bị tệp, bắt đầu gửi và nối/ngắt điện thoại. |
| `relay/static/app.js` | Gắn sự kiện cho nút bấm, chọn tệp, kéo thả, hủy/thử lại và cập nhật định kỳ. |
| `relay/static/phone.js` | Thao tác gửi/tải và hủy trên trang điện thoại. |
| `relay/static/phone-connect.js` | Lấy thông tin model khi xác nhận lời mời điện thoại. |
| `relay/templates/` | HTML của giao diện máy tính, điện thoại và trang xem trước. |
| `relay/static/*.css` | Bố cục, màu, trạng thái hiển thị và cách giao diện co giãn trên màn hình hẹp. |

`index.html` nạp `app.js` bằng `type="module"`. Mỗi file JavaScript dùng `import`/`export` để chỉ rõ phụ thuộc. `state.js` được dùng chung; `render.js` đọc trạng thái để vẽ, `actions.js` gọi server và cập nhật trạng thái, còn `app.js` nối thao tác người dùng với các hàm này. Không cần framework frontend hoặc bước build.

## Phân công chuẩn bị và trình bày

Thay A, B, C bằng tên thành viên. Mỗi người chuẩn bị phần dẫn khoảng 4–5 phút và tự kiểm tra một luồng demo. Đây là thời lượng gợi ý, điều chỉnh theo yêu cầu của lớp.

| Thành viên | Phần dẫn trình bày | Code đọc trước | Kết quả cần chuẩn bị |
| --- | --- | --- | --- |
| A | Khởi động, tìm thiết bị, giao diện và chuẩn bị tệp | `__main__.py`, `app.py`, `config.py`, `discovery.py`, `staging.py`; `state.js`, `ui.js`, `actions.js`, `app.js` | Sơ đồ kiến trúc; chỉ được đường đi từ chọn tệp tới `StagedItem`; demo chọn tệp/thư mục. |
| B | Truyền giữa hai máy tính, xác nhận nhận, toàn vẹn và hủy | `transfers.py`, `receiving.py`, `network.py`, `routes/remote.py`, `transfer_paths.py`, `transfer_types.py`, `models.py` | Sơ đồ gửi–nhận; demo nhận/từ chối/hủy; giải thích một khối dữ liệu từ A tới B. |
| C | Điện thoại, lịch sử, khôi phục, mở tệp và kiểm thử | `phone.py`, `routes/phone.py`, `routes/received.py`, `file_preview.py`, `transfer_store.py`; `render.js`, `phone.js`, các template và tests | Demo QR, gửi/tải điện thoại và lịch sử; bảng tình huống lỗi; giải thích khởi động lại. |

Phần B có thuật toán truyền và xử lý lỗi khó hơn, nên A và C cùng kiểm tra luồng này trước khi ôn riêng. Không đánh giá đóng góp bằng số dòng hoặc số file: phần C có kiểm thử và demo nhiều thiết bị, phần A có khởi động, mạng và liên kết giao diện.

### Quy trình ôn để cả 3 biết toàn bộ

1. **Ôn chung kiến trúc:** cả nhóm chạy ứng dụng, đọc sơ đồ và lần theo một lượt gửi hoàn chỉnh. Mỗi người tự kể lại luồng bằng lời của mình trước khi chia phần.
2. **Chuẩn bị phần dẫn:** mỗi người đọc kỹ nhóm file được giao và ghi lại đầu vào, đầu ra, bước xử lý, dữ liệu thay đổi và lỗi có thể xảy ra.
3. **Dạy chéo vòng một:** A giải thích phần mình cho B, B cho C, C cho A. Người nghe phải mở code và chỉ đúng hàm vừa được giải thích.
4. **Đổi phần vòng hai:** A trình bày phần B, B trình bày phần C, C trình bày phần A; người chuẩn bị ban đầu sửa các chỗ hiểu sai.
5. **Vấn đáp ngẫu nhiên:** chọn một hàm bất kỳ, một câu hỏi hoặc một lỗi demo. Bốc người trả lời, không để người phụ trách phần đó luôn trả lời hộ.

Chỉ coi một phần đã ôn xong khi cả 3 đều giải thích được **ai gọi, nhận gì, làm gì, trả gì và lỗi xử lý ra sao**. Nếu chưa đạt, giảm thời gian chỉnh giao diện và dành thời gian đọc lại luồng; không học thuộc từng dòng CSS.

## Luồng cả 3 phải thuộc

### Khởi động

```text
python -m relay
  → __main__.main: đọc tham số, chọn cổng, tạo ứng dụng
  → AppContext: cấu hình, định danh/chứng chỉ, điện thoại, discovery, transfers
  → discovery.start: công bố máy trong LAN
  → uvicorn: chạy HTTPS
  → index.html + app.js: mở giao diện và tải /api/v1/state
```

`0.0.0.0` là địa chỉ để server lắng nghe trên các giao diện mạng, không phải IP đưa cho máy khác kết nối. Trình duyệt điều khiển dùng `127.0.0.1`/`localhost`; máy khác dùng IP LAN và cổng dịch vụ.

### Truyền giữa hai máy tính

```text
Chọn tệp/thư mục
  → app.js bắt sự kiện → actions.uploadFiles
  → POST /api/v1/stage/chunk → TransferManager → StagingService.stage_chunk
  → lưu tệp chuẩn bị + kích thước + SHA-256

Bấm Gửi
  → actions.send → POST /api/v1/transfers
  → TransferManager.start_outgoing: tìm máy, kiểm tra HTTPS, gửi manifest
  → máy nhận: POST /api/v1/remote/transfers → ReceivingService.request_incoming
  → tạo ReceiveOffer và token, trạng thái waiting

Máy nhận bấm Nhận
  → POST /api/v1/requests/{id}/accept → decide_offer → create_incoming
  → chỉ lúc này mới chuẩn bị thư mục/tệp nhận
  → máy gửi run_outgoing thấy đã đồng ý → gửi từng khối 1 MiB
  → API chunks kiểm tra token + quyền nhận → ReceivingService.receive_chunk
  → ghi .part, kiểm tra số byte và SHA-256, chuyển thành tệp hoàn tất
  → máy gửi lấy kết quả cuối → cập nhật lịch sử và tiến trình
```

**Manifest** là danh sách thông tin tệp: mã tệp, đường dẫn tương đối, kích thước và SHA-256. Yêu cầu đồng ý gửi manifest trước; chưa gửi nội dung tệp tới máy nhận. Token gắn với lượt gửi và manifest đã chấp nhận. Máy nhận có thể từ chối; yêu cầu chưa trả lời hết hạn sau 120 giây.

Chuẩn bị tệp có hai chặng: trình duyệt → dịch vụ trên máy gửi, rồi dịch vụ máy gửi → máy nhận. JavaScript chuẩn bị khối tối đa 8 MiB; truyền giữa hai máy tính dùng khối 1 MiB. Không nhầm hai kích thước này khi giải thích bộ nhớ và tiến trình.

### Điện thoại

```text
Máy tính tạo lời mời → QR chứa IP LAN + token lời mời
  → điện thoại mở /phone?invite=... → bấm kết nối → /phone/connect
  → đổi lời mời thành cookie phiên + token cho thao tác trên trang
  → gửi tệp: /phone/api/files → PhoneAccess.receive_file
  → tải tệp đã chuẩn bị: /phone/api/files/{item_id}
  → máy tính ngắt kết nối: thu hồi phiên
```

Lời mời dùng một lần, hết hạn sau 10 phút; phiên điện thoại tối đa 1 giờ. Luồng điện thoại dùng quyền của phiên QR, khác luồng máy tính phải đồng ý từng lượt. Điện thoại không chạy Python và không cần mDNS để tìm máy; IP đã có trong QR.

### Lịch sử và khởi động lại

`TransferStore.save` ghi JSON vào file tạm rồi dùng `os.replace` để thay file trạng thái. Khi khởi động, `load` đối chiếu tệp đã lưu, kích thước, hash và mốc dữ liệu `.part`. Lượt gửi đang dở trở thành lỗi và phải thử lại, tạo yêu cầu đồng ý mới. Không tự khôi phục quyền nhận cũ. Tệp đã hoàn tất có nội dung phù hợp có thể được bỏ qua; không hứa rằng mọi lượt mới luôn tiếp tục chính xác từ byte đang dở. Phiên điện thoại không tồn tại sau khi khởi động lại.

## Câu hỏi giáo viên có thể hỏi

| Câu hỏi | Ý trả lời cần nắm |
| --- | --- |
| Vì sao dùng HTTP/HTTPS thay vì tự viết TCP? | HTTP có endpoint, header, status và thư viện gửi/nhận rõ ràng; HTTPS mã hóa đường truyền. TCP vẫn nằm ở tầng truyền tải bên dưới. Bản tối giản TCP là chương trình riêng. |
| Tìm máy và truyền tệp có dùng cùng giao thức không? | Tìm máy dùng mDNS/Zeroconf; trao đổi manifest và truyền nội dung dùng HTTPS. Thấy máy chưa có nghĩa là máy đó đã đồng ý nhận. |
| Frontend có gửi thẳng tệp sang máy nhận không? | Trong luồng máy tính, frontend đưa tệp vào staging của máy gửi; backend gửi tới backend máy nhận. |
| Vì sao cần staging? | Giữ bản tệp ổn định để tính hash, gửi lại hoặc gửi cùng tệp tới nhiều máy; đổi lại phải dùng dung lượng đĩa máy gửi. |
| Vì sao không đọc cả tệp vào RAM? | Tệp lớn có thể vượt RAM; đọc/ghi từng khối giới hạn bộ nhớ dùng cho dữ liệu mỗi lần. |
| `offset` và `final` dùng để làm gì? | `offset` cho biết vị trí khối; `final` báo khối cuối. Máy nhận vẫn kiểm tra tổng byte và hash, không tin riêng cờ `final`. |
| Gửi lại một khối có làm file bị nhân đôi không? | Nếu offset nằm trước vị trí đã nhận, receiver so dữ liệu với phần đã lưu; khớp thì trả vị trí hiện có, không nối lại khối. |
| Mất mạng giữa khối xử lý thế nào? | Receiver lùi phần đang ghi về offset trước khối và khôi phục hash trước đó. Sender thử khối tối đa 3 lần; lỗi kéo dài sẽ báo thất bại. |
| Vì sao có cả kích thước và SHA-256? | Kích thước phát hiện thiếu/thừa byte; hash phát hiện nội dung khác dù cùng kích thước. SHA-256 không phải mã hóa. |
| Có ghi đè tệp cùng tên không? | Tệp cùng nội dung có thể được dùng lại; khác nội dung được thêm hậu tố. Receiver còn kiểm tra lại nếu tệp mới xuất hiện trong lúc truyền. |
| Vì sao ghi `.part` trước? | Không để tệp chưa đủ dữ liệu xuất hiện như tệp hoàn tất. Chỉ chuyển sang tên cuối sau khi kiểm tra toàn vẹn. |
| Chặn đường dẫn như `../../...` ở đâu? | `sanitize_relative_path` kiểm tra đường dẫn tương đối; `safe_join` kiểm tra đường dẫn sau resolve vẫn nằm dưới thư mục gốc. |
| HTTPS và fingerprint bảo vệ điều gì? | HTTPS mã hóa; fingerprint đối chiếu chứng chỉ với thông tin tìm thiết bị. Thông tin mDNS/tên máy không chứng minh danh tính con người, nên chỉ nhận từ máy nhận ra trong LAN tin cậy. |
| Sao API điều khiển chỉ cho localhost? | Người trong LAN không được tùy ý đổi thư mục, chấp nhận yêu cầu hoặc mở tệp trên máy mình. API điều khiển còn kiểm tra token cho thao tác thay đổi dữ liệu. |
| Cookie điện thoại khác token điều khiển thế nào? | Cookie nhận diện phiên điện thoại; token trang điện thoại xác nhận thao tác của phiên. Token điều khiển thuộc trang quản lý trên máy tính. Token lượt gửi thuộc một yêu cầu máy tính cụ thể. |
| `async`/`await` có nghĩa là nhiều máy được xử lý song song không? | `await` cho phép nhường điều khiển khi chờ dữ liệu. Các request và tác vụ gửi có thể xen kẽ, nhưng không có nghĩa mọi câu lệnh chạy cùng lúc. |
| `BackgroundTasks` làm gì? | API tạo lượt gửi trả thông tin cho giao diện rồi chạy `run_outgoing` ở nền; giao diện theo dõi trạng thái qua những request khác. |
| Vì sao dùng `RLock`? | Nhiều request/tác vụ dùng chung các dictionary; khóa bảo vệ thay đổi trạng thái. `RLock` cho phép cùng luồng lấy lại khóa, ví dụ `decide_offer` gọi `create_incoming`. Khóa không bảo vệ toàn bộ thời gian truyền dữ liệu. |
| `dataclass` và model Pydantic khác gì? | Dataclass giữ dữ liệu nội bộ đang chạy; model Pydantic kiểm tra dữ liệu API và chuyển thành cấu trúc hợp lệ. |
| `public_dict` dùng để làm gì? | Chọn dữ liệu trả cho frontend, đổi đối tượng thành dictionary/JSON; không trả mọi chi tiết nội bộ như digest hoặc token bí mật. |
| Giao diện biết tiến trình bằng cách nào? | `refresh` lấy `/api/v1/state`, cập nhật state và gọi `renderAll`. Polling khoảng 0,5 giây khi đang truyền; 2 giây khi rảnh, 5 giây khi tab ẩn và không truyền. |
| Hủy nhận có xóa file đã hoàn tất không? | Không; hủy dừng lượt đang hoạt động và dọn phần chưa hoàn tất. Trạng thái hủy được phía gửi/nhận kiểm tra trước khi tiếp tục. |
| Khởi động lại có tự gửi tiếp không? | Không. Phải thử lại và được đồng ý lần nữa; dữ liệu đã hoàn tất được kiểm tra để tránh truyền lại. Phiên điện thoại phải kết nối mới. |
| Đã thử localhost thì có chắc hai máy thật chạy được không? | Chưa. Kiểm thử xác nhận logic và giao tiếp HTTPS trên một máy; demo LAN còn phụ thuộc Wi-Fi, Firewall, chứng chỉ và mDNS. |

Khi được chỉ vào một hàm, trả lời theo thứ tự: **mục đích → nơi gọi → đầu vào → các bước → đầu ra → tình huống lỗi**. Với hàm dài như `run_outgoing` hoặc `receive_chunk`, chia thành 4–5 bước nghiệp vụ trước rồi mới chỉ từng khối code.

## Demo và kiểm tra trước buổi bảo vệ

| Tình huống | Kết quả cần thấy |
| --- | --- |
| Gửi một tệp nhỏ và một tệp rỗng | Tệp đúng nội dung, trạng thái hoàn tất ở hai đầu. |
| Gửi thư mục có tệp con, tên tiếng Việt | Giữ cấu trúc bên trong thư mục thiết bị gửi. Thư mục rỗng không được truyền. |
| Nhận, từ chối và không trả lời | Chỉ truyền nội dung sau khi nhận; từ chối dừng; chờ quá 2 phút hết hạn. |
| Gửi đồng thời tới hai máy | Mỗi máy quyết định và có tiến trình riêng. |
| Gửi tệp trùng tên khác nội dung | Tệp cũ còn nguyên, tệp mới có hậu tố. |
| Hủy khi đang chờ hoặc đang truyền | Trạng thái hủy rõ ràng; tệp dở không xuất hiện như tệp hoàn tất. |
| Mất mạng, thử lại, khởi động lại | Lỗi được hiển thị; lượt mới phải xin đồng ý lại. |
| Quét QR và gửi/tải điện thoại | Điện thoại dùng trình duyệt; máy tính thấy tệp/lịch sử; ngắt kết nối thu hồi quyền. |
| Mở tệp đã nhận và đổi thư mục nhận | Mở được bằng ứng dụng mặc định; lượt nhận mới dùng thư mục mới. |

Chạy bộ kiểm thử từ thư mục gốc sau khi cài môi trường theo README:

```powershell
python -m pytest -q
python -m ruff check relay tests
python -m mypy relay
```

Các bài trong `tests/browser/` kiểm tra giao diện với hai hoặc nhiều dịch vụ HTTPS thật trên localhost, cần Playwright và Microsoft Edge. Nhóm vẫn nên thử ít nhất một lượt giữa hai máy Windows trong LAN và một lượt trên điện thoại thật trước khi lên lớp. Kiểm tra trước cảnh báo chứng chỉ, mạng Private và quyền Firewall; chuẩn bị sẵn vài tệp demo có kích thước khác nhau.

Mỗi người tự thực hiện được demo của cả A, B và C, tự tìm được hàm xử lý, và trả lời được các câu hỏi trong bảng. Người dẫn từng phần giúp bài trình bày liền mạch; kiến thức để bảo vệ là kiến thức chung của cả nhóm.
