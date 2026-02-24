from pydantic import BaseModel, Field
from typing import Optional, List


class POLineItem(BaseModel):
    """A single line item on a Purchase Order."""
    line_number: Optional[int] = None
    sku: Optional[str] = None
    description: str
    quantity: float
    unit: Optional[str] = None
    unit_price: float
    total: float


class PurchaseOrder(BaseModel):
    """
    A Purchase Order loaded from the PO data source (CSV or API).
    po_number is the primary key used for matching against invoices.
    """
    po_number: str
    supplier_id: Optional[str] = None       # ID referencing the suppliers list
    supplier_name: Optional[str] = None
    issue_date: Optional[str] = None        # YYYY-MM-DD
    expected_delivery: Optional[str] = None # YYYY-MM-DD
    subtotal: Optional[float] = None
    tax_amount: Optional[float] = None
    total: Optional[float] = None
    currency: Optional[str] = "AUD"
    status: Optional[str] = None            # e.g. "open", "partially_received", "closed"
    notes: Optional[str] = None
    line_items: List[POLineItem] = Field(default_factory=list)
