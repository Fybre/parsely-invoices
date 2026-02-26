# ---------------------------------------------------------------------------
# Invoice Processing Pipeline
# ---------------------------------------------------------------------------
# Multi-stage build:
#   builder  — installs Python deps (avoids re-downloading on every code change)
#   runtime  — lean final image with only what's needed at runtime
# ---------------------------------------------------------------------------

# --- Stage 1: dependency builder -------------------------------------------
FROM python:3.12-slim AS builder

# System packages needed to compile/install heavy ML deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .

RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt


# --- Stage 2: runtime -------------------------------------------------------
FROM python:3.12-slim AS runtime

# Runtime system libraries required by Docling's vision/OCR stack
#   libgl1          — OpenCV (used by EasyOCR inside Docling)
#   libglib2.0-0    — GLib (OpenCV dependency)
#   libgomp1        — OpenMP (PyTorch parallelism)
#   poppler-utils   — pdf2image / pdfinfo (optional but useful)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder stage
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy application source
COPY config.py               ./
COPY main.py                 ./
COPY bootstrap.py            ./
COPY docker-entrypoint.sh    ./
COPY models/                 ./models/
COPY pipeline/               ./pipeline/
COPY dashboard/              ./dashboard/
COPY defaults/               ./defaults/

RUN chmod +x /app/docker-entrypoint.sh

# Ensure model cache directories are writable by any user (for non-root execution)
# RapidOCR, Transformers, and Docling download models on first use
RUN mkdir -p /tmp/rapidocr /tmp/transformers /tmp/docling /tmp/torch /tmp/home && \
    chmod -R 777 /tmp/rapidocr /tmp/transformers /tmp/docling /tmp/torch /tmp/home && \
    # Make package model directories writable too
    mkdir -p /usr/local/lib/python3.12/site-packages/rapidocr/models && \
    chmod -R 777 /usr/local/lib/python3.12/site-packages/rapidocr/models 2>/dev/null || true

# Ensure /app is on the Python path so relative imports (from models, from config) work
ENV PYTHONPATH=/app

# Set default cache directories for ML libraries
ENV HOME=/tmp/home
ENV USER=appuser
ENV RAPIDOCR_HOME=/tmp/rapidocr
ENV TRANSFORMERS_CACHE=/tmp/transformers
ENV HF_HOME=/tmp/transformers
ENV TORCHINDUCTOR_CACHE_DIR=/tmp/torch
ENV DOCLING_CACHE_DIR=/tmp/docling

# ---------------------------------------------------------------------------
# Volume mount points
#   /app/data       — suppliers.csv, purchase_orders.csv, purchase_order_lines.csv
#   /app/invoices   — input PDF invoices (read-only recommended)
#   /app/output     — JSON results written here
#   /root/.cache/docling — Docling ML models (~1 GB, persisted via named volume)
# ---------------------------------------------------------------------------
VOLUME ["/app/data", "/app/invoices", "/app/output", "/root/.cache/docling"]

# ---------------------------------------------------------------------------
# Entrypoint: routes to watch or batch mode based on WATCH_MODE env var.
# Explicit subcommands always take priority:
#   docker compose run --rm pipeline check
#   docker compose run --rm pipeline process /app/invoices
#   docker compose run --rm pipeline process /app/invoices/my_invoice.pdf
#   docker compose run --rm pipeline watch /app/invoices --interval 60
# ---------------------------------------------------------------------------
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD []
