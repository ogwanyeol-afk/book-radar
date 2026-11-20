FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bookrec ./bookrec
COPY server.py .
ENV PORT=8000 MCP_TRANSPORT=streamable-http BOOKREC_LOCAL_FILES=0
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["python", "server.py"]
