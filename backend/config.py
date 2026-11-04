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
GITHUB_TOKEN = env_values.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
E2B_API_KEY = env_values.get("E2B_API_KEY") or os.getenv("E2B_API_KEY", "")

# Ensure the system-level environ is also updated for subprocesses
if env_values.get("GITHUB_TOKEN"):
    os.environ["GITHUB_TOKEN"] = env_values.get("GITHUB_TOKEN")

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# Gemini Integration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
LLM_PRIMARY_PROVIDER = os.getenv('LLM_PRIMARY_PROVIDER', 'groq').strip().lower()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
PORT = int(os.getenv("PORT", "8000"))
