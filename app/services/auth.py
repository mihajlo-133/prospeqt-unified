import hmac

from fastapi import Cookie, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

_TOKEN_MAX_AGE = 86400  # 24 hours in seconds


def _get_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key)


def check_password(submitted: str) -> bool:
    """Compare submitted password against ADMIN_PASSWORD using constant-time comparison."""
    return hmac.compare_digest(submitted, settings.admin_password)


def create_session_token() -> str:
    """Create a signed session token for the admin user."""
    serializer = _get_serializer()
    return serializer.dumps("admin")


def verify_session_token(token: str) -> bool:
    """Verify a session token. Returns True if valid and not expired."""
    serializer = _get_serializer()
    try:
        serializer.loads(token, max_age=_TOKEN_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


class AdminAuthRedirect(Exception):
    """Raised when unauthenticated browser request hits admin routes."""
    pass


async def require_admin(request: Request, admin_session: str | None = Cookie(default=None)) -> None:
    """FastAPI dependency that enforces admin authentication via cookie.
    Redirects to login page for browser requests, returns 401 JSON for API calls."""
    if not admin_session or not verify_session_token(admin_session):
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            raise AdminAuthRedirect()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authentication required",
        )
