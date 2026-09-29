FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MONITOR_CONFIG=/app/config.yaml

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor ./monitor

RUN mkdir -p /data

# Docker socket access is root-equivalent. Keep this service isolated and do
# not expose its socket or HTTP API outside the host.
USER root
CMD ["python", "-m", "monitor"]
