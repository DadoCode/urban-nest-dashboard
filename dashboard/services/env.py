"""Settings from the environment, falling back to a .env file in the project
root (NAME=value per line). Nothing here is ever logged or stored."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def get(name, default=None):
    value = os.environ.get(name)
    if value:
        return value.strip()
    try:
        for line in (ROOT / ".env").read_text().splitlines():
            key, _, val = line.partition("=")
            if key.strip() == name and val.strip():
                return val.strip().strip("\"'")
    except OSError:
        pass
    return default
