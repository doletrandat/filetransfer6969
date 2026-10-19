"""Cấu hình mặc định dùng chung cho giao thức, máy gửi và máy nhận."""

CHUNK = 1024 * 1024  # Đọc/gửi từng khối tối đa 1 MiB, tránh nạp cả file vào RAM.
PROTOCOL = "relay-lan-v1"  # Hai đầu phải dùng cùng mã giao thức.
TCP_PORT, UDP_PORT = 8765, 8766  # TCP truyền file; UDP tìm máy nhận.
TIMEOUT = 60  # Giới hạn chờ mỗi thao tác socket khi truyền dữ liệu, tính bằng giây.
CONSENT_TIMEOUT = 120  # Thời gian dành cho người nhận trả lời y/N, tính bằng giây.
