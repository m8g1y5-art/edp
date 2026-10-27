"""账号、登录会话、角色权限、操作日志。

两种角色：
  admin      管理员：改设置（模型、资源、词表）、管理账号、上传许可证、删除项目和批次、看操作日志
  annotator  标注员：导入、预检、开始处理、浏览校对、导出

单机模式（EDP_MODE=single）不需要登录，所有操作按管理员处理；服务器模式必须登录。
"""
import hashlib
import hmac
import re
import secrets
import time

from .db import SERVER, q, x, xc

ROLES = {"admin": "管理员", "annotator": "标注员"}
SESSION_DAYS = 7
COOKIE = "edp_session"
LOCAL_USER = {"id": 0, "username": "local", "name": "本机", "role": "admin"}

# 只有管理员能做的操作：(方法, 路径正则)
ADMIN_ONLY = [
    ("PUT", r"^/api/settings$"), ("PUT", r"^/api/vocab$"), ("POST", r"^/api/vlm/test$"),
    ("POST", r"^/api/sam/install$"), ("POST", r"^/api/cleanup$"), ("POST", r"^/api/open$"),
    ("DELETE", r"^/api/projects/"), ("DELETE", r"^/api/batches/"),
    ("POST", r"^/api/batchq/"),
    ("*", r"^/api/users"), ("GET", r"^/api/audit"), ("POST", r"^/api/license$"),
]
# 不登录也能访问的接口
PUBLIC = [r"^/api/login$", r"^/api/me$"]


# ---------------------------------------------------------------- 密码 ----
def hash_pw(pw, iters=200_000):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), iters).hex()
    return f"pbkdf2${iters}${salt}${h}"


def check_pw(pw, stored):
    try:
        _, iters, salt, h = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), int(iters)).hex()
        return hmac.compare_digest(got, h)
    except Exception:
        return False


def pw_problem(pw):
    if len(pw or "") < 8:
        return "密码至少 8 位"
    if pw.isdigit() or pw.isalpha():
        return "密码要同时包含字母和数字"
    return ""


# ---------------------------------------------------------------- 账号 ----
def _pub(u):
    return {k: u[k] for k in ("id", "username", "name", "role", "disabled", "created", "last_login")} if u else None


def users():
    return [_pub(u) for u in q("SELECT * FROM users ORDER BY id")]


def create_user(username, pw, role="annotator", name=""):
    username = (username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{2,32}", username):
        raise ValueError("用户名只能用 2–32 位字母、数字、下划线、点或短横线")
    if role not in ROLES:
        raise ValueError("角色不对")
    p = pw_problem(pw)
    if p:
        raise ValueError(p)
    if q("SELECT 1 FROM users WHERE username=?", (username,), one=True):
        raise ValueError("用户名已存在")
    x("INSERT INTO users(username,name,pw,role,created) VALUES(?,?,?,?,?)", (username, name or username, hash_pw(pw), role, time.time()))
    return _pub(q("SELECT * FROM users WHERE username=?", (username,), one=True))


def update_user(uid, role=None, name=None, disabled=None, pw=None):
    u = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not u:
        raise ValueError("账号不存在")
    if (role and role != "admin" or disabled) and u["role"] == "admin":
        left = q("SELECT COUNT(*) n FROM users WHERE role='admin' AND disabled=0 AND id!=?", (uid,), one=True)["n"]
        if not left:
            raise ValueError("至少要保留一个可用的管理员")
    if role:
        if role not in ROLES:
            raise ValueError("角色不对")
        x("UPDATE users SET role=? WHERE id=?", (role, uid))
    if name is not None:
        x("UPDATE users SET name=? WHERE id=?", (name, uid))
    if disabled is not None:
        x("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, uid))
        if disabled:
            x("DELETE FROM sessions WHERE user_id=?", (uid,))
    if pw:
        p = pw_problem(pw)
        if p:
            raise ValueError(p)
        x("UPDATE users SET pw=? WHERE id=?", (hash_pw(pw), uid))
        x("DELETE FROM sessions WHERE user_id=?", (uid,))
    return _pub(q("SELECT * FROM users WHERE id=?", (uid,), one=True))


def has_users():
    return q("SELECT 1 FROM users LIMIT 1", one=True) is not None


# ---------------------------------------------------------------- 会话 ----
_fails = {}  # ip -> [失败时间]，防止暴力猜密码


def login(username, pw, ip=""):
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 300]
    if len(recent) >= 8:
        raise PermissionError("失败次数太多，请 5 分钟后再试")
    u = q("SELECT * FROM users WHERE username=?", ((username or "").strip(),), one=True)
    if not u or u["disabled"] or not check_pw(pw or "", u["pw"]):
        _fails[ip] = recent + [now]
        raise PermissionError("用户名或密码不对")
    _fails.pop(ip, None)
    tok = secrets.token_urlsafe(32)
    x("INSERT INTO sessions(token,user_id,created,expires,ip) VALUES(?,?,?,?,?)", (tok, u["id"], now, now + SESSION_DAYS * 86400, ip))
    x("UPDATE users SET last_login=? WHERE id=?", (now, u["id"]))
    x("DELETE FROM sessions WHERE expires < ?", (now,))
    return tok, _pub(u)


def logout(tok):
    x("DELETE FROM sessions WHERE token=?", (tok,))


def user_of(tok):
    if not SERVER:
        return LOCAL_USER
    if not tok:
        return None
    r = q("""SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
             WHERE s.token=? AND s.expires>? AND u.disabled=0""", (tok, time.time()), one=True)
    if r:  # 用着就自动续期
        xc("UPDATE sessions SET expires=? WHERE token=? AND expires<?", (time.time() + SESSION_DAYS * 86400, tok, time.time() + (SESSION_DAYS - 1) * 86400))
    return _pub(r)


def needs_admin(method, path):
    return any((m == "*" or m == method) and re.search(p, path) for m, p in ADMIN_ONLY)


def is_public(path):
    return not path.startswith("/api/") or any(re.search(p, path) for p in PUBLIC)


# ---------------------------------------------------------------- 操作日志 ----
def audit(user, method, path, status, detail="", ip=""):
    x("INSERT INTO audit(ts,user,role,method,path,status,detail,ip) VALUES(?,?,?,?,?,?,?,?)",
      (time.time(), (user or {}).get("username", ""), (user or {}).get("role", ""), method, path, status, detail[:500], ip))


def audit_rows(limit=200, before=None, user=None):
    cond, args = [], []
    if before:
        cond.append("id < ?"); args.append(before)
    if user:
        cond.append("user = ?"); args.append(user)
    where = (" WHERE " + " AND ".join(cond)) if cond else ""
    return q(f"SELECT * FROM audit{where} ORDER BY id DESC LIMIT ?", tuple(args) + (min(int(limit), 1000),))
