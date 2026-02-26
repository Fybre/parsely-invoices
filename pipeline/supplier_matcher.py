"""
Supplier matching module.

Attempts to identify the invoice supplier against a known supplier master list
using multiple strategies in priority order:
  1. ABN exact match
  2. Name exact match (case-insensitive)
  3. Fuzzy name match (using rapidfuzz)
  4. Email domain match
"""
import csv
import logging
import re
from pathlib import Path
from typing import Optional

from models.supplier import Supplier
from models.invoice import ExtractedInvoice
from models.result import MatchedSupplier

logger = logging.getLogger(__name__)

# Minimum fuzzy score (0-100) to accept a name match
FUZZY_THRESHOLD = 75


def _normalise_abn(abn: Optional[str]) -> Optional[str]:
    """Strip all non-digits from an ABN/ACN string."""
    if not abn:
        return None
    digits = re.sub(r"\D", "", abn)
    return digits if digits else None


def _email_domain(email: Optional[str]) -> Optional[str]:
    if email and "@" in email:
        return email.split("@", 1)[1].lower()
    return None


class SupplierMatcher:
    """
    Loads a supplier master list from CSV and matches invoice supplier info.

    CSV format (suppliers.csv):
      id, name, abn, acn, email, phone, address, aliases
      aliases: pipe-separated alternative names, e.g. "ACME Corp|ACME Pty Ltd"
    """

    def __init__(self, suppliers_csv: str | Path):
        self.suppliers: list[Supplier] = []
        self._load(Path(suppliers_csv))

    def _load(self, path: Path) -> None:
        if not path.exists():
            logger.warning("Suppliers CSV not found: %s — supplier matching disabled", path)
            return
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                aliases_raw = row.get("aliases", "")
                aliases = [a.strip() for a in aliases_raw.split("|") if a.strip()]
                supplier = Supplier(
                    id=row["id"].strip(),
                    name=row["name"].strip(),
                    abn=_normalise_abn(row.get("abn")),
                    acn=_normalise_abn(row.get("acn")),
                    email=(row.get("email") or "").strip() or None,
                    phone=(row.get("phone") or "").strip() or None,
                    address=(row.get("address") or "").strip() or None,
                    aliases=aliases,
                )
                self.suppliers.append(supplier)
        logger.info("Loaded %d suppliers from %s", len(self.suppliers), path.name)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def match(self, invoice: ExtractedInvoice) -> Optional[MatchedSupplier]:
        """
        Try all matching strategies and return the best MatchedSupplier,
        or None if no supplier could be identified.
        """
        if not self.suppliers:
            return None

        supplier_info = invoice.supplier
        if not supplier_info:
            logger.debug("Invoice has no supplier block — skipping supplier match")
            return None

        # 1. ABN exact match (most reliable)
        invoice_abn = _normalise_abn(supplier_info.abn or supplier_info.acn)
        if invoice_abn:
            for s in self.suppliers:
                if s.abn_normalised and s.abn_normalised == invoice_abn:
                    logger.info("Supplier matched by ABN: %s -> %s", invoice_abn, s.name)
                    return MatchedSupplier(
                        supplier_id=s.id,
                        supplier_name=s.name,
                        match_method="abn_exact",
                        confidence=1.0,
                        abn=s.abn,
                        matched_on={"field": "abn", "value": supplier_info.abn or supplier_info.acn},
                    )

        # 2. Name exact match (case-insensitive)
        invoice_name = (supplier_info.name or "").strip().lower()
        if invoice_name:
            for s in self.suppliers:
                if any(n.lower() == invoice_name for n in s.all_names):
                    logger.info("Supplier matched by exact name: %s", s.name)
                    return MatchedSupplier(
                        supplier_id=s.id,
                        supplier_name=s.name,
                        match_method="name_exact",
                        confidence=0.95,
                        abn=s.abn,
                        matched_on={"field": "name", "value": supplier_info.name},
                    )

        # 3. Fuzzy name match
        if invoice_name:
            best = self._fuzzy_name_match(invoice_name)
            if best:
                return best

        # 4. Email domain match
        invoice_domain = _email_domain(supplier_info.email)
        if invoice_domain:
            for s in self.suppliers:
                s_domain = _email_domain(s.email)
                if s_domain and s_domain == invoice_domain:
                    logger.info("Supplier matched by email domain: %s -> %s", invoice_domain, s.name)
                    return MatchedSupplier(
                        supplier_id=s.id,
                        supplier_name=s.name,
                        match_method="email_domain",
                        confidence=0.7,
                        abn=s.abn,
                        matched_on={"field": "email_domain", "value": invoice_domain},
                    )

        logger.info("No supplier match found for: %s (ABN: %s)", supplier_info.name, invoice_abn)
        return None

    def _fuzzy_name_match(self, invoice_name: str) -> Optional[MatchedSupplier]:
        """Use rapidfuzz to find the best name match above FUZZY_THRESHOLD."""
        try:
            from rapidfuzz import fuzz
        except ImportError:
            logger.warning("rapidfuzz not installed — fuzzy supplier matching disabled. "
                           "Run: pip install rapidfuzz")
            return None

        best_score = 0
        best_supplier: Optional[Supplier] = None

        for s in self.suppliers:
            for candidate_name in s.all_names:
                score = fuzz.token_sort_ratio(invoice_name, candidate_name.lower())
                if score > best_score:
                    best_score = score
                    best_supplier = s

        if best_supplier and best_score >= FUZZY_THRESHOLD:
            confidence = best_score / 100.0
            logger.info(
                "Supplier fuzzy matched: '%s' -> '%s' (score=%d)",
                invoice_name, best_supplier.name, best_score,
            )
            return MatchedSupplier(
                supplier_id=best_supplier.id,
                supplier_name=best_supplier.name,
                match_method="name_fuzzy",
                confidence=confidence,
                abn=best_supplier.abn,
                matched_on={"field": "name", "value": invoice_name, "fuzzy_score": confidence},
            )

        logger.debug("Best fuzzy match score was %d (threshold=%d)", best_score, FUZZY_THRESHOLD)
        return None
