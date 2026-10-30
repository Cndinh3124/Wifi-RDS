#!/bin/bash
# Cài / cập nhật dịch vụ wifi-auth trên RDS. Chạy lại nhiều lần được (không ghi đè config.json đã có).
# Chạy bằng root, đứng trong thư mục wifi-auth:  bash backend/install_service.sh
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
[ "$(id -u)" = 0 ] || { echo "Hãy chạy bằng root"; exit 1; }

echo "== 1. Tài khoản hệ thống wifiauth"
id wifiauth >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin wifiauth
id wifiauth

echo "== 2. Thư mục"
install -d -o wifiauth -g wifiauth -m 750 /var/lib/wifi-auth
install -d -m 755 /opt/wifi-auth
install -d -m 750 -o root -g wifiauth /etc/wifi-auth

echo "== 3. Chương trình"
install -m 755 "$HERE/wifi_auth.py" /opt/wifi-auth/wifi_auth.py
python3 -m py_compile /opt/wifi-auth/wifi_auth.py && rm -rf /opt/wifi-auth/__pycache__

echo "== 4. Cấu hình"
if [ -f /etc/wifi-auth/config.json ]; then
  echo "   Giữ nguyên /etc/wifi-auth/config.json đã có (không ghi đè)"
else
  install -m 640 -o root -g wifiauth "$HERE/config.example.json" /etc/wifi-auth/config.json
  echo "   Đã tạo /etc/wifi-auth/config.json từ mẫu — nhớ điền google_client_id / google_client_secret sau"
fi
python3 -c "import json;json.load(open('/etc/wifi-auth/config.json'))" && echo "   config.json hợp lệ (JSON)"

echo "== 5. systemd"
install -m 644 "$ROOT/systemd/wifi-auth.service" /etc/systemd/system/wifi-auth.service
systemctl daemon-reload
systemctl enable wifi-auth >/dev/null 2>&1
systemctl restart wifi-auth
sleep 2

echo "== 6. Kiểm tra"
systemctl is-active wifi-auth
curl -s -m 3 http://127.0.0.1:8090/healthz && echo "  <- healthz"
