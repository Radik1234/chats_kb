FROM python:3.11-slim

WORKDIR /app
COPY import_telegram.py ingest_watch.py .

CMD ["python", "-u", "/app/ingest_watch.py"]
