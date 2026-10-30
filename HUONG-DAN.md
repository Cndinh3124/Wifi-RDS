# Đăng nhập Wi-Fi bằng Google — pfSense Captive Portal + RDS

Người dùng bấm **Đăng nhập bằng Google** → chọn hoặc xác minh tài khoản trường → tự vào mạng. Không gõ mật khẩu Wi-Fi.

```
Điện thoại (VLAN 200, gateway + DHCP = pfSense 172.16.200.2)
  (1) pfSense Captive Portal hiện trang portal.html (đã tải lên pfSense)
  (2) bấm Google ─► https://auth.truongtayuc.edu.vn/auth/google/start  (wifi-auth trên RDS, qua nginx)
                 ─► accounts.google.com ─► quay lại /auth/google/callback
  (3) wifi-auth kiểm tra tên miền (allowed_domains của vùng), tạo mật khẩu dùng 1 lần (120 giây)
      và để trình duyệt tự gửi auth_user/auth_pass về $PORTAL_ACTION$ của pfSense
  (4) pfSense ─RADIUS PAP (nội bộ)─► FreeRADIUS 172.16.203.20:1812
  (5) FreeRADIUS ─module wifi_google─► 127.0.0.1:8090 ─► đúng mật khẩu 1 lần → Access-Accept
```

Không cần Port Forwarding UDP 1812 và không cần Meraki Cloud: RADIUS đi trong mạng nội bộ.

## Thành phần trong gói

| Tệp | Đặt ở đâu |
|---|---|
| `pfsense-pages/wass/portal.html` | pfSense → Captive Portal (zone WASS) → **Portal page contents** |
| `pfsense-pages/wass/auth-error.html` | pfSense → Captive Portal (zone WASS) → **Auth error page contents** |
| `nginx-pages/wass/error.html`, `nginx-pages/penn/error.html` | RDS: `/var/www/wifi/wass/`, `/var/www/wifi/penn/` |
| `backend/wifi_auth.py` | RDS: `/opt/wifi-auth/wifi_auth.py` |
| `backend/config.example.json` | RDS: `/etc/wifi-auth/config.json` (sửa giá trị, quyền 600) |
| `systemd/wifi-auth.service` | RDS: `/etc/systemd/system/` |
| `nginx/wifi-auth.conf` | RDS: `/etc/nginx/sites-available/` |
| `freeradius/wifi_google` | RDS: `/etc/freeradius/3.0/mods-available/` |

`pfsense-pages/penn/` dùng khi có vùng (zone) riêng cho PennSchool trên VLAN của Penn.

Thứ tự bên dưới đi từ nền lên: RADIUS cục bộ chạy được trước, rồi mới tới Google. **Mỗi bước kiểm tra xong mới sang bước sau.**

---

## Bước 0 — Xác nhận đường đi hiện tại

**Ở đâu:** một máy đang nối Wi-Fi VLAN 200.

- macOS: `ipconfig getpacket en0 | grep -E "router|domain_name_server"`
- Windows: `ipconfig /all`

**Mong đợi:** Gateway = `172.16.200.2`, DNS = `172.16.200.2` (pfSense).
**Nếu Gateway là `172.16.200.1`:** người dùng đang đi qua MX, captive portal pfSense không chặn được → dừng lại và báo tôi.

**Meraki:** nếu Splash Page trên MX (VLAN 200) vẫn bật thì người dùng sẽ gặp hai lần đăng nhập. Tắt nó là một thay đổi trên mạng đang chạy, nên cần người phụ trách quyết định.

## Bước 1 — FreeRADIUS nhận yêu cầu từ pfSense

**Ở đâu:** RDS. Trong `clients.conf` đang có `pfsense = 172.16.203.15`, nhưng đây **không phải** IP nào của pfSense đã ghi nhận.
pfSense không có interface trong VLAN 203, nên gói RADIUS nhiều khả năng đi ra cổng WAN với IP nguồn `172.16.204.2`. Cách xác định chính xác:

