"""Cấu hình mặc định dùng chung cho giao thức, máy gửi và máy nhận."""

CHUNK = 1024 * 1024
PROTOCOL = "relay-lan-v1"
TCP_PORT, UDP_PORT = 8765, 8766
TIMEOUT = 60
CONSENT_TIMEOUT = 120
