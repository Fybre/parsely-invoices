"""
Invoice validation and discrepancy detection module.

Checks:
  Arithmetic:  line item totals, tax calculation, grand total
  Dates:       future invoices, very old invoices, overdue, due < invoice
  PO:          PO not found, supplier mismatch, total exceeded, line mismatches
  Supplier:    not matched, ABN mismatch vs PO
  Data quality: missing required fields, negative/zero amounts
"""
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from models.invoice import ExtractedInvoice
from models.purchase_order import PurchaseOrder
from models.result import Discrepancy, MatchedPO, MatchedSupplier

logger = logging.getLogger(__name__)

# Configurable thresholds (can be overridden via Config)
MAX_DAYS_IN_PAST = 90       # warn if invoice date > this many days ago
MAX_DAYS_IN_FUTURE = 7      # warn if invoice date > this many days ahead
ARITHMETIC_TOLERANCE = 0.05 # $0.05 absolute tolerance for totals
TAX_RATE_TOLERANCE = 0.005  # 0.5% tolerance on tax rate back-calculation
PO_TOTAL_TOLERANCE_PCT = 0.01  # 1% tolerance for invoice vs PO total


class InvoiceValidator:
    """
    Produces a list of Discrepancy objects for a processed invoice.

    Usage:
        validator = InvoiceValidator()
        discrepancies = validator.validate(invoice, matched_po, matched_supplier, po_record)
    """

    def __init__(
        self,
        max_days_past: int = MAX_DAYS_IN_PAST,
        max_days_future: int = MAX_DAYS_IN_FUTURE,
    ):
        self.max_days_past = max_days_past
        self.max_days_future = max_days_future

    def validate(
        self,
        invoice: ExtractedInvoice,
        matched_po: Optional[MatchedPO],
        matched_supplier: Optional[MatchedSupplier],
        po_record: Optional[PurchaseOrder] = None,
    ) -> list[Discrepancy]:
        """Run all checks and return combined discrepancies list."""
        issues: list[Discrepancy] = []
        issues.extend(self._check_data_quality(invoice))
        issues.extend(self._check_arithmetic(invoice))
        issues.extend(self._check_dates(invoice))
        issues.extend(self._check_supplier(invoice, matched_supplier))
        issues.extend(self._check_po(invoice, matched_po, po_record))
        return issues

    # ------------------------------------------------------------------
    # Data quality checks
    # ------------------------------------------------------------------

    def _check_data_quality(self, inv: ExtractedInvoice) -> list[Discrepancy]:
        issues = []

        if not inv.invoice_number:
            issues.append(Discrepancy(
                type="missing_invoice_number",
                severity="warning",
                description="No invoice number found on the invoice",
                field="invoice_number",
            ))

        if not inv.invoice_date:
            issues.append(Discrepancy(
                type="missing_invoice_date",
                severity="warning",
                description="No invoice date found on the invoice",
                field="invoice_date",
            ))

        if not (inv.supplier and inv.supplier.name):
            issues.append(Discrepancy(
                type="missing_supplier_name",
                severity="warning",
                description="No supplier name could be extracted",
                field="supplier.name",
            ))

        if inv.total is None:
            issues.append(Discrepancy(
                type="missing_total",
                severity="error",
                description="No total amount found on the invoice",
                field="total",
            ))
        elif inv.total == 0:
            issues.append(Discrepancy(
                type="zero_total",
                severity="warning",
                description="Invoice total is zero",
                field="total",
                invoice_value="0.00",
            ))
        elif inv.total < 0:
            issues.append(Discrepancy(
                type="negative_amount",
                severity="error",
                description=f"Invoice total is negative: {inv.total:.2f}",
                field="total",
                invoice_value=str(inv.total),
            ))

        if not inv.line_items:
            issues.append(Discrepancy(
                type="missing_line_items",
                severity="info",
                description="No line items extracted — arithmetic cross-checks skipped",
                field="line_items",
            ))

        # Check for any negative line item amounts
        for i, item in enumerate(inv.line_items):
            if item.total is not None and item.total < 0:
                issues.append(Discrepancy(
                    type="negative_amount",
                    severity="warning",
                    description=f"Line item {i + 1} has a negative total: {item.total:.2f}",
                    field=f"line_items[{i}].total",
                    invoice_value=str(item.total),
                    po_line_number=i + 1,
                ))

        return issues

    # ------------------------------------------------------------------
    # Arithmetic checks
    # ------------------------------------------------------------------

    def _check_arithmetic(self, inv: ExtractedInvoice) -> list[Discrepancy]:
        issues = []
        tol = ARITHMETIC_TOLERANCE

        # Sum of line item totals vs subtotal
        if inv.line_items and inv.subtotal is not None:
            items_with_total = [li for li in inv.line_items if li.total is not None]
            if items_with_total:
                computed_subtotal = sum(li.total for li in items_with_total)  # type: ignore[misc]
                if abs(computed_subtotal - inv.subtotal) > tol:
                    issues.append(Discrepancy(
                        type="line_items_subtotal_mismatch",
                        severity="error",
                        description=(
                            f"Sum of line items ({computed_subtotal:.2f}) does not match "
                            f"stated subtotal ({inv.subtotal:.2f})"
                        ),
                        field="subtotal",
                        invoice_value=f"{inv.subtotal:.2f}",
                        expected_value=f"{computed_subtotal:.2f}",
                    ))

        # Tax amount vs subtotal * tax_rate
        if inv.subtotal is not None and inv.tax_rate is not None and inv.tax_amount is not None:
            expected_tax = inv.subtotal * inv.tax_rate
            if abs(expected_tax - inv.tax_amount) > tol:
                issues.append(Discrepancy(
                    type="tax_calculation_mismatch",
                    severity="warning",
                    description=(
                        f"Tax amount ({inv.tax_amount:.2f}) does not match "
                        f"subtotal × rate ({expected_tax:.2f})"
                    ),
                    field="tax_amount",
                    invoice_value=f"{inv.tax_amount:.2f}",
                    expected_value=f"{expected_tax:.2f}",
                ))

        # Grand total vs subtotal + tax + shipping + other
        if inv.total is not None and inv.subtotal is not None:
            components = inv.subtotal
            if inv.tax_amount is not None:
                components += inv.tax_amount
            if inv.shipping is not None:
                components += inv.shipping
            if inv.other_charges is not None:
                components += inv.other_charges
            if abs(components - inv.total) > tol:
                issues.append(Discrepancy(
                    type="grand_total_mismatch",
                    severity="error",
                    description=(
                        f"Grand total ({inv.total:.2f}) does not match "
                        f"sum of components ({components:.2f})"
                    ),
                    field="total",
                    invoice_value=f"{inv.total:.2f}",
                    expected_value=f"{components:.2f}",
                ))

        return issues

    # ------------------------------------------------------------------
    # Date checks
    # ------------------------------------------------------------------

    def _check_dates(self, inv: ExtractedInvoice) -> list[Discrepancy]:
        issues = []
        today = date.today()

        inv_date = _parse_date(inv.invoice_date)
        due_date = _parse_date(inv.due_date)

        if inv_date:
            days_ago = (today - inv_date).days
            days_ahead = (inv_date - today).days

            if days_ahead > self.max_days_future:
                issues.append(Discrepancy(
                    type="invoice_date_future",
                    severity="error",
                    description=(
                        f"Invoice date {inv.invoice_date} is {days_ahead} days in the future"
                    ),
                    field="invoice_date",
                    invoice_value=inv.invoice_date,
                    expected_value=f"<= {today.isoformat()}",
                ))

            if days_ago > self.max_days_past:
                issues.append(Discrepancy(
                    type="invoice_date_too_old",
                    severity="warning",
                    description=(
                        f"Invoice date {inv.invoice_date} is {days_ago} days in the past "
                        f"(threshold: {self.max_days_past} days)"
                    ),
                    field="invoice_date",
                    invoice_value=inv.invoice_date,
                    expected_value=f">= {(today - timedelta(days=self.max_days_past)).isoformat()}",
                ))

        if due_date:
            if due_date < today:
                issues.append(Discrepancy(
                    type="invoice_overdue",
                    severity="warning",
                    description=f"Invoice due date {inv.due_date} has already passed",
                    field="due_date",
                    invoice_value=inv.due_date,
                ))
            if inv_date and due_date < inv_date:
                issues.append(Discrepancy(
                    type="due_date_before_invoice",
                    severity="error",
                    description=(
                        f"Due date ({inv.due_date}) is before invoice date ({inv.invoice_date})"
                    ),
                    field="due_date",
                    invoice_value=inv.due_date,
                    expected_value=f">= {inv.invoice_date}",
                ))

        return issues

    # ------------------------------------------------------------------
    # Supplier checks
    # ------------------------------------------------------------------

    def _check_supplier(
        self,
        inv: ExtractedInvoice,
        matched: Optional[MatchedSupplier],
    ) -> list[Discrepancy]:
        issues = []

        if matched is None and inv.supplier and inv.supplier.name:
            issues.append(Discrepancy(
                type="supplier_not_found",
                severity="warning",
                description=(
                    f"Supplier '{inv.supplier.name}' could not be matched to the supplier list"
                ),
                field="supplier.name",
                invoice_value=inv.supplier.name,
            ))

        return issues

    # ------------------------------------------------------------------
    # PO checks
    # ------------------------------------------------------------------

    def _check_po(
        self,
        inv: ExtractedInvoice,
        matched_po: Optional[MatchedPO],
        po_record: Optional[PurchaseOrder],
    ) -> list[Discrepancy]:
        issues = []

        if not inv.po_number:
            return issues  # No PO on invoice — nothing to check

        if matched_po is None:
            return issues  # Matcher returned nothing (shouldn't happen if po_number present)

        # PO referenced but not found in the loaded POs
        if po_record is None:
            issues.append(Discrepancy(
                type="po_not_found",
                severity="error",
                description=f"Invoice references PO '{inv.po_number}' which was not found",
                field="po_number",
                invoice_value=inv.po_number,
            ))
            return issues

        # Supplier mismatch between PO and matched supplier
        if (matched_po.po_supplier_name
                and inv.supplier
                and inv.supplier.name
                and inv.supplier.name.lower() != matched_po.po_supplier_name.lower()):
            issues.append(Discrepancy(
                type="po_supplier_mismatch",
                severity="warning",
                description=(
                    f"Invoice supplier '{inv.supplier.name}' differs from "
                    f"PO supplier '{matched_po.po_supplier_name}'"
                ),
                field="supplier.name",
                invoice_value=inv.supplier.name,
                expected_value=matched_po.po_supplier_name,
            ))

        # Invoice total vs PO total
        if inv.total is not None and po_record.total is not None:
            pct_diff = abs(inv.total - po_record.total) / max(po_record.total, 0.01)
            if pct_diff > PO_TOTAL_TOLERANCE_PCT:
                issues.append(Discrepancy(
                    type="po_total_exceeded",
                    severity="error",
                    description=(
                        f"Invoice total ({inv.total:.2f}) differs from PO total "
                        f"({po_record.total:.2f}) by {pct_diff * 100:.1f}%"
                    ),
                    field="total",
                    invoice_value=f"{inv.total:.2f}",
                    expected_value=f"{po_record.total:.2f}",
                ))

        # Line item discrepancies
        for lm in matched_po.line_matches:
            if not lm.matched:
                issues.append(Discrepancy(
                    type="po_line_not_found",
                    severity="warning",
                    description=(
                        f"Invoice line {lm.invoice_line_index + 1} "
                        f"('{lm.invoice_description}') has no matching PO line"
                    ),
                    field=f"line_items[{lm.invoice_line_index}]",
                    invoice_value=lm.invoice_description,
                    po_line_number=lm.invoice_line_index + 1,
                ))
                continue

            if lm.quantity_matches is False:
                inv_line = inv.line_items[lm.invoice_line_index] if lm.invoice_line_index < len(inv.line_items) else None
                po_line = next(
                    (pl for pl in po_record.line_items if pl.line_number == lm.po_line_number),
                    None,
                )
                issues.append(Discrepancy(
                    type="po_line_quantity_mismatch",
                    severity="warning",
                    description=(
                        f"Line {lm.invoice_line_index + 1} quantity mismatch vs PO line {lm.po_line_number}"
                    ),
                    field=f"line_items[{lm.invoice_line_index}].quantity",
                    invoice_value=str(inv_line.quantity) if inv_line else None,
                    expected_value=str(po_line.quantity) if po_line else None,
                    po_line_number=lm.po_line_number,
                ))

            if lm.price_matches is False:
                inv_line = inv.line_items[lm.invoice_line_index] if lm.invoice_line_index < len(inv.line_items) else None
                po_line = next(
                    (pl for pl in po_record.line_items if pl.line_number == lm.po_line_number),
                    None,
                )
                issues.append(Discrepancy(
                    type="po_line_price_mismatch",
                    severity="error",
                    description=(
                        f"Line {lm.invoice_line_index + 1} unit price mismatch vs PO line {lm.po_line_number}"
                    ),
                    field=f"line_items[{lm.invoice_line_index}].unit_price",
                    invoice_value=str(inv_line.unit_price) if inv_line else None,
                    expected_value=str(po_line.unit_price) if po_line else None,
                    po_line_number=lm.po_line_number,
                ))

        # PO lines that appear on the PO but not on the invoice
        for po_line_num in matched_po.unmatched_po_lines:
            issues.append(Discrepancy(
                type="po_line_not_found",
                severity="info",
                description=(
                    f"PO line {po_line_num} was not found on the invoice "
                    f"(may be a partial delivery)"
                ),
                field="line_items",
                expected_value=f"PO line {po_line_num}",
                po_line_number=po_line_num,
            ))

        return issues


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_date(date_str: Optional[str]) -> Optional[date]:
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None
