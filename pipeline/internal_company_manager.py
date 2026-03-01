"""
Manager for internal company entities (the "Buyer" side).
Used for anchoring the LLM and preventing self-identification as supplier.
"""
import json
import logging
from pathlib import Path
from typing import List

from models.supplier import Supplier

logger = logging.getLogger(__name__)

class InternalCompanyManager:
    """Loads internal company list from JSON."""

    def __init__(self, json_path: str | Path):
        self.companies: List[Supplier] = []
        self._load(Path(json_path))

    def _load(self, path: Path) -> None:
        if not path.exists():
            logger.debug("Internal companies JSON not found: %s", path)
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data:
                    company = Supplier(
                        id=item["id"],
                        name=item["name"],
                        abn=item.get("abn"),
                        acn=item.get("acn"),
                        email=item.get("email"),
                        phone=item.get("phone"),
                        address=item.get("address"),
                        aliases=item.get("aliases", []),
                    )
                    self.companies.append(company)
            logger.info("Loaded %d internal companies from %s", len(self.companies), path.name)
        except Exception as e:
            logger.error("Failed to load internal companies: %s", e)

    def get_all(self) -> List[Supplier]:
        return self.companies

    def is_internal_abn(self, abn: str) -> bool:
        """Check if an ABN belongs to an internal company."""
        if not abn:
            return False
        clean_abn = "".join(c for c in abn if c.isdigit())
        for c in self.companies:
            if c.abn_normalised == clean_abn:
                return True
        return False
