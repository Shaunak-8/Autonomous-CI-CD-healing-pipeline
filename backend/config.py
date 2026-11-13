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

GROQ_API_KEY = env_values.get("GROQ_API_KEY") or os.getenv("GROQ_API_KEY", "")
_groq_keys = [GROQ_API_KEY]
_groq_keys.extend((env_values.get("GROQ_API_KEYS") or os.getenv("GROQ_API_KEYS", "")).split(","))
_groq_keys.extend((env_values.get(f"GROQ_API_KEY_{index}") or os.getenv(f"GROQ_API_KEY_{index}", "")) for index in range(1, 6))
GROQ_API_KEYS = list(dict.fromkeys(key.strip() for key in _groq_keys if key and key.strip()))[:5]
GITHUB_TOKEN = env_values.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
E2B_API_KEY = env_values.get("E2B_API_KEY") or os.getenv("E2B_API_KEY", "")

# Ensure the system-level environ is also updated for subprocesses
if env_values.get("GITHUB_TOKEN"):
    os.environ["GITHUB_TOKEN"] = env_values.get("GITHUB_TOKEN")

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# Gemini Integration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
_gemini_keys = [GEMINI_API_KEY]
_gemini_keys.extend((env_values.get("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEYS", "")).split(","))
_gemini_keys.extend((env_values.get(f"GEMINI_API_KEY_{index}") or os.getenv(f"GEMINI_API_KEY_{index}", "")) for index in range(1, 6))
GEMINI_API_KEYS = list(dict.fromkeys(key.strip() for key in _gemini_keys if key and key.strip()))[:5]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
LLM_PRIMARY_PROVIDER = os.getenv('LLM_PRIMARY_PROVIDER', 'groq').strip().lower()

# Five credentials total across providers. Keep the primary provider's credentials
# first so it receives slots when more than five keys are configured.
LLM_MAX_API_KEYS = 5
if LLM_PRIMARY_PROVIDER == 'gemini':
    GEMINI_API_KEYS = GEMINI_API_KEYS[:LLM_MAX_API_KEYS]
    GROQ_API_KEYS = GROQ_API_KEYS[:max(0, LLM_MAX_API_KEYS - len(GEMINI_API_KEYS))]
else:
    GROQ_API_KEYS = GROQ_API_KEYS[:LLM_MAX_API_KEYS]
    GEMINI_API_KEYS = GEMINI_API_KEYS[:max(0, LLM_MAX_API_KEYS - len(GROQ_API_KEYS))]

# Reserved for the provider-neutral multi-model batch layer. Keys are optional and
# intentionally empty by default; provider adapters should be constructed only when
# a key and model are both configured.
try:
    LLM_BATCH_SIZE = max(1, min(8, int(os.getenv('LLM_BATCH_SIZE', '2'))))
except ValueError:
    LLM_BATCH_SIZE = 2
LLM_ISSUES_PER_KEY = 10
LLM_RUN_ISSUE_LIMIT = 50
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
OPENAI_MODEL = os.getenv('OPENAI_MODEL', '')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
ANTHROPIC_MODEL = os.getenv('ANTHROPIC_MODEL', '')

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
PORT = int(os.getenv("PORT", "8000"))
