# Build context is ./services so we can reuse import/import_telegram.py.
FROM python:3.11-slim

WORKDIR /app

COPY importer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Reuse the stdlib-only OpenSearch client from the ingest service.
COPY import/import_telegram.py .
COPY importer/backfill_worker.py importer/telegram_source.py ./

CMD ["python", "-u", "/app/backfill_worker.py"]
