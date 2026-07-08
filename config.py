"""
Root-level configuration shim for Leisure Center Assistant bot.

This file re-exports all configuration from LeisureLLM/config.py
to support imports from the root directory.

CANONICAL SOURCE: LeisureLLM/config.py
All configuration changes should be made there.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure LeisureLLM is on the path
LEISURELLM_DIR = Path(__file__).resolve().parent / "LeisureLLM"
if str(LEISURELLM_DIR) not in sys.path:
    sys.path.insert(0, str(LEISURELLM_DIR))

# Re-export everything from the canonical config
from LeisureLLM.config import *  # noqa: F401, F403
from LeisureLLM.config import (
    BASE_DIR,
    BOTS_CHANNEL_ID,
    BOTS_OFFICE_CHANNEL_ID,
    OPS_CHANNEL_NAME,
    PARTNERS_CHANNEL_NAME,
    SCHEMES_DREAMS_CHANNEL_ID,
    allowed_channel_ids,
    # Explicitly list main exports for IDE support
    bot_token,
    directory_path,
    gpt_key,
    hash_csv,
    persist_directory,
    server_id,
)
