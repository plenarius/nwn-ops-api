FROM python:3.12-slim
RUN useradd --uid 1000 --create-home app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
USER app
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=3).status == 200 else 1)"
# Idle keep-alive connections are kept 75 s (uvicorn's default is 5 s), so a caller that keeps its connections for 60 s reuses one and the
# server never closes a connection the caller is about to reuse.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8100", "--timeout-keep-alive", "75"]
