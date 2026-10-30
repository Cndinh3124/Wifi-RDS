# Wifi-RDS — Captive Portal Wi-Fi cho trường (pfSense + FreeRADIUS + Google)

Đăng nhập Wi-Fi cho học sinh, giáo viên và nhân viên bằng **tài khoản Google của trường**.

```
Thiết bị (VLAN 200, DHCP/gateway = pfSense 172.16.200.2)
  └─► pfSense Captive Portal (zone wifirds) — trang portal.html
        ├─ Email trường + mật khẩu Google ─► RADIUS ─► FreeRADIUS (RDS 172.16.203.20) ─► Google Secure LDAP
        └─ Nút "Đăng nhập bằng Google" (tuỳ chọn) ─► wifi-auth (RDS) ─► Google OAuth
                                                     └─ mật khẩu dùng 1 lần ─► RADIUS ─► module wifi_google
```

## Thành phần

| Thư mục | Nội dung |
|---|---|
| `pfsense-pages/` | Trang đăng nhập (`portal.html`) và trang lỗi (`auth-error.html`) tải lên pfSense, cho WASS và PennSchool |
| `backend/` | Dịch vụ `wifi-auth` (Python 3, chỉ thư viện chuẩn) + script cài + cấu hình mẫu |
| `freeradius/` | Module `rest` `wifi_google` + script nối vào FreeRADIUS (có sao lưu, tự khôi phục khi lỗi) |
| `nginx/`, `nginx-pages/` | Reverse proxy HTTPS cho `wifi-auth` và trang lỗi của backend |
| `systemd/` | Unit `wifi-auth.service` |
| `HUONG-DAN.md` | Hướng dẫn triển khai từng bước (tiếng Việt) |

## Trạng thái (30/10/2026)

- Captive Portal pfSense (zone `wifirds`) chạy, xác thực qua FreeRADIUS → Google Secure LDAP.
- `wifi-auth` và module `wifi_google` đã cài trên RDS; nút Google **đang tắt** (`GOOGLE_ENABLED = false` trong `portal.html`)
  cho tới khi có HTTPS (`wifi.truongtayuc.edu.vn`) và OAuth Client trên Google Cloud.

## Bảo mật

- **Không commit** `/etc/wifi-auth/config.json` thật (có `google_client_secret`) hay shared secret RADIUS.
  Chỉ `config.example.json` (giá trị mẫu) nằm trong repo; `.gitignore` chặn `config.json`.
- Mật khẩu đăng nhập Google là loại dùng một lần (120 giây), chỉ gửi tới host portal nằm trong danh sách cho phép.
