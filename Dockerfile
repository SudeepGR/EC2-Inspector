FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as a non-root user.
RUN useradd --create-home inspector
USER inspector

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

# Credentials are NOT baked into the image: use a task/instance role, or pass
# AWS_* variables or a mounted ~/.aws at run time.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
