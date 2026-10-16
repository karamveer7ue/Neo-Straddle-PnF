"""Owner-only local files: .env holds the Neo MPIN and API secrets, and the state
files and logs record the trading account's activity."""

import logging
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)


def keep_files_private() -> None:
    """Make every file this process creates owner-only, and warn about a .env that
    other accounts on the machine can read."""
    os.umask(0o077)
    env = ROOT / ".env"
    try:
        mode = env.stat().st_mode
    except FileNotFoundError:
        return
    if mode & 0o077:
        log.warning("%s can be read by other accounts on this machine: run chmod 600 .env", env)
