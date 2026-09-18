FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir opensearch-mcp-server-py

EXPOSE 9900
CMD ["python", "-m", "mcp_server_opensearch", "--transport", "stream", "--host", "0.0.0.0", "--port", "9900"]
