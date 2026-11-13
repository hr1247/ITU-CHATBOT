FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --system --uid 10001 --home /app ingest
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ingest.py .
RUN mkdir -p /state && chown ingest /state
USER ingest
ENTRYPOINT ["python", "/app/ingest.py"]
