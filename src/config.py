"""Configuration and environment setup for custody document processing."""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

# Paths — auto-detect project root (directory containing src/)
CUSTODY_DIR = Path(os.getenv("CUSTODY_DIR", Path(__file__).resolve().parent.parent))
PROCESSED_DIR = CUSTODY_DIR / "processed"
DB_PATH = CUSTODY_DIR / "custody.db"

# Load API key from .env in project root
_env_path = CUSTODY_DIR / ".env"
if _env_path.exists():
    load_dotenv(_env_path, override=True)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Model config
MODEL = "claude-sonnet-4-20250514"
MODEL_LINKING = "claude-haiku-4-5-20251001"  # cheaper model for issue linking
DPI = 150  # produces ~1242x1753 images, within Claude's 1568px limit
JPEG_QUALITY = 85

# API safety
API_TIMEOUT = 120       # seconds per Claude API call
API_MAX_RETRIES = 2     # SDK built-in exponential backoff for 429/500
MAX_PAGES = 20          # refuse PDFs above this page count

# Logging
LOG_PATH = CUSTODY_DIR / "custody.log"


def setup_logging() -> logging.Logger:
    """Configure rotating file + console logger. Safe to call multiple times."""
    logger = logging.getLogger("custody")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh = RotatingFileHandler(
        str(LOG_PATH), maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


def check_setup() -> bool:
    """Verify environment is ready. Returns True if OK."""
    log = setup_logging()
    ok = True

    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not found.")
        log.error(f"  Add it to {CUSTODY_DIR / '.env'} or set ANTHROPIC_API_KEY env var")
        ok = False

    # Check pdf2image / poppler
    try:
        from pdf2image import convert_from_path  # noqa: F401
    except ImportError:
        log.error("pdf2image not installed. Run: pip install pdf2image")
        ok = False

    # Create dirs
    PROCESSED_DIR.mkdir(exist_ok=True)

    if ok:
        log.info("Setup OK.")
    return ok


if __name__ == "__main__":
    if check_setup():
        log = setup_logging()
        log.info(f"  CUSTODY_DIR: {CUSTODY_DIR}")
        log.info(f"  DB_PATH:     {DB_PATH}")
        log.info(f"  API key:     {ANTHROPIC_API_KEY[:12]}...")
        log.info(f"  Model:       {MODEL}")
        log.info(f"  DPI:         {DPI}")
    else:
        sys.exit(1)
