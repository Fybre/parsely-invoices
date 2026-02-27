"""
PDF rendering and lookup service.
"""
import base64
import io
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# In-memory PDF page-render cache  {stem: {"mtime": float, "pages": list}}
_PAGE_CACHE_MAX = 50
_PAGE_CACHE: OrderedDict[str, dict] = OrderedDict()
_PAGE_CACHE_LOCK = threading.Lock()


def find_pdf(source_file: str, stem: str, invoices_dir: Path, export_dir: Path) -> Optional[Path]:
    """
    Locate the original PDF.

    Search order:
      1. The exact path recorded at extraction time
      2. INVOICES_DIR/<filename>   (PDF still in inbox)
      3. EXPORT_DIR/<stem>.pdf     (PDF moved to export folder after approval)
    """
    if source_file:
        p = Path(source_file)
        if p.exists():
            return p
        candidate = invoices_dir / p.name
        if candidate.exists():
            return candidate

    # Try export dir (invoice was approved)
    export_pdf = export_dir / f"{stem}.pdf"
    if export_pdf.exists():
        return export_pdf

    return None


def render_pages(pdf_path: Path, max_pages: int = 12) -> list[dict]:
    """Render PDF pages to JPEG and return as base64-encoded dicts."""
    try:
        from pdf2image import convert_from_path  # type: ignore
        images = convert_from_path(
            str(pdf_path), dpi=150,
            first_page=1, last_page=max_pages,
        )
        pages = []
        for i, img in enumerate(images):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            pages.append({
                "page": i + 1,
                "mime": "image/jpeg",
                "data": base64.b64encode(buf.getvalue()).decode(),
            })
        return pages
    except ImportError:
        pass  # fall through to pdfplumber

    try:
        import pdfplumber  # type: ignore
        pages = []
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages[:max_pages]):
                img = page.to_image(resolution=150)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                pages.append({
                    "page": i + 1,
                    "mime": "image/png",
                    "data": base64.b64encode(buf.getvalue()).decode(),
                })
        return pages
    except Exception as e:
        logger.error("Failed to render PDF %s: %s", pdf_path, e)
        return []


def get_cached_pages(stem: str, mtime: float) -> list[dict] | None:
    """Get cached pages if mtime matches."""
    with _PAGE_CACHE_LOCK:
        cached = _PAGE_CACHE.get(stem)
        if cached and abs(cached["mtime"] - mtime) < 0.5:
            _PAGE_CACHE.move_to_end(stem)
            return cached["pages"]
    return None


def cache_pages(stem: str, mtime: float, pages: list[dict]) -> None:
    """Cache rendered pages with LRU eviction."""
    with _PAGE_CACHE_LOCK:
        _PAGE_CACHE[stem] = {"mtime": mtime, "pages": pages}
        _PAGE_CACHE.move_to_end(stem)
        while len(_PAGE_CACHE) > _PAGE_CACHE_MAX:
            _PAGE_CACHE.popitem(last=False)


def invalidate_cache(stem: str) -> None:
    """Remove a stem from the page cache."""
    with _PAGE_CACHE_LOCK:
        _PAGE_CACHE.pop(stem, None)
