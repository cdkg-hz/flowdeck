from __future__ import annotations

from datetime import timedelta
import hashlib
import hmac
import secrets
from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from backend.db import AdminAccount, AdminSession, LoginAttempt, utcnow
from backend.service import aware

COOKIE = "flowdeck_session"


def session_state(settings, *, authenticated: bool, admin_configured: bool, username: str | None = None, csrf_token: str | None = None):
    return {
        "authenticated": authenticated,
        "username": username,
        "csrf_token": csrf_token,
        "admin_configured": admin_configured,
        "require_login": settings.require_login,
    }


def password_hash(password: str) -> str:
    if not 12 <= len(password) <= 1024:
        raise ValueError("管理员密码长度必须为 12–1024 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, encoded: str) -> bool:
    try:
        name, salt, digest = encoded.split("$")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return name == "scrypt" and hmac.compare_digest(actual.hex(), digest)
    except (ValueError, TypeError):
        return False


def set_admin(db, username: str, password: str):
    if not username.strip() or len(username) > 100:
        raise ValueError("请输入有效管理员名称")
    hashed = password_hash(password)
    db.execute(update(AdminAccount).where(AdminAccount.id == 1).values(revision=AdminAccount.revision + 1))
    db.expire_all()
    admin = db.get(AdminAccount, 1)
    if admin:
        admin.username, admin.password_hash = username.strip(), hashed
        admin.revision += 1
    else:
        db.add(AdminAccount(id=1, username=username.strip(), password_hash=hashed))
    db.execute(delete(AdminSession))
    db.commit()


def session_id(request: Request) -> str:
    return hashlib.sha256(request.cookies.get(COOKIE, "").encode()).hexdigest()


def require_admin(request: Request, db, *, write: bool = False):
    if write:
        # Lock first, then re-read the session: a concurrent reset/logout must win
        # before a previously-authorized request can mutate project state.
        db.execute(update(AdminAccount).where(AdminAccount.id == 1).values(revision=AdminAccount.revision + 1))
        db.expire_all()
    session = db.get(AdminSession, session_id(request))
    admin = db.get(AdminAccount, 1)
    if not session or not admin or aware(session.expires_at) <= utcnow():
        raise HTTPException(401, "请先登录管理员账号")
    if write:
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf_token):
            raise HTTPException(403, "会话校验失败，请刷新页面")
    return admin, session


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


def install_auth(app, settings, sessions):
    @app.get("/api/auth/session")
    def current_session(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        with sessions() as db:
            configured = bool(db.get(AdminAccount, 1))
            try:
                admin, session = require_admin(request, db)
                return session_state(settings, authenticated=True, username=admin.username, csrf_token=session.csrf_token, admin_configured=configured)
            except HTTPException:
                return session_state(settings, authenticated=False, admin_configured=configured)

    @app.post("/api/auth/login")
    def login(body: LoginBody, request: Request, response: Response):
        # JSON request bodies plus origin checking prevent login CSRF.
        origin = request.headers.get("origin")
        if origin:
            forwarded_host = request.headers.get("x-forwarded-host")
            forwarded_proto = request.headers.get("x-forwarded-proto") or request.url.scheme
            forwarded_origin = f"{forwarded_proto}://{forwarded_host}" if forwarded_host else None
            allowed = {str(request.base_url).rstrip("/")}
            if forwarded_origin:
                allowed.add(forwarded_origin.rstrip("/"))
            if settings.webhook_public_base_url:
                allowed.add(settings.webhook_public_base_url.rstrip("/"))
            if origin.rstrip("/") not in allowed:
                raise HTTPException(403, "禁止跨站登录")
        address = hashlib.sha256((request.client.host if request.client else "unknown").encode()).hexdigest()
        with sessions() as db:
            # Reserve attempts serially, including on PostgreSQL, before running scrypt.
            db.execute(update(AdminAccount).where(AdminAccount.id == 1).values(revision=AdminAccount.revision + 1))
            cutoff = utcnow() - timedelta(minutes=15)
            db.execute(delete(LoginAttempt).where(LoginAttempt.occurred_at < cutoff))
            recent = db.scalar(select(func.count()).select_from(LoginAttempt).where(LoginAttempt.address_hash == address))
            total = db.scalar(select(func.count()).select_from(LoginAttempt))
            if recent >= 5 or total >= 50:
                db.rollback()
                raise HTTPException(429, "登录尝试过多，请 15 分钟后重试")
            db.add(LoginAttempt(address_hash=address))
            db.commit()
            # Password validation and session creation share the password-reset lock.
            db.execute(update(AdminAccount).where(AdminAccount.id == 1).values(revision=AdminAccount.revision + 1))
            db.expire_all()
            admin = db.get(AdminAccount, 1)
            fallback = "scrypt$" + "00" * 16 + "$" + "00" * 64
            valid = check_password(body.password, admin.password_hash if admin else fallback)
            if not admin or not valid or not hmac.compare_digest(admin.username.encode(), body.username.encode()):
                raise HTTPException(401, "账号或密码不正确；首次使用请在服务器初始化管理员")
            db.execute(delete(AdminSession).where((AdminSession.expires_at < utcnow()) | (AdminSession.id == session_id(request))))
            raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.add(AdminSession(id=hashlib.sha256(raw.encode()).hexdigest(), csrf_token=csrf, expires_at=utcnow() + timedelta(hours=settings.session_hours)))
            db.execute(delete(LoginAttempt).where(LoginAttempt.address_hash == address))
            db.commit()
            response.set_cookie(COOKIE, raw, max_age=settings.session_hours * 3600, httponly=True, secure=settings.session_secure, samesite="strict", path="/")
            response.headers["Cache-Control"] = "no-store"
            return session_state(settings, authenticated=True, username=admin.username, csrf_token=csrf, admin_configured=True)

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        with sessions() as db:
            require_admin(request, db, write=True)
            db.execute(delete(AdminSession).where(AdminSession.id == session_id(request)))
            db.commit()
        response.delete_cookie(COOKIE, path="/")
        response.headers["Cache-Control"] = "no-store"
        return session_state(settings, authenticated=False, admin_configured=True)
