"""
Persona modules for autonomous operations.

Each persona is a specialized "department" that handles specific business functions:
- Scout: Web research and opportunity discovery
- Dreamer: Ideation and blue-sky thinking
- Rainmaker: Lead management and pipeline
- Steward: Self-monitoring and health checks
- Coordinator: Daily operations and meeting facilitation
- Chief: Strategic analysis

All personas share:
- Access to LLM service for generation
- Access to database for persistence
- Posting to bots channel for transparency
"""

from .base import EASTERN, BasePersona
from .curator import CuratorMixin
from .dreamer import DreamerMixin
from .rainmaker import RainmakerMixin
from .scout import ScoutMixin
from .steward import StewardMixin

__all__ = [
    "BasePersona",
    "EASTERN",
    "CuratorMixin",
    "ScoutMixin",
    "DreamerMixin",
    "RainmakerMixin",
    "StewardMixin",
]
