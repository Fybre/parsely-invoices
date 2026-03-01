# Project Status: Interactive Highlighting, Anchoring, and Dynamic Metadata

## Recent Progress (v1.1.0-dev)

### 1. Interactive PDF Highlighting (Grounding)
- **Robust Mapping Engine**: Implemented `TextStreamMap` in `pipeline/processor.py` which treats the document as a continuous text stream. This solves fragmentation issues where text like "INV-123" is split across multiple PDF objects.
- **Multi-Strategy Search**: Added prioritized matching strategies:
    1. Exact element match.
    2. Date component permutation (handles ISO `2022-02-15` matching PDF `15/02/2022`).
    3. Boundary-aware numeric matching (prevents quantities matching fragments of phone numbers).
    4. Continuous stream matching for fragmented text.
- **Localized Table Awareness**: Implemented row-locked mapping for line items. Quantity, Price, and Total matching is now restricted to the specific table row identified by the description, eliminating cross-row ambiguity.
- **Advanced UI Overlay**:
    *   Added an explicit `highlight-layer` with `mix-blend-mode: multiply` for a professional "highlighter" look.
    *   Implemented vertical alignment heuristics to center highlights over text glyphs correctly regardless of Docling's mixed coordinate origins.
    *   Added support for multiple bounding boxes per field.

### 2. Buyer Anchoring (Inversion Prevention)
- **Internal Entities**: Created `config/internal_companies.json` to store "Self" identities (Name/ABN).
- **Prompt Injection**: The LLM prompt now includes a `STRICT IDENTIFICATION RULE` block defining these entities as the **Buyer**.
- **Auto-Correction Pass**: The `InvoiceProcessor` now detects if the LLM self-identifies as the supplier and automatically triggers a second LLM pass with a `CRITICAL CORRECTION` hint to force re-evaluation.
- **Validation**: Added `supplier_is_internal_entity` discrepancy check in `pipeline/validator.py`.

### 3. Dynamic Custom Fields (Metadata Engine)
- **Multi-Source Support**:
    *   `llm`: Automated extraction (Regex/LLM).
    *   `text`: Manual operator entry (skipped by pipeline to save tokens).
    *   `lookup`: Dropdown selection populated from CSV files in the `data/` directory (e.g., `projects.csv`).
- **Dynamic UI**: The dashboard now renders different input types (Text vs. Select) based on the field configuration.
- **Admin Management**: Added a new UI in the Admin panel to manage Internal Companies and complex Custom Field configurations without editing JSON files.

### 4. UI/UX Refinement
- **Glassmorphism Modals**: Replaced native browser `alert()` and `confirm()` with custom-styled modals featuring 0.5 opacity dark backgrounds and 16px backdrop blurs.
- **Role-Based UI**: The Admin menu is now dynamically hidden for non-admin users based on the `/api/auth/me` role check.
- **Version Tracking**: Implemented a `VERSION` file in the project root that is automatically picked up by the build process and displayed in the UI header.

## Future Pick-up Points
- **Lookup Combobox**: For very large lookup CSVs, replace the standard HTML `<select>` with a searchable combobox to improve performance.
- **Prompt Refinement**: Consider adding few-shot examples to the prompt if complex directionality issues persist beyond anchoring.
- **Multi-page Highlighting**: Ensure the `auto-scroll` logic in `showHighlight` handles multi-page focus optimally.
