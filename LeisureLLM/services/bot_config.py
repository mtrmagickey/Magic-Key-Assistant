"""
Bot Configuration Manager

Runtime-editable configuration for the Leisure Center Assistant bot.
Settings can be edited via the web GUI and are persisted to a JSON file.

This layer sits ABOVE the static config.py and provides dynamic overrides.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Configuration file path
CONFIG_DIR = Path(__file__).parent.parent / "config"
BOT_CONFIG_PATH = CONFIG_DIR / "bot_settings.json"


@dataclass
class DiscordSettings:
    """Discord-related configuration."""
    server_id: int = 0
    bots_channel_id: int = 0
    bots_office_channel_id: int = 0
    schemes_dreams_channel_id: int = 0
    partners_channel_name: str = "weekly-meeting-threads"
    ops_channel_name: str = ""
    allowed_channel_ids: List[int] = field(default_factory=list)


@dataclass
class PartnerSettings:
    """Partner/team configuration."""
    partner_role_ids: List[int] = field(default_factory=list)
    default_action_item_owner_user_id: Optional[int] = None
    overdue_backup_user_id: Optional[int] = None
    partner_update_max_per_day: int = 1
    partner_update_lookback_days: int = 7
    partner_update_max_surface: int = 15


@dataclass  
class PMAutomationSettings:
    """Project management automation settings."""
    wip_limit_in_progress: int = 3
    daily_top3_max_items: int = 3
    daily_top3_lookback_limit: int = 150
    daily_top3_weekdays_only: bool = True
    auto_assign_unowned_max_per_run: int = 2
    overdue_followup_max_per_owner_per_run: int = 1


@dataclass
class ActionItemSettings:
    """Action item behavior settings."""
    stale_untouched_days: int = 14
    abandoned_unassigned_cancel_days: int = 30
    overdue_followup_cooldown_days: int = 7
    overdue_followup_max_per_run: int = 1


@dataclass
class GamificationSettings:
    """Points and gamification settings."""
    points_action_done: int = 3
    points_action_done_resolves_gap_bonus: int = 2
    points_gap_resolved_interview: int = 5
    points_bounty_claim_bonus: int = 3


@dataclass
class ScoutSettings:
    """Scout persona settings."""
    max_urls_per_run: int = 10
    max_seen_urls: int = 500
    max_seed_queue: int = 50
    enabled: bool = True


@dataclass
class StewardSettings:
    """Steward persona settings."""
    stale_gap_days: int = 14
    recurring_question_threshold: int = 3
    enabled: bool = True


@dataclass
class DreamerSettings:
    """Dreamer persona settings."""
    enabled: bool = True
    creativity_level: str = "moderate"  # low, moderate, high


@dataclass
class RainmakerSettings:
    """Rainmaker persona settings."""
    enabled: bool = True
    lead_followup_days: int = 7


@dataclass
class CuratorSettings:
    """Curator persona settings."""
    enabled: bool = True


@dataclass
class KnowledgeGapSettings:
    """Knowledge gap tracking settings."""
    bounty_threshold_times_asked: int = 5
    bounty_post_cooldown_days: int = 7
    bounty_max_per_run: int = 1
    bounty_board_channel_name: str = "bounty-board"


@dataclass
class LLMSettings:
    """LLM response tuning settings."""
    temperature: float = 0.3
    max_tokens: int = 4000
    retrieval_top_k: int = 8
    context_window: int = 16000
    streaming_enabled: bool = True


@dataclass
class ThemeSettings:
    """UI colour-scheme settings (adjustable from the admin Settings page)."""
    ink: str = "#2B2622"        # near-black warm neutral
    canvas: str = "#F9DEA2"     # cream / warm light neutral
    accent_a: str = "#EFAAC4"   # blush pink
    accent_b: str = "#7EA3CC"   # dusty cornflower
    accent_c: str = "#83781B"   # olive gold
    accessibility_mode: bool = False


@dataclass
class ScheduleSettings:
    """Scheduled task settings."""
    daily_standup_enabled: bool = True
    daily_standup_hour: int = 9
    daily_standup_minute: int = 0
    weekly_summary_enabled: bool = True
    weekly_summary_day: int = 4  # Thursday
    weekly_summary_hour: int = 16
    persona_meetings_enabled: bool = False  # M0: default OFF per product roadmap
    timezone: str = "America/New_York"


@dataclass
class BotSettings:
    """Complete bot configuration."""
    discord: DiscordSettings = field(default_factory=DiscordSettings)
    partners: PartnerSettings = field(default_factory=PartnerSettings)
    pm_automation: PMAutomationSettings = field(default_factory=PMAutomationSettings)
    action_items: ActionItemSettings = field(default_factory=ActionItemSettings)
    gamification: GamificationSettings = field(default_factory=GamificationSettings)
    scout: ScoutSettings = field(default_factory=ScoutSettings)
    steward: StewardSettings = field(default_factory=StewardSettings)
    dreamer: DreamerSettings = field(default_factory=DreamerSettings)
    rainmaker: RainmakerSettings = field(default_factory=RainmakerSettings)
    curator: CuratorSettings = field(default_factory=CuratorSettings)
    knowledge_gaps: KnowledgeGapSettings = field(default_factory=KnowledgeGapSettings)
    llm_settings: LLMSettings = field(default_factory=LLMSettings)
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    theme: ThemeSettings = field(default_factory=ThemeSettings)
    
    # Metadata
    last_modified: str = ""
    version: str = "1.0.0"


class BotConfigManager:
    """Manages bot configuration with persistence."""
    
    def __init__(self):
        self._settings: Optional[BotSettings] = None
        self._load()
    
    def _load(self) -> None:
        """Load settings from file or create defaults."""
        if BOT_CONFIG_PATH.exists():
            try:
                with open(BOT_CONFIG_PATH, 'r') as f:
                    data = json.load(f)
                self._settings = self._dict_to_settings(data)
                logger.info(f"Loaded bot config from {BOT_CONFIG_PATH}")
            except Exception as e:
                logger.warning(f"Failed to load bot config: {e}, using defaults")
                self._settings = BotSettings()
        else:
            logger.info("No bot config found, using defaults")
            self._settings = BotSettings()
    
    def _dict_to_settings(self, data: Dict) -> BotSettings:
        """Convert dictionary to BotSettings dataclass."""
        settings = BotSettings()
        
        if 'discord' in data:
            settings.discord = DiscordSettings(**data['discord'])
        if 'partners' in data:
            settings.partners = PartnerSettings(**data['partners'])
        if 'pm_automation' in data:
            settings.pm_automation = PMAutomationSettings(**data['pm_automation'])
        if 'action_items' in data:
            settings.action_items = ActionItemSettings(**data['action_items'])
        if 'gamification' in data:
            settings.gamification = GamificationSettings(**data['gamification'])
        if 'scout' in data:
            settings.scout = ScoutSettings(**data['scout'])
        if 'steward' in data:
            settings.steward = StewardSettings(**data['steward'])
        if 'dreamer' in data:
            settings.dreamer = DreamerSettings(**data['dreamer'])
        if 'rainmaker' in data:
            settings.rainmaker = RainmakerSettings(**data['rainmaker'])
        if 'curator' in data:
            settings.curator = CuratorSettings(**data['curator'])
        if 'knowledge_gaps' in data:
            settings.knowledge_gaps = KnowledgeGapSettings(**data['knowledge_gaps'])
        if 'llm_settings' in data:
            settings.llm_settings = LLMSettings(**data['llm_settings'])
        if 'schedule' in data:
            settings.schedule = ScheduleSettings(**data['schedule'])
        if 'theme' in data:
            settings.theme = ThemeSettings(**data['theme'])
        
        settings.last_modified = data.get('last_modified', '')
        settings.version = data.get('version', '1.0.0')
        
        return settings
    
    def _settings_to_dict(self) -> Dict:
        """Convert settings to dictionary for JSON serialization."""
        return {
            'discord': asdict(self._settings.discord),
            'partners': asdict(self._settings.partners),
            'pm_automation': asdict(self._settings.pm_automation),
            'action_items': asdict(self._settings.action_items),
            'gamification': asdict(self._settings.gamification),
            'scout': asdict(self._settings.scout),
            'steward': asdict(self._settings.steward),
            'dreamer': asdict(self._settings.dreamer),
            'rainmaker': asdict(self._settings.rainmaker),
            'curator': asdict(self._settings.curator),
            'knowledge_gaps': asdict(self._settings.knowledge_gaps),
            'llm_settings': asdict(self._settings.llm_settings),
            'schedule': asdict(self._settings.schedule),
            'theme': asdict(self._settings.theme),
            'last_modified': self._settings.last_modified,
            'version': self._settings.version,
        }
    
    def save(self) -> bool:
        """Save current settings to file."""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self._settings.last_modified = datetime.utcnow().isoformat() + "Z"
            
            with open(BOT_CONFIG_PATH, 'w') as f:
                json.dump(self._settings_to_dict(), f, indent=2)
            
            logger.info(f"Saved bot config to {BOT_CONFIG_PATH}")
            return True
        except Exception as e:
            logger.error(f"Failed to save bot config: {e}")
            return False
    
    @property
    def settings(self) -> BotSettings:
        """Get current settings."""
        return self._settings
    
    def get_all(self) -> Dict:
        """Get all settings as dictionary."""
        return self._settings_to_dict()
    
    def get_section(self, section: str) -> Optional[Dict]:
        """Get a specific section of settings."""
        data = self._settings_to_dict()
        return data.get(section)
    
    def update_section(self, section: str, values: Dict) -> bool:
        """Update a specific section of settings."""
        try:
            if section == 'discord':
                for key, value in values.items():
                    if hasattr(self._settings.discord, key):
                        setattr(self._settings.discord, key, value)
            elif section == 'partners':
                for key, value in values.items():
                    if hasattr(self._settings.partners, key):
                        setattr(self._settings.partners, key, value)
            elif section == 'pm_automation':
                for key, value in values.items():
                    if hasattr(self._settings.pm_automation, key):
                        setattr(self._settings.pm_automation, key, value)
            elif section == 'action_items':
                for key, value in values.items():
                    if hasattr(self._settings.action_items, key):
                        setattr(self._settings.action_items, key, value)
            elif section == 'gamification':
                for key, value in values.items():
                    if hasattr(self._settings.gamification, key):
                        setattr(self._settings.gamification, key, value)
            elif section == 'scout':
                for key, value in values.items():
                    if hasattr(self._settings.scout, key):
                        setattr(self._settings.scout, key, value)
            elif section == 'steward':
                for key, value in values.items():
                    if hasattr(self._settings.steward, key):
                        setattr(self._settings.steward, key, value)
            elif section == 'dreamer':
                for key, value in values.items():
                    if hasattr(self._settings.dreamer, key):
                        setattr(self._settings.dreamer, key, value)
            elif section == 'rainmaker':
                for key, value in values.items():
                    if hasattr(self._settings.rainmaker, key):
                        setattr(self._settings.rainmaker, key, value)
            elif section == 'curator':
                for key, value in values.items():
                    if hasattr(self._settings.curator, key):
                        setattr(self._settings.curator, key, value)
            elif section == 'knowledge_gaps':
                for key, value in values.items():
                    if hasattr(self._settings.knowledge_gaps, key):
                        setattr(self._settings.knowledge_gaps, key, value)
            elif section == 'llm_settings':
                for key, value in values.items():
                    if hasattr(self._settings.llm_settings, key):
                        setattr(self._settings.llm_settings, key, value)
            elif section == 'schedule':
                for key, value in values.items():
                    if hasattr(self._settings.schedule, key):
                        setattr(self._settings.schedule, key, value)
            elif section == 'theme':
                for key, value in values.items():
                    if hasattr(self._settings.theme, key):
                        setattr(self._settings.theme, key, value)
            else:
                logger.warning(f"Unknown config section: {section}")
                return False
            
            return self.save()
        except Exception as e:
            logger.error(f"Failed to update section {section}: {e}")
            return False
    
    def reset_section(self, section: str) -> bool:
        """Reset a section to defaults."""
        try:
            if section == 'discord':
                self._settings.discord = DiscordSettings()
            elif section == 'partners':
                self._settings.partners = PartnerSettings()
            elif section == 'pm_automation':
                self._settings.pm_automation = PMAutomationSettings()
            elif section == 'action_items':
                self._settings.action_items = ActionItemSettings()
            elif section == 'gamification':
                self._settings.gamification = GamificationSettings()
            elif section == 'scout':
                self._settings.scout = ScoutSettings()
            elif section == 'steward':
                self._settings.steward = StewardSettings()
            elif section == 'dreamer':
                self._settings.dreamer = DreamerSettings()
            elif section == 'rainmaker':
                self._settings.rainmaker = RainmakerSettings()
            elif section == 'curator':
                self._settings.curator = CuratorSettings()
            elif section == 'knowledge_gaps':
                self._settings.knowledge_gaps = KnowledgeGapSettings()
            elif section == 'llm_settings':
                self._settings.llm_settings = LLMSettings()
            elif section == 'schedule':
                self._settings.schedule = ScheduleSettings()
            elif section == 'theme':
                self._settings.theme = ThemeSettings()
            else:
                return False
            
            return self.save()
        except Exception as e:
            logger.error(f"Failed to reset section {section}: {e}")
            return False
    
    def reset_all(self) -> bool:
        """Reset all settings to defaults."""
        try:
            self._settings = BotSettings()
            return self.save()
        except Exception as e:
            logger.error(f"Failed to reset all settings: {e}")
            return False


# Global instance
_config_manager: Optional[BotConfigManager] = None


def get_bot_config_manager() -> BotConfigManager:
    """Get or create the global config manager."""
    global _config_manager
    if _config_manager is None:
        _config_manager = BotConfigManager()
    return _config_manager


# Section metadata for UI display
CONFIG_SECTIONS = {
    'discord': {
        'name': 'Discord Configuration',
        'icon': '💬',
        'description': 'Discord server and channel settings',
        'fields': {
            'server_id': {'type': 'number', 'label': 'Server ID', 'description': 'Discord server/guild ID'},
            'bots_channel_id': {'type': 'number', 'label': 'Bots Channel ID', 'description': 'Channel for bot operational updates'},
            'bots_office_channel_id': {'type': 'number', 'label': 'Bots Office Channel ID', 'description': 'Backoffice channel for persona chatter'},
            'schemes_dreams_channel_id': {'type': 'number', 'label': 'Schemes & Dreams Channel ID', 'description': 'Channel for Dreamer wild escalations'},
            'partners_channel_name': {'type': 'text', 'label': 'Partners Channel Name', 'description': 'For partner-facing meeting coordination'},
            'ops_channel_name': {'type': 'text', 'label': 'Ops Channel Name', 'description': 'For daily operational pings'},
            'allowed_channel_ids': {'type': 'list', 'label': 'Allowed Channel IDs', 'description': 'Channels where bot can respond'},
        }
    },
    'partners': {
        'name': 'Partner Settings',
        'icon': '👥',
        'description': 'Team member and partner configuration',
        'fields': {
            'partner_role_ids': {'type': 'list', 'label': 'Partner Role IDs', 'description': 'Discord role IDs for partners'},
            'default_action_item_owner_user_id': {'type': 'number', 'label': 'Default Action Item Owner', 'description': 'User ID for auto-assigning unowned items'},
            'overdue_backup_user_id': {'type': 'number', 'label': 'Overdue Backup User', 'description': 'User to mention before escalating'},
            'partner_update_max_per_day': {'type': 'number', 'label': 'Updates Max Per Day', 'description': 'Maximum partner updates per day'},
            'partner_update_lookback_days': {'type': 'number', 'label': 'Update Lookback Days', 'description': 'Days to look back for updates'},
            'partner_update_max_surface': {'type': 'number', 'label': 'Max Updates to Surface', 'description': 'Maximum updates to show'},
        }
    },
    'pm_automation': {
        'name': 'PM Automation',
        'icon': '📋',
        'description': 'Project management automation settings',
        'fields': {
            'wip_limit_in_progress': {'type': 'number', 'label': 'WIP Limit', 'description': 'Maximum items in progress'},
            'daily_top3_max_items': {'type': 'number', 'label': 'Daily Top 3 Max', 'description': 'Maximum items in daily summary'},
            'daily_top3_lookback_limit': {'type': 'number', 'label': 'Lookback Limit', 'description': 'Items to consider for Top 3'},
            'daily_top3_weekdays_only': {'type': 'boolean', 'label': 'Weekdays Only', 'description': 'Only run on weekdays'},
            'auto_assign_unowned_max_per_run': {'type': 'number', 'label': 'Auto-assign Max', 'description': 'Max items to auto-assign per run'},
            'overdue_followup_max_per_owner_per_run': {'type': 'number', 'label': 'Overdue Followup Max', 'description': 'Max followups per owner per run'},
        }
    },
    'action_items': {
        'name': 'Action Items',
        'icon': '✅',
        'description': 'Action item behavior and staleness settings',
        'fields': {
            'stale_untouched_days': {'type': 'number', 'label': 'Stale After Days', 'description': 'Days until item is considered stale'},
            'abandoned_unassigned_cancel_days': {'type': 'number', 'label': 'Cancel Unassigned After', 'description': 'Days until unassigned items are cancelled'},
            'overdue_followup_cooldown_days': {'type': 'number', 'label': 'Followup Cooldown Days', 'description': 'Days between followup reminders'},
            'overdue_followup_max_per_run': {'type': 'number', 'label': 'Max Followups Per Run', 'description': 'Maximum followups per scheduled run'},
        }
    },
    'gamification': {
        'name': 'Gamification',
        'icon': '🎮',
        'description': 'Points and rewards system',
        'fields': {
            'points_action_done': {'type': 'number', 'label': 'Points per Completed Action', 'description': 'Points awarded for completing an action'},
            'points_action_done_resolves_gap_bonus': {'type': 'number', 'label': 'Gap Resolution Bonus', 'description': 'Bonus points if action resolves a gap'},
            'points_gap_resolved_interview': {'type': 'number', 'label': 'Interview Resolution Points', 'description': 'Points for resolving via interview'},
            'points_bounty_claim_bonus': {'type': 'number', 'label': 'Bounty Claim Bonus', 'description': 'Bonus points for claiming bounties'},
        }
    },
    'scout': {
        'name': 'Scout Persona',
        'icon': '🔭',
        'description': 'Web research and opportunity discovery',
        'fields': {
            'enabled': {'type': 'boolean', 'label': 'Enabled', 'description': 'Enable/disable Scout persona'},
            'max_urls_per_run': {'type': 'number', 'label': 'Max URLs Per Run', 'description': 'Maximum URLs to process per run'},
            'max_seen_urls': {'type': 'number', 'label': 'Max Seen URLs', 'description': 'Maximum URLs to track as seen'},
            'max_seed_queue': {'type': 'number', 'label': 'Max Seed Queue', 'description': 'Maximum URLs in seed queue'},
        }
    },
    'steward': {
        'name': 'Steward Persona',
        'icon': '🛡️',
        'description': 'Self-monitoring and health checks',
        'fields': {
            'enabled': {'type': 'boolean', 'label': 'Enabled', 'description': 'Enable/disable Steward persona'},
            'stale_gap_days': {'type': 'number', 'label': 'Stale Gap Days', 'description': 'Days until gap is considered stale'},
            'recurring_question_threshold': {'type': 'number', 'label': 'Recurring Question Threshold', 'description': 'Times asked before flagging'},
        }
    },
    'dreamer': {
        'name': 'Dreamer Persona',
        'icon': '💭',
        'description': 'Ideation and blue-sky exploration',
        'fields': {
            'enabled': {'type': 'boolean', 'label': 'Enabled', 'description': 'Enable/disable Dreamer persona'},
            'creativity_level': {'type': 'select', 'label': 'Creativity Level', 'description': 'How wild should ideas be?', 'options': ['low', 'moderate', 'high']},
        }
    },
    'rainmaker': {
        'name': 'Rainmaker Persona',
        'icon': '💰',
        'description': 'Lead management and sales pipeline',
        'fields': {
            'enabled': {'type': 'boolean', 'label': 'Enabled', 'description': 'Enable/disable Rainmaker persona'},
            'lead_followup_days': {'type': 'number', 'label': 'Lead Followup Days', 'description': 'Days before lead followup reminder'},
        }
    },
    'curator': {
        'name': 'Curator Persona',
        'icon': '📚',
        'description': 'Corpus quality, auto-synthesis, and knowledge expansion',
        'fields': {
            'enabled': {'type': 'boolean', 'label': 'Enabled', 'description': 'Enable/disable Curator persona'},
        }
    },
    'knowledge_gaps': {
        'name': 'Knowledge Gaps',
        'icon': '🎯',
        'description': 'Knowledge gap tracking and bounty board',
        'fields': {
            'bounty_threshold_times_asked': {'type': 'number', 'label': 'Bounty Threshold', 'description': 'Times asked before becoming bounty'},
            'bounty_post_cooldown_days': {'type': 'number', 'label': 'Bounty Post Cooldown', 'description': 'Days between bounty posts'},
            'bounty_max_per_run': {'type': 'number', 'label': 'Max Bounties Per Run', 'description': 'Maximum bounties to post per run'},
            'bounty_board_channel_name': {'type': 'text', 'label': 'Bounty Board Channel', 'description': 'Channel name for bounty board'},
        }
    },
    'llm_settings': {
        'name': 'LLM Response Tuning',
        'icon': '🧠',
        'description': 'Language model response parameters',
        'fields': {
            'temperature': {'type': 'number', 'label': 'Temperature', 'description': 'Randomness (0.0-1.0). Lower = more focused, higher = more creative', 'step': 0.1, 'min': 0, 'max': 1},
            'max_tokens': {'type': 'number', 'label': 'Max Tokens', 'description': 'Maximum response length in tokens'},
            'retrieval_top_k': {'type': 'number', 'label': 'Retrieval Top K', 'description': 'Number of documents to retrieve from knowledge base'},
            'context_window': {'type': 'number', 'label': 'Context Window', 'description': 'Maximum context size in tokens'},
            'streaming_enabled': {'type': 'boolean', 'label': 'Streaming Enabled', 'description': 'Stream responses as they generate'},
        }
    },
    'schedule': {
        'name': 'Scheduled Tasks',
        'icon': '⏰',
        'description': 'Timing for automated tasks',
        'fields': {
            'daily_standup_enabled': {'type': 'boolean', 'label': 'Daily Standup Enabled', 'description': 'Enable daily standup summaries'},
            'daily_standup_hour': {'type': 'number', 'label': 'Standup Hour (0-23)', 'description': 'Hour for daily standup (24h format)'},
            'daily_standup_minute': {'type': 'number', 'label': 'Standup Minute (0-59)', 'description': 'Minute for daily standup'},
            'weekly_summary_enabled': {'type': 'boolean', 'label': 'Weekly Summary Enabled', 'description': 'Enable weekly summary reports'},
            'weekly_summary_day': {'type': 'number', 'label': 'Weekly Summary Day (0-6)', 'description': '0=Monday, 4=Thursday, 6=Sunday'},
            'weekly_summary_hour': {'type': 'number', 'label': 'Weekly Summary Hour', 'description': 'Hour for weekly summary (24h format)'},
            'timezone': {'type': 'text', 'label': 'Timezone', 'description': 'Timezone for scheduled tasks (e.g., America/New_York)'},
        }
    },
    'theme': {
        'name': 'Theme / Colors',
        'icon': '🎨',
        'description': 'UI colour scheme — guided palette with accessibility mode',
        'fields': {
            'ink': {'type': 'color', 'label': 'Ink (dark neutral)', 'description': 'Near-black base for text & deep surfaces'},
            'canvas': {'type': 'color', 'label': 'Canvas (light neutral)', 'description': 'Warm light colour for headings & highlights'},
            'accent_a': {'type': 'color', 'label': 'Accent A (primary)', 'description': 'Primary accent — links, active states, CTA'},
            'accent_b': {'type': 'color', 'label': 'Accent B (cool)', 'description': 'Cool secondary accent colour'},
            'accent_c': {'type': 'color', 'label': 'Accent C (support)', 'description': 'Support colour for badges & emphasis'},
            'accessibility_mode': {'type': 'bool', 'label': 'Accessibility Mode', 'description': 'High-contrast two-colour scheme for guaranteed legibility'},
        }
    },
}
