from pydantic import BaseModel, Field
from typing import Optional, List


class SupplierInfo(BaseModel):
    """Supplier details as extracted from the invoice."""
    name: Optional[str] = None
    abn: Optional[str] = None          # Australian Business Number
    acn: Optional[str] = None          # Australian Company Number
    address: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None


class BillToInfo(BaseModel):
    """Bill-to / customer details extracted from the invoice."""
    name: Optional[str] = None
    address: Optional[str] = None
    email: Optional[str] = None
    contact: Optional[str] = None


class LineItem(BaseModel):
    """A single line item on an invoice."""
    line_number: Optional[int] = None
    sku: Optional[str] = None
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None          # e.g. "ea", "hr", "kg"
    unit_price: Optional[float] = None
    discount: Optional[float] = None    # as a decimal, e.g. 0.10 = 10%
    total: Optional[float] = None       # quantity * unit_price (after discount)


class ExtractedInvoice(BaseModel):
    """
    Full structured invoice data as extracted by the LLM.
    All monetary values are in the invoice currency (default AUD).
    Dates are ISO 8601 strings (YYYY-MM-DD).
    """
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None      # YYYY-MM-DD
    due_date: Optional[str] = None          # YYYY-MM-DD

    supplier: Optional[SupplierInfo] = None
    bill_to: Optional[BillToInfo] = None

    po_number: Optional[str] = None         # Purchase Order reference if present
    reference: Optional[str] = None         # Other reference numbers (order ref, job no.)

    line_items: List[LineItem] = Field(default_factory=list)

    subtotal: Optional[float] = None        # Pre-tax total
    tax_rate: Optional[float] = None        # e.g. 0.10 for 10% GST
    tax_amount: Optional[float] = None      # Dollar amount of tax
    shipping: Optional[float] = None        # Shipping/freight if itemised separately
    other_charges: Optional[float] = None   # Any other additional charges
    total: Optional[float] = None           # Grand total payable

    currency: Optional[str] = "AUD"
    payment_terms: Optional[str] = None     # e.g. "Net 30", "Due on Receipt"
    bank_details: Optional[str] = None      # BSB/account if present
    notes: Optional[str] = None

    # Operator-defined custom fields (from config/custom_fields.json)
    # Keys are field names as defined in the config; values are extracted strings.
    custom_fields: dict = Field(default_factory=dict)
    # Dashboard card header label for the custom fields section.
    custom_fields_title: str = "Custom Fields"