```bash
systemctl stop freeradius
freeradius -X | grep -i "unknown client"      # để cửa sổ này mở
```

Trên pfSense: **Diagnostics → Authentication**, chọn RADIUS server `172.16.203.20`, nhập một tài khoản thử → Test.
Dòng `Ignoring request ... from unknown client <IP>` cho biết IP cần khai báo. Nhấn Ctrl+C, sửa `clients.conf`:

```
client pfsense {
	ipaddr = <IP vừa thấy>
	secret = <shared secret, giống trên pfSense>
}
```

```bash
freeradius -XC | tail -1      # mong đợi: Configuration appears to be OK
systemctl start freeradius
```

**Mong đợi:** Diagnostics → Authentication báo *User authenticated successfully*.
**Nếu không có dòng nào trong log:** gói không tới được RDS → kiểm tra định tuyến/firewall giữa pfSense WAN (VLAN 204) và VLAN 203 trên MX.

## Bước 2 — Captive Portal pfSense với tài khoản cục bộ (chưa có Google)

**Ở đâu:** pfSense → Services → Captive Portal → zone của VLAN 200 → Edit.

- Authentication Method: **Use an Authentication backend** → RADIUS server `172.16.203.20`.
- RADIUS protocol: **PAP**.
- **Reauthenticate connected users every minute: TẮT.** *Bắt buộc* — mật khẩu Google là loại dùng một lần; bật mục này sẽ làm người dùng bị đá ra sau 1 phút.
- Hard timeout: ví dụ `480` phút; Idle timeout: ví dụ `60` phút (tuỳ chính sách trường).
- Tạm thời giữ trang portal mặc định của pfSense.

**Mong đợi:** điện thoại nối Wi-Fi → trang pfSense hiện ra → đăng nhập tài khoản RADIUS cục bộ → vào Internet. Status → Captive Portal có tên người dùng.
**Nếu lỗi:** xem Status → System Logs → Authentication (pfSense) và `journalctl -u freeradius` (RDS).

## Bước 3 — Tên miền và chứng chỉ HTTPS cho wifi-auth

Google bắt buộc địa chỉ trả về là **HTTPS + tên miền** (không dùng IP).

1. **DNS nội bộ trên pfSense:** Services → DNS Resolver → Host Overrides → **Add**: Host `auth`, Domain `truongtayuc.edu.vn`, IP `172.16.203.20`, Description `wifi-auth backend (RDS)`.
   **Giữ nguyên** bản ghi `wifi.truongtayuc.edu.vn → 172.16.200.2` đang có (dành cho pfSense Captive Portal).
   Người dùng VLAN 200 lấy DNS từ pfSense nên không cần bản ghi công khai.
2. **Chứng chỉ (trên RDS)** bằng DNS-01 — cần quyền thêm bản ghi TXT ở DNS công khai của `truongtayuc.edu.vn`:
   ```bash
   apt install -y nginx certbot
   certbot certonly --manual --preferred-challenges dns -d auth.truongtayuc.edu.vn
   ```
   Thêm bản ghi `TXT _acme-challenge.auth.truongtayuc.edu.vn` theo yêu cầu, rồi bấm Enter. Lưu ý: `--manual` không tự gia hạn (90 ngày).

**Mong đợi:** có `/etc/letsencrypt/live/auth.truongtayuc.edu.vn/fullchain.pem`.
**Nếu lỗi:** `dig TXT _acme-challenge.auth.truongtayuc.edu.vn @8.8.8.8` phải thấy bản ghi trước khi bấm Enter.

## Bước 4 — OAuth Client trên Google Cloud

**Ở đâu:** https://console.cloud.google.com, đăng nhập bằng tài khoản quản trị Google Workspace của trường.

