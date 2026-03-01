
import sys
from pathlib import Path
from docling.document_converter import DocumentConverter

def inspect_doc(pdf_path):
    converter = DocumentConverter()
    result = converter.convert(str(pdf_path))
    doc = result.document
    
    print(f"Document has {len(doc.pages)} pages.")
    
    # Inspect first 10 elements
    for i, element in enumerate(doc.elements[:20]):
        text = getattr(element, 'text', 'N/A')
        prov = getattr(element, 'prov', [])
        print(f"Element {i}: {type(element).__name__}")
        print(f"  Text: {text[:50]}")
        if prov:
            p = prov[0]
            print(f"  Page: {p.page_no}")
            if hasattr(p, 'bbox'):
                print(f"  BBox: {p.bbox}")
        print("-" * 20)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        inspect_doc(sys.argv[1])
    else:
        print("Usage: python inspect_doc.py <pdf_path>")
