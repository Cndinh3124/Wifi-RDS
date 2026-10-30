#!/bin/bash
# Nối FreeRADIUS với dịch vụ wifi-auth (module rest "wifi_google").
# - Sao lưu sites-available/default trước khi sửa
# - Chèn đoạn wifi_google NGAY TRƯỚC dòng "files" trong khối authorize
# - Kiểm tra cấu hình; nếu lỗi thì tự khôi phục bản sao lưu
# Chạy bằng root, đứng trong thư mục wifi-auth:  bash freeradius/install_wifi_google.sh
set -e
RAD=/etc/freeradius/3.0
SITE=$RAD/sites-available/default
HERE="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" = 0 ] || { echo "Hãy chạy bằng root"; exit 1; }
[ -f "$SITE" ] || { echo "Không thấy $SITE"; exit 1; }

echo "== 1. Cài gói freeradius-rest (nếu chưa có)"
dpkg -s freeradius-rest >/dev/null 2>&1 || apt-get install -y freeradius-rest

echo "== 2. Chép module wifi_google"
install -m 640 -o root -g freerad "$HERE/wifi_google" $RAD/mods-available/wifi_google
ln -sfn ../mods-available/wifi_google $RAD/mods-enabled/wifi_google

BAK="$SITE.bak-$(date +%F-%H%M%S)"
cp -p "$SITE" "$BAK"
echo "== 3. Đã sao lưu: $BAK"

if grep -q "wifi_google" "$SITE"; then
  echo "== 4. File default đã có đoạn wifi_google -> giữ nguyên, không chèn lại"
else
  python3 - "$SITE" <<'PY'
import re, sys
p = sys.argv[1]
t = open(p).read()
m = re.search(r'^authorize \{', t, re.M)
if not m:
    sys.exit("Không tìm thấy khối 'authorize {'")
f = re.compile(r'^\tfiles[ \t]*$', re.M).search(t, m.end())
if not f:
    sys.exit("Không tìm thấy dòng 'files' trong khối authorize")
block = """\t#  Wi-Fi Google: tên dạng email -> hỏi wifi-auth trước.
\t#  Khớp mật khẩu dùng một lần (updated) -> xác thực PAP ngay, bỏ qua tài khoản cục bộ.
\t#  Không khớp (notfound) hoặc wifi-auth không chạy (fail) -> đi tiếp xuống "files" (tài khoản cục bộ vẫn dùng được).
\tif (&User-Name =~ /@/) {
\t\twifi_google {
\t\t\tfail = 1
\t\t\tinvalid = 1
\t\t}
\t\tif (updated) {
\t\t\tupdate control {
\t\t\t\t&Auth-Type := PAP
\t\t\t}
\t\t\treturn
\t\t}
\t\t#  wifi-auth không chạy / không khớp: không để kết quả "fail" chặn tài khoản cục bộ
\t\tok
\t}

"""
t = t[:f.start()] + block + t[f.start():]
open(p, 'w').write(t)
print("== 4. Đã chèn đoạn wifi_google trước dòng 'files'")
PY
fi

echo "== 5. Kiểm tra cấu hình"
if freeradius -XC 2>&1 | tail -1 | grep -q "Configuration appears to be OK"; then
  echo "   Configuration appears to be OK"
else
  echo "   CẤU HÌNH LỖI -> khôi phục bản sao lưu"
  freeradius -XC 2>&1 | grep -iE "error|failed" | head -5
  cp -p "$BAK" "$SITE"
  rm -f $RAD/mods-enabled/wifi_google
  exit 1
fi

echo "== 6. Khởi động lại FreeRADIUS"
systemctl restart freeradius
sleep 1
systemctl is-active freeradius
echo "Xong. Nếu cần quay lại: cp -p $BAK $SITE && rm $RAD/mods-enabled/wifi_google && systemctl restart freeradius"