1. Tạo project, ví dụ `wifi-login`.
2. Google Auth Platform → Audience: **Internal** nếu mọi tên miền (`truongtayuc.edu.vn`, `wass.edu.vn`, `rtholdings.edu.vn`, `penn.edu.vn`) nằm trong **cùng một** Google Workspace; **External** nếu thuộc nhiều Workspace khác nhau (Internal sẽ chặn tài khoản của Workspace khác). Scope: `openid`, `email`, `profile`.
3. Clients → Create client → **Web application** → Authorized redirect URI: `https://auth.truongtayuc.edu.vn/auth/google/callback`
4. Giữ Client ID và Client secret. **Secret chỉ điền trên RDS.**

## Bước 5 — Cài wifi-auth trên RDS

```bash
cd /root/wifi-auth
bash backend/install_service.sh
```

Script tạo user `wifiauth`, chép chương trình vào `/opt/wifi-auth/`, tạo `/etc/wifi-auth/config.json` từ mẫu
(**không ghi đè** nếu đã có), cài dịch vụ systemd và kiểm tra.

**Mong đợi:** dòng cuối là `active` và `ok  <- healthz`.
Chưa có OAuth Client vẫn chạy được; điền `google_client_id`/`google_client_secret` sau (Bước 4) rồi `systemctl restart wifi-auth`.

Các khoá trong `/etc/wifi-auth/config.json`:
- `public_base_url`: `https://auth.truongtayuc.edu.vn`
- `google_client_id`, `google_client_secret`: từ Bước 4
- `pfsense_portal_hosts`: host trong địa chỉ portal pfSense, mặc định `172.16.200.2`
- `allowed_domains` của từng vùng — mặc định:
  - `wass`: `truongtayuc.edu.vn`, `wass.edu.vn`, `rtholdings.edu.vn`
  - `penn`: `penn.edu.vn`, `rtholdings.edu.vn`

**Nếu lỗi:** `journalctl -u wifi-auth -n 50 --no-pager`.

## Bước 6 — nginx

```bash
install -d /var/www/wifi/wass /var/www/wifi/penn
cp nginx-pages/wass/error.html /var/www/wifi/wass/
cp nginx-pages/penn/error.html /var/www/wifi/penn/
install -m 644 nginx/wifi-auth.conf /etc/nginx/sites-available/
ln -s /etc/nginx/sites-available/wifi-auth.conf /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

**Mong đợi:** từ máy trong VLAN 200 (đã đăng nhập portal ở Bước 2), `https://auth.truongtayuc.edu.vn/healthz` trả về `ok` và không có cảnh báo chứng chỉ.
**Nếu lỗi:** UFW đang bật thì `ufw allow 443/tcp`. Ngoài ra kiểm tra MX có cho VLAN 204 (đường ra của pfSense) tới `172.16.203.20:443` không.

## Bước 7 — Nối FreeRADIUS với wifi-auth

```bash
cd /root/wifi-auth
bash freeradius/install_wifi_google.sh
```

Script: cài `freeradius-rest`, chép module `wifi_google`, **sao lưu** `sites-available/default`,
chèn đoạn dưới đây **ngay trước dòng `files`** trong khối `authorize`, kiểm tra cấu hình (lỗi thì tự khôi phục) rồi khởi động lại FreeRADIUS.
Chạy lại nhiều lần được (không chèn trùng).

```
	if (&User-Name =~ /@/) {
		wifi_google {
			fail = 1
			invalid = 1
		}
		if (updated) {
			update control {
				&Auth-Type := PAP
			}
			return
		}
		ok
	}

	files
```

- Đặt **trước** `files` để tài khoản cục bộ dạng email (ví dụ `dinhng@truongtayuc.edu.vn`) dùng được cả mật khẩu cục bộ lẫn Google.
- `fail = 1` + `ok`: nếu wifi-auth không chạy, tài khoản cục bộ **vẫn đăng nhập được**.

**Mong đợi:** dòng `Configuration appears to be OK`, rồi `active`.
Sau đó trên pfSense **Diagnostics → Authentication** với tài khoản cục bộ phải **vẫn thành công**.
**Nếu cần quay lại:** script in sẵn lệnh khôi phục ở dòng cuối.

