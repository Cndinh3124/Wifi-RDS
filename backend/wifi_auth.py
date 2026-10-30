#!/usr/bin/env python3
"""
wifi_auth.py — Đăng nhập Wi-Fi bằng Google cho captive portal dùng RADIUS
(pfSense Captive Portal — mặc định; hoặc Meraki Splash "Sign-on with my RADIUS server").

Luồng (pfSense):
  1. pfSense hiện trang portal (tải lên pfSense). Người dùng bấm "Đăng nhập bằng Google" ->
     GET /auth/google/start?brand=wass&portal=pfsense&action=<$PORTAL_ACTION$>&zone=<$PORTAL_ZONE$>&redirurl=<...>
  2. Google xác thực -> GET /auth/google/callback?code=...&state=...
     Dịch vụ kiểm tra tên miền, tạo mật khẩu RADIUS dùng một lần (TTL ngắn) và tự gửi (auto-POST)
     auth_user/auth_pass về $PORTAL_ACTION$ của pfSense. Người dùng không phải gõ gì.
  3. pfSense gửi Access-Request (PAP) tới FreeRADIUS; module rest "wifi_google" gọi
     POST /radius/authorize trên 127.0.0.1 để kiểm tra mật khẩu dùng một lần.
  (Meraki: thay action/zone/redirurl bằng login_url/continue_url.)

Chỉ dùng thư viện chuẩn của Python 3 (không cần pip). Chạy sau nginx (nginx lo TLS).
"""
import base64, hashlib, html, json, logging, os, secrets, sqlite3, sys, threading, time
import urllib.parse, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG_PATH = os.environ.get("WIFI_AUTH_CONFIG", "/etc/wifi-auth/config.json")
log = logging.getLogger("wifi-auth")


def load_config(path):
    with open(path, encoding="utf-8") as f:
        c = json.load(f)
    c.setdefault("listen_host", "127.0.0.1")
    c.setdefault("listen_port", 8090)
    c.setdefault("db_path", "/var/lib/wifi-auth/logins.db")
    c.setdefault("token_ttl_seconds", 120)
    c.setdefault("state_ttl_seconds", 600)
    c.setdefault("meraki_login_hosts", ["network-auth.com"])
    c.setdefault("pfsense_portal_hosts", ["172.16.200.2"])
    c.setdefault("google_auth_url", "https://accounts.google.com/o/oauth2/v2/auth")
    c.setdefault("google_token_url", "https://oauth2.googleapis.com/token")
    if not c.get("brands"):
        raise SystemExit("config.json thiếu khóa bắt buộc: brands")
    # Mỗi trường (brand) có thể có địa chỉ backend + OAuth Client riêng; không khai báo thì dùng giá trị chung.
    for name, bc in c["brands"].items():
        for k in ("public_base_url", "google_client_id", "google_client_secret"):
            v = bc.get(k) or c.get(k)
            if not v:
                raise SystemExit(f"config.json: trường '{name}' thiếu {k} (khai báo trong brands.{name} hoặc ở mức chung)")
            bc[k] = v.rstrip("/") if k == "public_base_url" else v
    return c


CFG = None
LOCK = threading.Lock()
STATES = {}   # state -> dict(brand, portal, nonce, exp)
TOKENS = {}   # username(email) -> dict(pw_hash, exp, brand)


# ---------------------------------------------------------------- tiện ích
def now():
    return time.time()


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def purge():
    t = now()
    with LOCK:
        for d in (STATES, TOKENS):
            for k in [k for k, v in d.items() if v["exp"] < t]:
                d.pop(k, None)


def db():
    con = sqlite3.connect(CFG["db_path"], timeout=5)
    con.execute("""CREATE TABLE IF NOT EXISTS logins(
        id INTEGER PRIMARY KEY, ts TEXT DEFAULT (datetime('now','localtime')),
        brand TEXT, email TEXT, name TEXT, domain TEXT, client_ip TEXT, event TEXT)""")
    return con


def record(brand, email, name, domain, ip, event):
    try:
        with db() as con:
            con.execute("INSERT INTO logins(brand,email,name,domain,client_ip,event) VALUES(?,?,?,?,?,?)",
                        (brand, email, name, domain, ip, event))
    except Exception as e:  # ghi log không được thì vẫn cho đăng nhập
        log.warning("không ghi được DB: %s", e)


def host_ok(u, hosts, schemes):
    """Chỉ cho phép gửi mật khẩu tới đúng cổng captive portal (chống gửi tới host lạ)."""
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return False
    if p.scheme not in schemes or not p.hostname:
        return False
    h = p.hostname.lower()
    return any(h == d or h.endswith("." + d) for d in hosts)


