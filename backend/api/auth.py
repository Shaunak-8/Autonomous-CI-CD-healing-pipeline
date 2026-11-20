import asyncio
from typing import Any

from fastapi import Request, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import logging
from config import SUPABASE_JWT_SECRET, SUPABASE_SERVICE_KEY, SUPABASE_URL
from supabase import create_client, Client
from starlette.concurrency import run_in_threadpool
from supabase_auth.errors import AuthApiError, AuthRetryableError
import httpx
from jose import JWTError, jwt

logger = logging.getLogger(__name__)

security = HTTPBearer()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _verify_locally(token: str) -> dict[str, Any] | None:
    """Validate legacy HS-signed Supabase access tokens without a network hop.

    Supabase projects that use asymmetric signing keys still need the Auth API,
    so those tokens deliberately return ``None`` and use the bounded fallback
    in ``verify_token`` below.
    """
    if not SUPABASE_JWT_SECRET:
        return None

    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        # Not a JWT in a format we can inspect. Let the Auth API provide the
        # canonical result (and preserve compatibility with opaque tokens).
        return None

    try:
        algorithm = header.get("alg", "")
        if algorithm not in {"HS256", "HS384", "HS512"}:
            return None

        issuer = f"{SUPABASE_URL.rstrip('/')}/auth/v1" if SUPABASE_URL else None
        options = {"verify_aud": True, "verify_iss": bool(issuer)}
        return jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=[algorithm],
            audience="authenticated",
            issuer=issuer,
            options=options,
        )
    except JWTError:
        # This remains an authentication failure; don't send a malformed or
        # tampered HS token to the network fallback.
        raise HTTPException(status_code=401, detail="Your session has expired or is invalid. Please sign in again.")

async def verify_token(request: Request):
    """Verify a Supabase JWT and attach its authenticated identity to request."""
    auth_header = request.headers.get("Authorization")
    parts = auth_header.split() if auth_header else []
    if len(parts) != 2 or parts[0].lower() != 'bearer' or parts[1] in {'undefined', 'null'}:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    
    token = parts[1]

    claims = _verify_locally(token)
    if claims is not None:
        user_id = claims.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Your session has expired or is invalid. Please sign in again.")
        request.state.user_id = user_id
        request.state.email = claims.get("email")
        return {"sub": user_id, "email": claims.get("email")}
    
    try:
        # A network lookup is only required for newer asymmetric Supabase
        # tokens. Keep it below the frontend request timeout so an unavailable
        # Auth service returns a useful response instead of hanging the page.
        response = await asyncio.wait_for(
            run_in_threadpool(supabase.auth.get_user, token), timeout=8
        )
        user = response.user
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
            
        request.state.user_id = user.id
        request.state.email = user.email
        return {"sub": user.id, "email": user.email}
    except HTTPException:
        raise
    except (asyncio.TimeoutError, AuthRetryableError, httpx.RequestError):
        raise HTTPException(status_code=503, detail='Authentication service is temporarily unavailable. Please retry.')
    except AuthApiError as exc:
        code = 503 if int(exc.status or 0) >= 500 else 401
        raise HTTPException(status_code=code, detail='Authentication service unavailable' if code == 503 else 'Your session has expired or is invalid. Please sign in again.')
    except Exception as exc:
        logger.warning('Authentication verification failed (%s)', type(exc).__name__)
        raise HTTPException(status_code=503, detail='Unable to verify your session. Please retry.')


def get_user_id(request: Request) -> str:
    """Helper to extract user_id from request state after middleware."""
    return getattr(request.state, "user_id", None)
