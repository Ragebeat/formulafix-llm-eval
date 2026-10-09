"""Settings. Reads .env if present (stdlib only); real environment variables win."""

import hashlib
import os
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PROMPTS = ROOT / "prompts"
GOLDEN_PATH = DATA / "golden_v1.jsonl"
VALIDATION_PATH = DATA / "validation_v1.jsonl"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Order of precedence: real environment > the profile file named in ENV_FILE > .env.
# A profile (e.g. ENV_FILE=.env.openrouter) overrides only the LLM_* keys it contains;
# the database settings still come from .env.
_profile = os.environ.get("ENV_FILE")
if _profile:
    _load_dotenv(ROOT / _profile)
_load_dotenv(ROOT / ".env")


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    password = os.environ.get("POSTGRES_PASSWORD")
    if not password:
        raise SystemExit("Set POSTGRES_PASSWORD (or DATABASE_URL) in .env")
    user = os.environ.get("POSTGRES_USER", "formulafix")
    db = os.environ.get("POSTGRES_DB", "formulafix")
    port = os.environ.get("POSTGRES_PORT", "5440")  # same default as docker-compose.yml
    # 127.0.0.1, not "localhost": on Windows localhost is tried as IPv6 (::1) first, Docker
    # publishes the port on IPv4 only, and the failed attempt can stall for 20+ seconds
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    return f"postgresql://{user}:{quote(password, safe='')}@{host}:{port}/{db}"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
