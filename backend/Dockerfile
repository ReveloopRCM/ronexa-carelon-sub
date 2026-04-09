FROM python:3.12-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

# Run migrations on startup, then start API server
EXPOSE 8000

CMD ["sh", "-c", "python -m alembic upgrade head || echo 'Migration failed, continuing...' && python -m hypercorn main:app --bind 0.0.0.0:8000"]
