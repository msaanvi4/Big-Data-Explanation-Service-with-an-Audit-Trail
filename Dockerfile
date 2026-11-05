# Dockerfile for the Explanation Service with an Audit Trail (Person 2 query interface)
# Build:  docker build -t explanation-service .
# Run:    docker run -p 8000:8000 explanation-service
#
# Backend selection at runtime via EXPLANATION_REPO_BACKEND (duckdb | iceberg | fixture).
# DuckDB is the default and requires no external cluster.

FROM python:3.12-slim

WORKDIR /app

# System dependencies for pyarrow / pyiceberg builds
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy the whole repository (storage design, query interface, compliance module, data)
COPY . .

# Default: run the FastAPI service on the DuckDB/Parquet backend.
# Override with: docker run -e EXPLANATION_REPO_BACKEND=iceberg ...
ENV EXPLANATION_REPO_BACKEND=duckdb \
    PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "person2_query_interface.app.service:app", "--host", "0.0.0.0", "--port", "8000"]
