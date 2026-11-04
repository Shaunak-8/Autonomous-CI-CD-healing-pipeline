from fastapi import Request, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import logging
from config import SUPABASE_URL, SUPABASE_SERVICE_KEY
from supabase import create_client, Client
from starlette.concurrency import run_in_threadpool
from supabase_auth.errors import AuthApiError, AuthRetryableError
import httpx

logger = logging.getLogger(__name__)

security = HTTPBearer()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

async def verify_token(request: Request):
    """Verify the Supabase JWT using the Supabase client."""
    auth_header = request.headers.get("Authorization")
    parts = auth_header.split() if auth_header else []
    if len(parts) != 2 or parts[0].lower() != 'bearer' or parts[1] in {'undefined', 'null'}:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    
    token = parts[1]
    
    try:
        response = await run_in_threadpool(supabase.auth.get_user, token)
        user = response.user
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
            
        request.state.user_id = user.id
        request.state.email = user.email
        return {"sub": user.id, "email": user.email}
    except HTTPException:
        raise
    except (AuthRetryableError, httpx.RequestError):
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
