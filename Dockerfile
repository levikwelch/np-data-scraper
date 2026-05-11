# Container image for the Flask UI, deployed alongside n8n on a Hostinger VPS.
# Single process running gunicorn (1 worker + threads is required so that
# background scrape jobs and the in-memory dataframe stay in one address
# space; multiple workers would break the job-polling endpoints).
FROM python:3.12-slim

WORKDIR /app

# Install pip deps first so the layer is cached when only source changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent data lives on a docker volume mounted here.
ENV DATA_ROOT=/data

EXPOSE 8000

# Ensure the data/output subdirs exist on the mounted volume before
# gunicorn loads app.py (which fails fast if MASTER_CSV is missing,
# but the dirs themselves must be present so we don't error earlier).
CMD ["sh", "-c", "mkdir -p $DATA_ROOT/data $DATA_ROOT/output && exec gunicorn --workers 1 --threads 8 --timeout 300 --bind 0.0.0.0:8000 app:app"]
