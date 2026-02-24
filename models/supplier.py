from pydantic import BaseModel
from typing import Optional, List


class Supplier(BaseModel):
    """
    A known supplier from the supplier master list.
    aliases is a list of alternative names / trading names used for fuzzy matching.
    """
    id: str
    name: str
    abn: Optional[str] = None          # Normalised: digits only, e.g. "12345678901"
    acn: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    aliases: List[str] = []            # Alternative names / trading names

    @property
    def abn_normalised(self) -> Optional[str]:
        """Return ABN as digits only (no spaces or punctuation)."""
        if self.abn:
            return "".join(c for c in self.abn if c.isdigit())
        return None

    @property
    def all_names(self) -> List[str]:
        """Return the canonical name plus all aliases for matching."""
        return [self.name] + self.aliases
