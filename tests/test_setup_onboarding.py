from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
sys.path.insert(0, str(LEISURELLM_DIR))
sys.path.insert(0, str(ROOT_DIR))

from admin.routers.settings import _build_onboarding_experience


class TestSetupOnboardingExperience:
    def test_defaults_to_local_only_when_local_runtime_is_available(self):
        experience = _build_onboarding_experience(
            {
                "ollama_detected": True,
                "provider_connected": True,
                "local_models": ["gemma3:4b"],
                "cloud_keys": {"openai": False, "anthropic": False, "openrouter": False},
            }
        )

        assert experience["default_path"] == "local_only"
        assert experience["modes"][0]["label"] == "Local-only"
        assert experience["modes"][0]["status"] == "ready"

    def test_falls_back_to_cloud_assisted_when_local_runtime_is_unavailable(self):
        experience = _build_onboarding_experience(
            {
                "ollama_detected": False,
                "provider_connected": False,
                "local_models": [],
                "cloud_keys": {"openai": False, "anthropic": False, "openrouter": False},
            }
        )

        assert experience["default_path"] == "cloud_assisted"
        assert experience["fallback_behavior"]["title"] == "If local AI is not ready yet"
        assert "local path first" in experience["fallback_behavior"]["summary"]