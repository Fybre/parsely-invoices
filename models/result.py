from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from .invoice import ExtractedInvoice


DiscrepancyType = Literal[
    # Arithmetic / totals
    "line_items_subtotal_mismatch",
    "tax_calculation_mismatch",
    "grand_total_mismatch",
    # Dates
    "invoice_date_future",
    "invoice_date_too_old",
    "due_date_before_invoice",
    "invoice_overdue",
    # PO matching
    "po_not_found",
    "po_supplier_mismatch",
    "po_total_exceeded",
    "po_line_not_found",
    "po_line_quantity_mismatch",
    "po_line_price_mismatch",
    "po_line_total_mismatch",
    # Supplier
    "supplier_not_found",
    "supplier_abn_mismatch",
    # Data quality
    "missing_invoice_number",
    "missing_invoice_date",
    "missing_supplier_name",
    "missing_total",
    "missing_line_items",
    "negative_amount",
    "zero_total",
]

SeverityLevel = Literal["error", "warning", "info"]


class Discrepancy(BaseModel):
    """A single detected discrepancy or validation issue."""
    type: str                               # One of DiscrepancyType values
    severity: SeverityLevel                 # error / warning / info
    description: str                        # Human-readable explanation
    field: Optional[str] = None             # Which field is affected
    invoice_value: Optional[str] = None     # What the invoice shows
    expected_value: Optional[str] = None    # What was expected / found elsewhere
    po_line_number: Optional[int] = None    # Relevant for PO line mismatches


class POLineMatch(BaseModel):
    """Result of matching a single invoice line item against a PO line."""
    invoice_line_index: int
    invoice_description: Optional[str] = None
    po_line_number: Optional[int] = None
    po_description: Optional[str] = None
    match_score: float = 0.0                # 0-1 fuzzy match confidence
    matched: bool = False
    quantity_matches: Optional[bool] = None
    price_matches: Optional[bool] = None
    total_matches: Optional[bool] = None


class MatchedPO(BaseModel):
    """The Purchase Order matched against this invoice."""
    po_number: str
    match_method: str                       # e.g. "invoice_reference", "lookup"
    po_total: Optional[float] = None
    po_supplier_name: Optional[str] = None
    po_supplier_id: Optional[str] = None
    line_matches: List[POLineMatch] = Field(default_factory=list)
    unmatched_invoice_lines: List[int] = Field(default_factory=list)   # indices
    unmatched_po_lines: List[int] = Field(default_factory=list)        # line_numbers


class MatchedSupplier(BaseModel):
    """The supplier from the master list matched against this invoice."""
    supplier_id: str
    supplier_name: str
    match_method: str                       # e.g. "abn_exact", "name_fuzzy"
    confidence: float                       # 0-1
    abn: Optional[str] = None


class InvoiceProcessingResult(BaseModel):
    """
    The complete output of processing a single invoice.
    This is the primary machine-readable output written as JSON.
    """
    # --- Metadata ---
    source_file: str
    processed_at: str                       # ISO 8601 datetime
    processing_time_seconds: float

    # --- Extracted invoice data ---
    extracted_invoice: ExtractedInvoice
    raw_text_length: int
    llm_model_used: str

    # --- Matched entities ---
    matched_supplier: Optional[MatchedSupplier] = None
    matched_po: Optional[MatchedPO] = None

    # --- Validation ---
    discrepancies: List[Discrepancy] = Field(default_factory=list)

    # --- Summary ---
    requires_review: bool = False
    review_reasons: List[str] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0

    def compute_summary(self) -> None:
        """Populate summary fields from the discrepancies list."""
        self.error_count = sum(1 for d in self.discrepancies if d.severity == "error")
        self.warning_count = sum(1 for d in self.discrepancies if d.severity == "warning")
        self.requires_review = self.error_count > 0 or self.warning_count > 0
        self.review_reasons = list({d.description for d in self.discrepancies
                                    if d.severity in ("error", "warning")})
