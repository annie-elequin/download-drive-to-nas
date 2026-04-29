import os
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import auth, jobs

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Drive to NAS")

_session_secret = os.environ.get("SESSION_SECRET", "")
if not _session_secret or _session_secret == "dev-insecure-change-me":
    _session_secret = "dev-insecure-change-me"

app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    session_cookie="session",
    same_site="lax",
    https_only=False,
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    if auth.is_logged_in(request):
        return RedirectResponse("/", status_code=303)
    csrf = auth.session_csrf(request)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"csrf_token": csrf, "error": None},
    )


@app.post("/login", response_class=HTMLResponse, response_model=None)
def login_submit(
    request: Request,
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
) -> RedirectResponse | HTMLResponse:
    auth.require_csrf(request, csrf_token)
    if not auth.verify_password(password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "csrf_token": auth.rotate_csrf(request),
                "error": "Invalid password",
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    auth.login_user(request)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(
    request: Request,
    _: auth.CurrentUserHtmlDep,
    csrf_token: Annotated[str, Form()],
) -> RedirectResponse:
    auth.require_csrf(request, csrf_token)
    auth.logout_user(request)
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    _: auth.CurrentUserHtmlDep,
) -> HTMLResponse:
    csrf = auth.session_csrf(request)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "csrf_token": csrf,
            "error": None,
            "default_destination": jobs.default_output_suggestion(),
        },
    )


def _validate_drive_url(url: str) -> None:
    u = url.strip().lower()
    if not u:
        raise HTTPException(status_code=400, detail="Drive URL is required")
    if "drive.google.com" not in u and "docs.google.com" not in u:
        raise HTTPException(status_code=400, detail="URL must be a Google Drive link")


def _index_form_context(request: Request, error: str | None, destination_path: str) -> dict:
    return {
        "csrf_token": auth.rotate_csrf(request),
        "error": error,
        "default_destination": destination_path.strip() or jobs.default_output_suggestion(),
    }


@app.post("/jobs", response_model=None)
def create_job_route(
    request: Request,
    _: auth.CurrentUserHtmlDep,
    drive_url: Annotated[str, Form()],
    destination_path: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
    archive_base: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    auth.require_csrf(request, csrf_token)
    dest_for_form = destination_path
    try:
        _validate_drive_url(drive_url)
        job = jobs.create_job(drive_url, destination_path, archive_base or "")
    except HTTPException as exc:
        msg = exc.detail if isinstance(exc.detail, str) else "Invalid request"
        return templates.TemplateResponse(
            request,
            "index.html",
            _index_form_context(request, msg, dest_for_form),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            _index_form_context(request, str(exc), dest_for_form),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    jobs.start_job_worker(job.id)
    auth.rotate_csrf(request)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.post("/jobs/{job_id}/cancel", response_model=None)
def cancel_job_route(
    request: Request,
    _: auth.CurrentUserHtmlDep,
    job_id: str,
    csrf_token: Annotated[str, Form()],
) -> RedirectResponse:
    auth.require_csrf(request, csrf_token)
    ok, msg = jobs.request_cancel(job_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_status_page(
    request: Request,
    _: auth.CurrentUserHtmlDep,
    job_id: str,
) -> HTMLResponse:
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    csrf = auth.session_csrf(request)
    return templates.TemplateResponse(
        request,
        "status.html",
        {"job": job, "csrf_token": csrf},
    )


@app.get("/api/jobs/{job_id}")
def job_status_api(
    request: Request,
    _: auth.CurrentUserApiDep,
    job_id: str,
) -> JSONResponse:
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse(jobs.job_public_dict(job))
