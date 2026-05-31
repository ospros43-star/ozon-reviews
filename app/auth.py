"""Авторизация: сессии через подписанные куки."""
import hashlib
import hmac
from functools import wraps

from fastapi import Request
from fastapi.responses import RedirectResponse

from app.config import settings

_SESSION_KEY = "user"


def _hash_password(password: str) -> str:
    return hmac.new(settings.secret_key.encode(), password.encode(), hashlib.sha256).hexdigest()


def check_credentials(username: str, password: str) -> bool:
    ok_user = hmac.compare_digest(username.strip(), settings.app_username)
    ok_pass = hmac.compare_digest(password.strip(), settings.app_password)
    return ok_user and ok_pass


def is_authenticated(request: Request) -> bool:
    return request.session.get(_SESSION_KEY) == settings.app_username


def login_user(request: Request, username: str) -> None:
    request.session[_SESSION_KEY] = username


def logout_user(request: Request) -> None:
    request.session.clear()


def require_auth(request: Request):
    """Dependency — редиректит на /login если не авторизован."""
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=302)
    return None
