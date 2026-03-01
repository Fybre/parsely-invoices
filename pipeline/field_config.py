"""
Field configuration manager for invoice fields.

Loads and provides access to field metadata from:
- config/standard_fields.json (standard invoice fields)
- config/custom_fields.json (custom fields)

Each field can have:
- mandatory (bool): Field must have a value before export
- hidden (bool): Field is not shown in the dashboard UI
"""
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FieldConfig:
    """Configuration for a single field."""
    name: str
    mandatory: bool = False
    hidden: bool = False
    source: str = "llm"
    source_config: dict = field(default_factory=dict)


class FieldConfigManager:
    """Manages field configuration from JSON files."""

    def __init__(self, config_dir: Optional[Path] = None):
        self.config_dir = config_dir or Path(__file__).parent.parent / "config"
        self._standard_fields: dict[str, FieldConfig] = {}
        self._custom_fields: dict[str, FieldConfig] = {}
        self._load_configs()

    def _load_configs(self) -> None:
        """Load both standard and custom field configurations."""
        self._standard_fields = self._load_standard_fields()
        self._custom_fields = self._load_custom_fields()
        logger.debug(
            "Loaded %d standard fields, %d custom fields",
            len(self._standard_fields),
            len(self._custom_fields),
        )

    def _load_standard_fields(self) -> dict[str, FieldConfig]:
        """Load standard field configuration."""
        config_file = self.config_dir / "standard_fields.json"
        if not config_file.exists():
            logger.warning("Standard fields config not found: %s", config_file)
            return {}

        try:
            with open(config_file, encoding="utf-8") as f:
                data = json.load(f)

            fields = {}
            for field_def in data.get("fields", []):
                name = field_def.get("name")
                if not name:
                    continue
                fields[name] = FieldConfig(
                    name=name,
                    mandatory=field_def.get("mandatory", False),
                    hidden=field_def.get("hidden", False),
                    source=field_def.get("source", "llm"),
                    source_config=field_def.get("source_config", {}),
                )
            return fields
        except Exception as e:
            logger.error("Failed to load standard fields config: %s", e)
            return {}

    def _load_custom_fields(self) -> dict[str, FieldConfig]:
        """Load custom field configuration."""
        config_file = self.config_dir / "custom_fields.json"
        if not config_file.exists():
            return {}

        try:
            with open(config_file, encoding="utf-8") as f:
                data = json.load(f)

            fields = {}
            for field_def in data.get("fields", []):
                name = field_def.get("name")
                if not name:
                    continue
                fields[name] = FieldConfig(
                    name=name,
                    mandatory=field_def.get("mandatory", False),
                    hidden=field_def.get("hidden", False),
                    source=field_def.get("source", "llm"),
                    source_config=field_def.get("source_config", {}),
                )
            return fields
        except Exception as e:
            logger.error("Failed to load custom fields config: %s", e)
            return {}

    def get_field(self, name: str) -> FieldConfig:
        """
        Get configuration for a field.
        
        Checks custom fields first (they can override standard fields),
        then standard fields. Returns defaults if not found.
        """
        if name in self._custom_fields:
            return self._custom_fields[name]
        if name in self._standard_fields:
            return self._standard_fields[name]
        return FieldConfig(name=name)

    def is_mandatory(self, name: str) -> bool:
        """Check if a field is mandatory."""
        return self.get_field(name).mandatory

    def is_hidden(self, name: str) -> bool:
        """Check if a field is hidden."""
        return self.get_field(name).hidden

    def get_all_mandatory_fields(self) -> list[str]:
        """Get list of all mandatory field names."""
        mandatory = []
        # Standard fields
        for name, config in self._standard_fields.items():
            if config.mandatory:
                mandatory.append(name)
        # Custom fields (avoid duplicates)
        for name, config in self._custom_fields.items():
            if config.mandatory and name not in mandatory:
                mandatory.append(name)
        return mandatory

    def get_visible_fields(self) -> list[str]:
        """Get list of all non-hidden field names."""
        visible = []
        # Standard fields
        for name, config in self._standard_fields.items():
            if not config.hidden:
                visible.append(name)
        # Custom fields
        for name, config in self._custom_fields.items():
            if not config.hidden and name not in visible:
                visible.append(name)
        return visible


# Global instance for reuse
_field_config_manager: Optional[FieldConfigManager] = None


def get_field_config() -> FieldConfigManager:
    """Get the global field config manager instance."""
    global _field_config_manager
    if _field_config_manager is None:
        _field_config_manager = FieldConfigManager()
    return _field_config_manager


def reload_field_config() -> FieldConfigManager:
    """Reload field configuration and return new manager."""
    global _field_config_manager
    _field_config_manager = FieldConfigManager()
    return _field_config_manager
