"""
Purchase Order matching module.

Loads POs from two CSV files:
  - purchase_orders.csv      (PO header records)
  - purchase_order_lines.csv (PO line items, linked by po_number)

Matches an invoice to a PO and compares line items using fuzzy description
matching (rapidfuzz) and numeric tolerance checks.
"""
import csv
import logging
import re
from pathlib import Path
from typing import Optional

from models.invoice import ExtractedInvoice, LineItem
from models.purchase_order import PurchaseOrder, POLineItem
from models.result import MatchedPO, POLineMatch

logger = logging.getLogger(__name__)

# Tolerances
PRICE_TOLERANCE_PCT = 0.01      # 1% — treat as matching if within this band
TOTAL_TOLERANCE_ABS = 0.05      # $0.05 rounding tolerance
LINE_FUZZY_THRESHOLD = 65       # minimum score to consider a description match


class POMatcher:
    """
    Loads Purchase Orders from CSV files and matches them against invoices.

    CSV formats:
      purchase_orders.csv:
        po_number, supplier_id, supplier_name, issue_date, expected_delivery,
        subtotal, tax_amount, total, currency, status, notes

      purchase_order_lines.csv:
        po_number, line_number, sku, description, quantity, unit, unit_price, total
    """

    def __init__(self, po_csv: str | Path, po_lines_csv: str | Path):
        self.purchase_orders: dict[str, PurchaseOrder] = {}
        self._load(Path(po_csv), Path(po_lines_csv))

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load(self, po_path: Path, lines_path: Path) -> None:
        if not po_path.exists():
            logger.warning("PO CSV not found: %s — PO matching disabled", po_path)
            return

        # Load PO headers
        with open(po_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                po = PurchaseOrder(
                    po_number=row["po_number"].strip(),
                    supplier_id=(row.get("supplier_id") or "").strip() or None,
                    supplier_name=(row.get("supplier_name") or "").strip() or None,
                    issue_date=(row.get("issue_date") or "").strip() or None,
                    expected_delivery=(row.get("expected_delivery") or "").strip() or None,
                    subtotal=_to_float(row.get("subtotal")),
                    tax_amount=_to_float(row.get("tax_amount")),
                    total=_to_float(row.get("total")),
                    currency=(row.get("currency") or "AUD").strip(),
                    status=(row.get("status") or "").strip() or None,
                    notes=(row.get("notes") or "").strip() or None,
                )
                self.purchase_orders[po.po_number.upper()] = po

        # Load PO lines
        if lines_path.exists():
            with open(lines_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    key = row["po_number"].strip().upper()
                    if key not in self.purchase_orders:
                        logger.warning("PO line references unknown PO: %s", key)
                        continue
                    line = POLineItem(
                        line_number=int(row.get("line_number") or 0) or None,
                        sku=(row.get("sku") or "").strip() or None,
                        description=row["description"].strip(),
                        quantity=float(row["quantity"]),
                        unit=(row.get("unit") or "").strip() or None,
                        unit_price=float(row["unit_price"]),
                        total=float(row["total"]),
                    )
                    self.purchase_orders[key].line_items.append(line)
        else:
            logger.info("No PO lines CSV found at %s — line matching skipped", lines_path)

        logger.info(
            "Loaded %d POs (%d with line items)",
            len(self.purchase_orders),
            sum(1 for po in self.purchase_orders.values() if po.line_items),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def match(self, invoice: ExtractedInvoice) -> Optional[MatchedPO]:
        """
        Attempt to match the invoice to a PO.

        Returns a MatchedPO if a PO reference is found and the PO exists,
        otherwise returns None.
        """
        po_number = _normalise_po_number(invoice.po_number)
        if not po_number:
            logger.debug("Invoice has no PO number — skipping PO match")
            return None

        po = self.purchase_orders.get(po_number.upper())
        if not po:
            logger.info("PO number '%s' not found in loaded POs", po_number)
            return MatchedPO(
                po_number=po_number,
                match_method="invoice_reference",
                # Signal that the PO was referenced but not found
                po_total=None,
            )

        logger.info("Matched invoice PO reference '%s' to loaded PO", po_number)
        line_matches, unmatched_inv, unmatched_po = self._match_lines(
            invoice.line_items, po.line_items
        )

        return MatchedPO(
            po_number=po.po_number,
            match_method="invoice_reference",
            po_total=po.total,
            po_supplier_name=po.supplier_name,
            po_supplier_id=po.supplier_id,
            line_matches=line_matches,
            unmatched_invoice_lines=unmatched_inv,
            unmatched_po_lines=unmatched_po,
        )

    def get_po(self, po_number: str) -> Optional[PurchaseOrder]:
        """Retrieve a PO by number (case-insensitive)."""
        return self.purchase_orders.get(po_number.upper())

    # ------------------------------------------------------------------
    # Line-item matching
    # ------------------------------------------------------------------

    def _match_lines(
        self,
        invoice_lines: list[LineItem],
        po_lines: list[POLineItem],
    ) -> tuple[list[POLineMatch], list[int], list[int]]:
        """
        Match each invoice line to the best available PO line.

        Returns:
          - list of POLineMatch objects
          - indices of unmatched invoice lines
          - line_numbers of unmatched PO lines
        """
        if not po_lines:
            return [], list(range(len(invoice_lines))), []

        matches: list[POLineMatch] = []
        used_po_indices: set[int] = set()
        unmatched_invoice: list[int] = []

        for inv_idx, inv_line in enumerate(invoice_lines):
            best_match = self._find_best_po_line(inv_line, po_lines, used_po_indices)
            if best_match is not None:
                po_idx, po_line, score = best_match
                used_po_indices.add(po_idx)
                lm = self._build_line_match(inv_idx, inv_line, po_idx, po_line, score)
                matches.append(lm)
            else:
                unmatched_invoice.append(inv_idx)
                matches.append(POLineMatch(
                    invoice_line_index=inv_idx,
                    invoice_description=inv_line.description,
                    matched=False,
                ))

        all_po_indices = set(range(len(po_lines)))
        unmatched_po_nums = [
            po_lines[i].line_number or (i + 1)
            for i in sorted(all_po_indices - used_po_indices)
        ]

        return matches, unmatched_invoice, unmatched_po_nums

    def _find_best_po_line(
        self,
        inv_line: LineItem,
        po_lines: list[POLineItem],
        used: set[int],
    ) -> Optional[tuple[int, POLineItem, float]]:
        """Find the best matching PO line for an invoice line (by description + SKU)."""
        # 1. SKU exact match
        if inv_line.sku:
            for i, po_line in enumerate(po_lines):
                if i not in used and po_line.sku and po_line.sku.upper() == inv_line.sku.upper():
                    return i, po_line, 100.0

        # 2. Fuzzy description match
        inv_desc = (inv_line.description or "").lower()
        if not inv_desc:
            return None

        try:
            from rapidfuzz import fuzz
            best_score = 0
            best_idx = -1
            for i, po_line in enumerate(po_lines):
                if i in used:
                    continue
                score = fuzz.token_sort_ratio(inv_desc, po_line.description.lower())
                if score > best_score:
                    best_score = score
                    best_idx = i
            if best_idx >= 0 and best_score >= LINE_FUZZY_THRESHOLD:
                return best_idx, po_lines[best_idx], float(best_score)
        except ImportError:
            # Fall back to simple substring check
            for i, po_line in enumerate(po_lines):
                if i in used:
                    desc = po_line.description.lower()
                    if inv_desc in desc or desc in inv_desc:
                        return i, po_line, 80.0

        return None

    def _build_line_match(
        self,
        inv_idx: int,
        inv_line: LineItem,
        po_idx: int,
        po_line: POLineItem,
        score: float,
    ) -> POLineMatch:
        qty_ok = self._qty_matches(inv_line.quantity, po_line.quantity)
        price_ok = self._price_matches(inv_line.unit_price, po_line.unit_price)
        total_ok = self._total_matches(inv_line.total, po_line.total)

        return POLineMatch(
            invoice_line_index=inv_idx,
            invoice_description=inv_line.description,
            po_line_number=po_line.line_number or (po_idx + 1),
            po_description=po_line.description,
            match_score=round(score / 100.0, 3),
            matched=True,
            quantity_matches=qty_ok,
            price_matches=price_ok,
            total_matches=total_ok,
        )

    @staticmethod
    def _qty_matches(a: Optional[float], b: float) -> Optional[bool]:
        if a is None:
            return None
        return abs(a - b) <= 0.001

    @staticmethod
    def _price_matches(a: Optional[float], b: float) -> Optional[bool]:
        if a is None:
            return None
        if b == 0:
            return a == 0
        return abs(a - b) / b <= PRICE_TOLERANCE_PCT

    @staticmethod
    def _total_matches(a: Optional[float], b: float) -> Optional[bool]:
        if a is None:
            return None
        return abs(a - b) <= TOTAL_TOLERANCE_ABS


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _to_float(value: Optional[str]) -> Optional[float]:
    if not value or not str(value).strip():
        return None
    cleaned = re.sub(r"[^\d.\-]", "", str(value))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _normalise_po_number(po: Optional[str]) -> Optional[str]:
    if not po:
        return None
    return po.strip()
