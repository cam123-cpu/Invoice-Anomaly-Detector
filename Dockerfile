FROM python:3.12-slim
# Tesseract reads scanned and photographed invoices (OCR), entirely inside this container.
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir ".[postgres,ocr]"
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh && useradd --create-home app && mkdir /data && chown app /data
USER app
ENV DB_PATH=/data/invoices.db PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4)"
# Secrets are supplied at runtime (environment variables or *_FILE paths), never baked in.
ENTRYPOINT ["/entrypoint.sh"]
