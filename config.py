"""
Shared config.yaml loader, used by listener.py, sync.py, and anything
else that needs Telegram credentials, the group list, or the newer
Sheets/notify/geocoding settings.
"""

import sys
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(
            "config.yaml not found. Copy config.example.yaml to config.yaml "
            "and fill in your settings first."
        )
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)
