FROM python:3.13-slim

LABEL org.opencontainers.image.description="Prometheus exporter for ASUS ZenWiFi XT9 mesh network — collects system, WiFi radio, and per-client metrics via SSH."
LABEL org.opencontainers.image.source="https://github.com/wheetazlab/asuszenwifixt9-monitoring"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy exporter source
COPY collector/ ./collector/

# Run as non-root
RUN useradd --create-home --uid 1000 --no-user-group exporter
USER exporter

# Expose the Prometheus metrics port
EXPOSE 9100

CMD ["python", "-m", "collector.main"]
