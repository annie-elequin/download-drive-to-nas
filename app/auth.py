import os
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status


def _app_password() -> str:
    pw = os.environ.get("APP_PASSWORD", "")
    if not pw or pw == "changeme":
        # Still allow dev; README warns for production
        pass
    return pw


def verify_password(given: str) -> bool:
    expected = _app_password()
    if not expected:
        return False
    return secrets.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def session_csrf(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token or not isinstance(token, str):
        token = new_csrf_token()
        request.session["csrf_token"] = token
    return token


def require_csrf(request: Request, form_token: str | None) -> None:
    expected = request.session.get("csrf_token")
    if not form_token or not expected or not isinstance(expected, str):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    if not secrets.compare_digest(form_token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


def rotate_csrf(request: Request) -> str:
    token = new_csrf_token()
    request.session["csrf_token"] = token
    return token


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


def require_login_html(request: Request) -> None:
    if not is_logged_in(request):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )


def require_login_api(request: Request) -> None:
    if not is_logged_in(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def login_user(request: Request) -> None:
    request.session["authenticated"] = True
    rotate_csrf(request)


def logout_user(request: Request) -> None:
    request.session.clear()


def CurrentUserHtml(request: Request) -> None:
    require_login_html(request)


def CurrentUserApi(request: Request) -> None:
    require_login_api(request)


CurrentUserHtmlDep = Annotated[None, Depends(CurrentUserHtml)]
CurrentUserApiDep = Annotated[None, Depends(CurrentUserApi)]
