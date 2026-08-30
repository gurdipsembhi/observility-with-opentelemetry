FROM python:3.12-slim

WORKDIR /app

# Logs appear immediately instead of sitting in a buffer.
ENV PYTHONUNBUFFERED=1

# Deps first, so code edits don't bust the pip cache layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py telemetry.py ./

# "localhost" inside a container is the container itself. On the compose
# network, the backend is reachable by its service name.
ENV OTEL_OTLP=1 OTEL_EXPORTER_OTLP_ENDPOINT=http://lgtm:4317

EXPOSE 8000

CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