def portal_from_query(q):
    """Trả về dict mô tả nơi gửi đăng nhập, hoặc None nếu không hợp lệ."""
    if q.get("portal", "pfsense") == "pfsense" and q.get("action"):
        action = q["action"]
        if not host_ok(action, CFG["pfsense_portal_hosts"], ("http", "https")):
            return None
        return dict(type="pfsense", action=action, user_field="auth_user", pass_field="auth_pass",
                    fields={"zone": q.get("zone", ""), "redirurl": q.get("redirurl", ""), "accept": "Login"},
                    retry=action)
    if q.get("login_url"):
        lu = q["login_url"]
        if not host_ok(lu, CFG["meraki_login_hosts"], ("https",)):
            return None
        return dict(type="meraki", action=lu, user_field="username", pass_field="password",
                    fields={"success_url": q.get("continue_url", "")},
                    retry="", login_url=lu, continue_url=q.get("continue_url", ""))
    return None


def b64url_json(seg):
    seg += "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg))


def email_allowed(brand_cfg, claims):
    """Trả về (ok, lý_do).

    Tài khoản trường hợp lệ khi:
      - email đã xác minh và có đuôi nằm trong allowed_domains, VÀ
      - có claim "hd" (tức là tài khoản Google Workspace do tổ chức quản lý, không phải tài khoản
        Google cá nhân tự đăng ký bằng địa chỉ email đó).
    Không bắt hd trùng đuôi email, để tên miền phụ (secondary domain) của Workspace vẫn đăng nhập được
    (với tên miền phụ, hd là tên miền chính của tổ chức).
    """
    if not claims.get("email") or not claims.get("email_verified"):
        return False, "email_unverified"
    domain = claims["email"].rsplit("@", 1)[-1].lower()
    allowed = [d.lower() for d in brand_cfg.get("allowed_domains", [])]
    hd = (claims.get("hd") or "").lower()
    allowed_hd = [d.lower() for d in brand_cfg.get("allowed_hd", [])]   # tuỳ chọn: siết thêm theo tổ chức
    if domain in allowed and hd and (not allowed_hd or hd in allowed_hd):
        return True, ""
    if brand_cfg.get("allow_any_google_account"):
        return True, ""
    return False, "domain_not_allowed"


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    server_version = "wifi-auth/1.0"

    def log_message(self, fmt, *a):
        log.info("%s %s", self.client_ip(), fmt % a)

    def client_ip(self):
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def send(self, code, body=b"", ctype="text/html; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, url):
        self.send(302, "", headers={"Location": url})

    def error_page(self, brand, code, portal=None):
        q = {"code": code}
        if portal:
            if portal.get("retry"):
                q["retry"] = portal["retry"]
            if portal.get("login_url"):
                q["login_url"] = portal["login_url"]
                q["continue_url"] = portal.get("continue_url", "")
        b = brand if brand in CFG["brands"] else next(iter(CFG["brands"]))
        self.redirect(f"/{b}/error.html?" + urllib.parse.urlencode(q))

    # ---------------- GET
    def do_GET(self):
        purge()
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/healthz":
            return self.send(200, "ok", "text/plain")
        if u.path == "/auth/google/start":
            return self.start(q)
        if u.path == "/auth/google/callback":
            return self.callback(q)
        self.send(404, "not found", "text/plain")

    def start(self, q):
        brand = q.get("brand", "")
        if brand not in CFG["brands"]:
            return self.send(400, "brand không hợp lệ", "text/plain; charset=utf-8")
        portal = portal_from_query(q)
        if not portal:
            log.warning("từ chối địa chỉ portal không nằm trong danh sách cho phép: %r",
                        q.get("action") or q.get("login_url"))
            return self.error_page(brand, "network")
        bc = CFG["brands"][brand]
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(16)
        with LOCK:
            STATES[state] = dict(brand=brand, portal=portal, nonce=nonce,
                                 exp=now() + CFG["state_ttl_seconds"])
        params = {
            "client_id": bc["google_client_id"],
            "redirect_uri": bc["public_base_url"] + "/auth/google/callback",
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "prompt": "select_account",
        }
        if bc.get("hd_hint"):
            params["hd"] = bc["hd_hint"]    # tuỳ chọn, chỉ là gợi ý cho Google; vẫn kiểm tra lại ở callback
        self.redirect(CFG["google_auth_url"] + "?" + urllib.parse.urlencode(params))

    def callback(self, q):
        with LOCK:
            st = STATES.pop(q.get("state", ""), None)
        if not st:
            return self.error_page("", "expired")
        brand, portal = st["brand"], st["portal"]
        bc = CFG["brands"][brand]
        if q.get("error"):
            return self.error_page(brand, "google_cancel", portal)
        try:
            data = urllib.parse.urlencode({
                "code": q.get("code", ""),
                "client_id": bc["google_client_id"],
                "client_secret": bc["google_client_secret"],
                "redirect_uri": bc["public_base_url"] + "/auth/google/callback",
                "grant_type": "authorization_code",
            }).encode()
            req = urllib.request.Request(CFG["google_token_url"], data=data,
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=10) as r:
                tok = json.load(r)
            # id_token nhận trực tiếp từ Google qua HTTPS bằng client_secret nên tin được nội dung,
            # nhưng vẫn kiểm tra aud/iss/exp/nonce.
            claims = b64url_json(tok["id_token"].split(".")[1])
        except Exception as e:
            log.error("đổi code với Google thất bại: %s", e)
            return self.error_page(brand, "server", portal)

        if (claims.get("aud") != bc["google_client_id"]
                or claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com")
                or float(claims.get("exp", 0)) < now()
                or claims.get("nonce") != st["nonce"]):
            log.error("id_token không hợp lệ: aud/iss/exp/nonce")
            return self.error_page(brand, "server", portal)

        email = claims["email"].lower() if claims.get("email") else ""
        ok, why = email_allowed(CFG["brands"][brand], claims)
        domain = email.rsplit("@", 1)[-1] if email else ""
        if not ok:
            record(brand, email, claims.get("name", ""), domain, self.client_ip(), "denied:" + why)
            return self.error_page(brand, why, portal)

        pw = secrets.token_urlsafe(24)
        with LOCK:
            TOKENS[email] = dict(pw_hash=sha(pw), exp=now() + CFG["token_ttl_seconds"], brand=brand)
        record(brand, email, claims.get("name", ""), domain, self.client_ip(), "google_ok")
        log.info("Google OK %s (%s) -> cấp mật khẩu dùng một lần", email, brand)
        self.send(200, autopost_page(CFG["brands"][brand], portal, email, pw))

    # ---------------- POST (chỉ FreeRADIUS gọi, từ 127.0.0.1)
    def do_POST(self):
        purge()
        if urllib.parse.urlsplit(self.path).path != "/radius/authorize":
            return self.send(404, "", "text/plain")
        if self.client_address[0] not in ("127.0.0.1", "::1") or self.headers.get("X-Real-IP"):
            return self.send(403, "", "text/plain")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
            user = attr(body, "User-Name").lower()
            pw = attr(body, "User-Password")
        except Exception as e:
            log.error("JSON từ FreeRADIUS không đọc được: %s", e)
            return self.send(400, "", "text/plain")
        with LOCK:
            t = TOKENS.get(user)
            if not t or t["exp"] < now():
                TOKENS.pop(user, None)
                return self.send(404, "", "text/plain")       # notfound -> FreeRADIUS xử lý tiếp/ từ chối
            if not pw or not secrets.compare_digest(t["pw_hash"], sha(pw)):
                return self.send(404, "", "text/plain")             # không khớp -> notfound, để FreeRADIUS thử tài khoản cục bộ
            TOKENS.pop(user, None)                             # dùng một lần
        log.info("RADIUS chấp nhận %s", user)
        record(t["brand"], user, "", user.rsplit("@", 1)[-1], "", "radius_accept")
        self.send(200, json.dumps({"control:Cleartext-Password": pw}), "application/json")


def attr(body, name):
    v = body.get(name)
    if isinstance(v, dict):
        v = v.get("value")
    if isinstance(v, list):
        v = v[0] if v else ""
    return v or ""


def autopost_page(bc, portal, user, pw):
    e = html.escape
    color = e(bc.get("color", "#1E386B"))
    name = e(bc.get("display_name", "Wi-Fi"))
    hidden = [(portal["user_field"], user), (portal["pass_field"], pw)] + list(portal["fields"].items())
    inputs = "\n".join(f'<input type="hidden" name="{e(k)}" value="{e(v)}">' for k, v in hidden)
    return f"""<!DOCTYPE html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{name} Wi-Fi — Đang kết nối</title>
<style>body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
font-family:"Segoe UI",Roboto,Arial,sans-serif;background:#f6f7f9;color:#1f1f24}}
.c{{text-align:center;padding:32px}}.s{{width:46px;height:46px;border:4px solid #e3e5ea;border-top-color:{color};
border-radius:50%;margin:0 auto 18px;animation:r 1s linear infinite}}@keyframes r{{to{{transform:rotate(360deg)}}}}
h1{{font-size:20px;margin:0 0 6px}}p{{color:#6b6b75;margin:0 0 18px;font-size:14px}}
button{{background:{color};color:#fff;border:0;border-radius:10px;padding:12px 26px;font-weight:700;font-size:14px}}</style>
</head><body><div class="c"><div class="s"></div><h1>Đang kết nối Wi-Fi…</h1>
<p>{e(user)}</p>
<form id="f" method="POST" action="{e(portal["action"])}">
{inputs}
<noscript><button type="submit">Tiếp tục</button></noscript></form></div>
<script>document.getElementById('f').submit();</script></body></html>"""


def main():
    global CFG
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    CFG = load_config(CONFIG_PATH)
    os.makedirs(os.path.dirname(CFG["db_path"]), exist_ok=True)
    db().close()
    srv = ThreadingHTTPServer((CFG["listen_host"], CFG["listen_port"]), H)
    log.info("wifi-auth lắng nghe %s:%s, brands=%s", CFG["listen_host"], CFG["listen_port"], list(CFG["brands"]))
    srv.serve_forever()


if __name__ == "__main__":
    main()
