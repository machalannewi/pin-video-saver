# Runs the Stash web app. One worker on purpose: download jobs are kept in memory.
FROM python:3.13-slim
WORKDIR /app
COPY web/requirements.txt web/requirements.txt
RUN pip install --no-cache-dir -r web/requirements.txt
COPY stash_core.py .
COPY web web
ENV TRUST_PROXY=1
CMD gunicorn --chdir web --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:${PORT:-8000} app:app
