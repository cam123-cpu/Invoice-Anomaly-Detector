FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN useradd --create-home app && mkdir /data && chown app /data
USER app
ENV DB_PATH=/data/invoices.db
EXPOSE 8000
# Secrets are passed at runtime (docker run --env-file .env), never baked into the image.
CMD ["uvicorn", "invoice_detector.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
