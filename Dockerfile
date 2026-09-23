FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    CCC_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
COPY ccc_mcp ./ccc_mcp
RUN useradd --uid 10001 --create-home mcp && mkdir -p /data && chown -R mcp:mcp /data
USER mcp
VOLUME ["/data"]

EXPOSE 8000
CMD ["python", "server.py"]
