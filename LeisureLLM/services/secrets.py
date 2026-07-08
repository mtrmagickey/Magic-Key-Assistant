"""
Secure Secrets Manager

Uses the OS keyring (Windows Credential Manager / macOS Keychain / Linux Secret Service)
to store API keys securely, rather than plain text .env files.

Usage:
    from services.secrets import SecretsManager
    
    secrets = SecretsManager()
    
    # Store a key
    secrets.set("openai", "sk-...")
    
    # Retrieve a key
    api_key = secrets.get("openai")
    
    # List stored keys (names only, not values)
    keys = secrets.list_keys()
"""

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Service name for keyring storage
SERVICE_NAME = "LeisureCenterAssistant"

# Supported secret keys organized by category
KNOWN_KEYS = {
    # Discord Configuration
    "discord_token": {
        "name": "Discord Bot Token",
        "env_var": "DISCORD_TOKEN",
        "category": "discord",
        "sensitive": True,
        "description": "Your Discord bot token from the Developer Portal",
    },
    
    # LLM API Keys
    "openai": {
        "name": "OpenAI API Key",
        "env_var": "OPENAI_API_KEY",
        "category": "llm",
        "sensitive": True,
        "description": "For GPT-4o, GPT-5, o3-mini, etc.",
    },
    "anthropic": {
        "name": "Anthropic API Key",
        "env_var": "ANTHROPIC_API_KEY",
        "category": "llm",
        "sensitive": True,
        "description": "For Claude models",
    },
    "openrouter": {
        "name": "OpenRouter API Key",
        "env_var": "OPENROUTER_API_KEY",
        "category": "llm",
        "sensitive": True,
        "description": "Multi-provider routing service",
    },
    
    # External Services
    "tavily": {
        "name": "Tavily API Key",
        "env_var": "TAVILY_API_KEY",
        "category": "services",
        "sensitive": True,
        "description": "For Scout web search functionality",
    },
    "database_url": {
        "name": "Database URL",
        "env_var": "DATABASE_URL",
        "category": "services",
        "sensitive": True,
        "description": "PostgreSQL connection string (optional)",
    },
}

# Category metadata
CATEGORIES = {
    "discord": {
        "name": "Discord Configuration",
        "icon": "🤖",
        "description": "Bot identity and server settings",
    },
    "llm": {
        "name": "LLM API Keys",
        "icon": "🧠",
        "description": "Language model provider credentials",
    },
    "services": {
        "name": "External Services",
        "icon": "🔌",
        "description": "Third-party integrations",
    },
}