## Bước 8 — Trang Google trên pfSense

1. `portal.html` mặc định **chỉ có nút Google** (`var GOOGLE_ENABLED = true;`).
   - Muốn hiện thêm ô email + mật khẩu Google (qua LDAP) bên dưới: `var PASSWORD_LOGIN = true;`
   - Backend Google chưa sẵn sàng: `var GOOGLE_ENABLED = false;` → trang chỉ còn ô email + mật khẩu.
   - Nếu tên miền khác `auth.truongtayuc.edu.vn`, sửa dòng `var BACKEND = 'https://auth.truongtayuc.edu.vn';`.
2. pfSense → Captive Portal → zone VLAN 200 → Edit:
   - **Portal page contents:** tải lên `pfsense-pages/wass/portal.html`.
   - **Auth error page contents:** tải lên `pfsense-pages/wass/auth-error.html`.
3. Tab **Allowed Hostnames** (cho truy cập trước khi đăng nhập), thêm:
   `auth.truongtayuc.edu.vn`, `accounts.google.com`, `accounts.google.com.vn`, `ssl.gstatic.com`, `www.gstatic.com`,
   `fonts.gstatic.com`, `apis.google.com`, `accounts.youtube.com`
   Google có thể đổi tên miền; trang Google tải không hết thì xem trình duyệt bị chặn tên miền nào rồi thêm vào.
4. Tab **Allowed IP Addresses:** thêm `172.16.203.20`.

**Mong đợi:** điện thoại nối Wi-Fi → trang WASS → Đăng nhập bằng Google → chọn tài khoản Google của trường → “Đang kết nối…” → vào Internet.

## Bước 9 — Kiểm thử và theo dõi

| Thử với | Mong đợi |
|---|---|
| Laptop | Vào được bằng tài khoản @truongtayuc.edu.vn, @wass.edu.vn, @rtholdings.edu.vn |
| Tài khoản @penn.edu.vn trên Wi-Fi WASS | Bị chặn (chỉ dùng được trên vùng Penn) |
| Android 10+ | Như trên |
| iPhone | Có thể gặp **403 disallowed_useragent** trong cửa sổ đăng nhập nhỏ → làm theo hướng dẫn trên trang (mở Safari) |
| Gmail cá nhân | Trang “Tài khoản Google không thuộc trường” |
| Sai hoặc hết hạn | Trang lỗi pfSense, có nút Thử lại |

```bash
journalctl -u wifi-auth -f
sqlite3 /var/lib/wifi-auth/logins.db "select ts,brand,email,event from logins order by id desc limit 20;"
```

- Có `google_ok` mà **không có** `radius_accept` ngay sau → pfSense không gửi được RADIUS, hoặc FreeRADIUS chưa gọi wifi_google (xem Bước 1 và Bước 7).
- Journal báo “từ chối địa chỉ portal…” → thêm host đó vào `pfsense_portal_hosts`, rồi `systemctl restart wifi-auth`.

## Quay lại cấu hình cũ (rollback)

- **pfSense:** xoá Portal page contents → về trang mặc định. Người dùng vẫn đăng nhập được bằng tài khoản RADIUS cục bộ.
- **FreeRADIUS:** khôi phục `default.bak-<ngày>` rồi `systemctl restart freeradius`.
- **wifi-auth:** `systemctl disable --now wifi-auth`.

## Đã thử trong môi trường giả lập

FreeRADIUS 3.2.5 thật, Google và pfSense giả lập, trình duyệt Chromium:
- Đăng nhập tài khoản trường thành công.
- Mật khẩu dùng lại hoặc sai bị từ chối.
- Gmail cá nhân bị chặn, nút “Thử lại” quay về đúng portal.
- Địa chỉ portal lạ bị chặn.
- Trang lỗi pfSense hiện đúng nội dung.

**Chưa thử với pfSense và Google thật.**
