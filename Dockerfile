FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY chatgemini/ ./chatgemini/
COPY config.example.json ./config.json
RUN mkdir -p /app/data

VOLUME ["/app/data"]
EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8081/healthz', timeout=3).read()"

CMD ["python", "-m", "chatgemini", "--config", "/app/config.json"]