class SecretsManager:
    """
    Manages API keys and secrets with multiple storage backends:
    1. OS Keyring (preferred, secure)
    2. Environment variables (fallback)
    3. Encrypted local file (future)
    """
    
    def __init__(self):
        self._keyring_available = self._check_keyring()
        self._cache: Dict[str, str] = {}
        
    def _check_keyring(self) -> bool:
        """Check if keyring is available."""
        try:
            import keyring
            # Test if we can actually use it
            keyring.get_keyring()
            return True
        except Exception as e:
            logger.warning(f"Keyring not available: {e}. Falling back to environment variables.")
            return False
    
    def get(self, key: str) -> Optional[str]:
        """
        Get a secret value.
        Priority: Cache -> Keyring -> Environment -> None
        """
        # Check cache first
        if key in self._cache:
            return self._cache[key]
        
        value = None
        
        # Try keyring
        if self._keyring_available:
            try:
                import keyring
                value = keyring.get_password(SERVICE_NAME, key)
            except Exception as e:
                logger.debug(f"Keyring get failed for {key}: {e}")
        
        # Fallback to environment
        if not value:
            env_key = self._key_to_env(key)
            value = os.environ.get(env_key)
        
        # Cache if found
        if value:
            self._cache[key] = value
            
        return value
    
    def set(self, key: str, value: str) -> bool:
        """
        Store a secret value.
        Stores in keyring if available, otherwise just caches.
        """
        # Update cache
        self._cache[key] = value
        
        # Store in keyring
        if self._keyring_available:
            try:
                import keyring
                keyring.set_password(SERVICE_NAME, key, value)
                logger.info(f"Stored '{key}' in OS keyring")
                return True
            except Exception as e:
                logger.warning(f"Failed to store in keyring: {e}")
                return False
        else:
            logger.info(f"Keyring not available. '{key}' cached in memory only.")
            return True
    
    def delete(self, key: str) -> bool:
        """Remove a secret from all storage layers (cache, keyring, env var, .env file)."""
        # Remove from cache
        self._cache.pop(key, None)

        # Remove from environment variable
        env_key = self._key_to_env(key)
        os.environ.pop(env_key, None)

        # Remove from .env file on disk
        self._remove_from_env_file(env_key)

        # Remove from keyring
        if self._keyring_available:
            try:
                import keyring
                keyring.delete_password(SERVICE_NAME, key)
                logger.info(f"Deleted '{key}' from OS keyring")
            except Exception as e:
                logger.warning(f"Failed to delete from keyring: {e}")
                return False
        return True
    
    def list_keys(self) -> List[Dict]:
        """
        List all known secret keys and their status, organized by category.
        Returns list of {key, name, has_value, category, sensitive, description}.
        """
        result = []
        for key, info in KNOWN_KEYS.items():
            value = self.get(key)
            result.append({
                "key": key,
                "name": info["name"],
                "has_value": value is not None,
                "category": info["category"],
                "sensitive": info.get("sensitive", True),
                "description": info.get("description", ""),
                "env_var": info["env_var"],
            })
        return result
    
    def list_keys_by_category(self) -> Dict[str, Dict]:
        """
        List all keys grouped by category.
        Returns {category_id: {name, icon, description, secrets: [...]}} with category metadata.
        """
        all_keys = self.list_keys()
        by_category = {}
        
        for cat_id, cat_info in CATEGORIES.items():
            cat_keys = [k for k in all_keys if k["category"] == cat_id]
            by_category[cat_id] = {
                "name": cat_info["name"],
                "icon": cat_info["icon"],
                "description": cat_info["description"],
                "secrets": cat_keys,
            }
        
        return by_category
    
    def _key_to_env(self, key: str) -> str:
        """Convert key name to environment variable name."""
        if key in KNOWN_KEYS:
            return KNOWN_KEYS[key]["env_var"]
        return f"LEISURE_{key.upper()}"

    @staticmethod
    def _remove_from_env_file(env_var: str) -> None:
        """Remove a line matching ``ENV_VAR=...`` from the .env file (if it exists)."""
        try:
            env_path = Path(__file__).resolve().parent.parent / ".env"
            if not env_path.exists():
                return
            lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
            filtered = [
                ln for ln in lines
                if not ln.lstrip().startswith(f"{env_var}=")
            ]
            if len(filtered) != len(lines):
                env_path.write_text("".join(filtered), encoding="utf-8")
                logger.info(f"Removed {env_var} from .env file")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not clean .env for {env_var}: {exc}")
    
    def is_keyring_available(self) -> bool:
        """Check if secure storage is available."""
        return self._keyring_available
    
    def get_storage_info(self) -> Dict:
        """Get info about current storage backend."""
        info = {
            "keyring_available": self._keyring_available,
            "storage_method": "OS Keyring" if self._keyring_available else "Environment Variables",
        }
        
        if self._keyring_available:
            try:
                import keyring
                info["keyring_backend"] = str(keyring.get_keyring())
            except Exception as e:
                logger.warning("get_storage_info: suppressed %s", e)
        
        return info


# Global instance
_secrets_manager: Optional[SecretsManager] = None


def get_secrets_manager() -> SecretsManager:
    """Get or create the global secrets manager."""
    global _secrets_manager
    if _secrets_manager is None:
        _secrets_manager = SecretsManager()
    return _secrets_manager
