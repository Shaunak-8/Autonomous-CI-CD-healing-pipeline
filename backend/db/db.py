from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
import os
from dotenv import load_dotenv

# Explicitly load .env from the backend root directory (parent of the db folder)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _sqlite_database_url():
    return f"sqlite:///{os.path.join(BASE_DIR, 'hogriders.db')}"


def _resolve_database_url():
    configured_url = os.getenv("DATABASE_URL", "")
    if not configured_url:
        return _sqlite_database_url()

    candidate = configured_url
    if candidate.startswith("postgresql://"):
        candidate = candidate.replace("postgresql://", "postgresql+psycopg2://", 1)

    try:
        test_engine = create_engine(candidate, pool_pre_ping=True)
        with test_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return candidate
    except Exception:
        return _sqlite_database_url()


DATABASE_URL = _resolve_database_url()

engine_kwargs = {}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    **engine_kwargs,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()

# Dependency for FastAPI
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
