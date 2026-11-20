import os
from dotenv import load_dotenv

# Load environment variables from .env file with FORCE OVERRIDE
# This ensures shell-level stale tokens don't leak into the app
from pathlib import Path
ENV_PATH = Path(__file__).resolve().with_name('.env')
load_dotenv(ENV_PATH, override=True)

# Explicitly re-read from .env for critical tokens
import dotenv
env_values = dotenv.dotenv_values(ENV_PATH)


def _configured_api_keys(name: str) -> tuple[str, ...]:
    """Return the base key plus numbered key slots without exposing values."""
    names = [name]
    for source in (env_values, os.environ):
        names.extend(key for key in source if key.startswith(f'{name}_') and key[len(name) + 1:].isdigit())
    names = [name, *sorted(set(names) - {name}, key=lambda key: int(key.rsplit('_', 1)[1]))]
    keys = [(env_values.get(key) or os.getenv(key, '')).strip() for key in names]
    return tuple(dict.fromkeys(key for key in keys if key))

GROQ_API_KEY = env_values.get("GROQ_API_KEY") or os.getenv("GROQ_API_KEY", "")
GROQ_API_KEYS = _configured_api_keys("GROQ_API_KEY")
GITHUB_TOKEN = (
    env_values.get("HOGRIDERS_GITHUB_TOKEN")
    or os.getenv("HOGRIDERS_GITHUB_TOKEN", "")
    or env_values.get("GITHUB_TOKEN")
    or os.getenv("GITHUB_TOKEN", "")
)
E2B_API_KEY = env_values.get("E2B_API_KEY") or os.getenv("E2B_API_KEY", "")

# Ensure the system-level environ is also updated for subprocesses
if env_values.get("GITHUB_TOKEN"):
    os.environ["GITHUB_TOKEN"] = env_values.get("GITHUB_TOKEN")

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# Gemini Integration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_API_KEYS = _configured_api_keys("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
LLM_PRIMARY_PROVIDER = os.getenv('LLM_PRIMARY_PROVIDER', 'groq').strip().lower()

# Validate every batch of at most five files. Zero disables the per-run edit cap.
MAX_FIXES_PER_RUN = int(os.getenv('MAX_FIXES_PER_RUN', '0'))
FIX_BATCH_SIZE = min(5, int(os.getenv('FIX_BATCH_SIZE', '5')))
if MAX_FIXES_PER_RUN < 0 or FIX_BATCH_SIZE <= 0:
    raise ValueError('MAX_FIXES_PER_RUN must be nonnegative (0 means all findings); FIX_BATCH_SIZE must be positive')

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
PORT = int(os.getenv("PORT", "8000"))
