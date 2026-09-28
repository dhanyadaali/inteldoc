FROM python:3.12-slim
WORKDIR /app

# libgl/glib: needed by OpenCV, which the local OCR engine (RapidOCR) uses
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Scenario PDFs are regenerated; the dataset scans are copied from data/dataset if present,
# otherwise downloaded from Hugging Face.
RUN python scripts/generate_invoices.py \
    && (test -f data/dataset/ground_truth.json || python scripts/fetch_dataset.py --limit 26) \
    && mkdir -p data/uploads \
    && chmod -R a+rwX /app/data   # Hugging Face Spaces run the container as a non-root user (uid 1000)

ENV PORT=8000 PYTHONUNBUFFERED=1
EXPOSE 8000
# seed.py is idempotent: creates tables, vendors and PO-7781 if missing, keeps existing history
CMD python scripts/seed.py && uvicorn web.server:app --host 0.0.0.0 --port ${PORT}
