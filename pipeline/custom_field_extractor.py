"""
Custom field extraction for invoice processing.

Loads field definitions from config/custom_fields.json and applies
up to three extraction strategies per field (in priority order):

  1. regex      — Python regex with one capture group; runs against the raw
                  markdown text.  Fast and deterministic.  Takes precedence
                  over LLM results when it matches.
  2. table_keys — column header synonyms to scan in structured table data.
  3. llm_hint   — plain-English description injected into the LLM prompt so
                  the model can locate the value in freeform text.

Only fields whose value is actually found (non-null) are included in the
output dict.  Missing or malformed entries in the config are skipped with
a warning — the pipeline continues normally.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CustomField:
    name: str                         # key in the output JSON  (e.g. "strata_reference")
    label: str                        # human-readable label    (e.g. "Strata Reference")
    regex: Optional[str] = None       # Python regex with ONE capture group
    table_keys: list[str] = field(default_factory=list)  # lowercase column synonyms
    llm_hint: Optional[str] = None    # description for the LLM prompt

    # Compiled regex (populated lazily)
    _re: Optional[re.Pattern] = field(default=None, repr=False, compare=False)

    def compiled_re(self) -> Optional[re.Pattern]:
        if self.regex and self._re is None:
            try:
                self._re = re.compile(self.regex, re.IGNORECASE)
            except re.error as exc:
                logger.warning("Custom field %r has invalid regex (%s) — skipping regex", self.name, exc)
        return self._re


def load_custom_fields(config_dir: Optional[Path] = None) -> list[CustomField]:
    """
    Load custom field definitions from custom_fields.json.
    Returns an empty list if the file is absent or contains no valid entries.
    """
    if config_dir is None:
        config_dir = Path(os.environ.get(
            "CONFIG_DIR",
            Path(__file__).parent.parent / "config"
        ))
    config_path = config_dir / "custom_fields.json"

    if not config_path.exists():
        return []

    try:
        with open(config_path) as fh:
            raw = json.load(fh)
    except Exception as exc:
        logger.warning("Could not load custom_fields.json (%s) — no custom fields", exc)
        return []

    if not isinstance(raw, list):
        logger.warning("custom_fields.json must be a JSON array — no custom fields loaded")
        return []

    fields: list[CustomField] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        # Skip comment-only entries
        if "name" not in entry or "label" not in entry:
            continue
        try:
            cf = CustomField(
                name=str(entry["name"]),
                label=str(entry["label"]),
                regex=entry.get("regex") or None,
                table_keys=[str(k).lower() for k in entry.get("table_keys", [])],
                llm_hint=entry.get("llm_hint") or None,
            )
            fields.append(cf)
        except Exception as exc:
            logger.warning("Skipping malformed custom field entry %r: %s", entry, exc)

    if fields:
        logger.info("Loaded %d custom field(s): %s", len(fields), [f.name for f in fields])
    return fields


class CustomFieldExtractor:
    """
    Applies regex and table-key strategies to extract custom fields
    from already-parsed invoice data.  LLM-based extraction is handled
    separately by injecting hints into the LLM prompt (see llm_parser.py).
    """

    def __init__(self, fields: list[CustomField]):
        self.fields = fields

    def extract_from_text(self, markdown: str) -> dict[str, str]:
        """Run regex patterns against the raw markdown.  Returns {name: value}."""
        results: dict[str, str] = {}
        for cf in self.fields:
            pattern = cf.compiled_re()
            if pattern is None:
                continue
            m = pattern.search(markdown)
            if m:
                value = m.group(1).strip()
                if value:
                    results[cf.name] = value
                    logger.debug("Custom field %r matched by regex: %r", cf.name, value)
        return results

    def extract_from_tables(self, tables: list[list[dict]]) -> dict[str, str]:
        """Scan table column headers for table_key matches.  Returns {name: value}."""
        results: dict[str, str] = {}
        fields_with_keys = [cf for cf in self.fields if cf.table_keys]
        if not fields_with_keys or not tables:
            return results

        for table in tables:
            if not table:
                continue
            headers = {str(k).lower().strip(): k for k in table[0].keys()}
            for cf in fields_with_keys:
                if cf.name in results:
                    continue  # already found
                for key in cf.table_keys:
                    if key in headers:
                        col = headers[key]
                        # Collect non-empty values from this column
                        values = [
                            str(row[col]).strip()
                            for row in table
                            if row.get(col) and str(row[col]).strip() not in ("", "None")
                        ]
                        if values:
                            results[cf.name] = values[0]
                            logger.debug("Custom field %r matched by table key %r: %r",
                                         cf.name, key, values[0])
                        break
        return results

    def merge(self, *sources: dict[str, str]) -> dict[str, str]:
        """
        Merge multiple extraction result dicts.
        Earlier sources take precedence (regex > table > llm).
        """
        merged: dict[str, str] = {}
        for source in reversed(sources):   # later sources are overridden by earlier ones
            merged.update(source)
        return merged
