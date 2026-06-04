FROM pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime

WORKDIR /srv

# Install system dependencies for OpenCV
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install API + pipeline deps
COPY requirements.txt requirements-detection.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-detection.txt

COPY app ./app
COPY pipeline ./pipeline
COPY yolov8n.pt ./yolov8n.pt
COPY store_layout.json ./store_layout.json

ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/store.db \
    POS_PATH=/data/pos_transactions.csv \
    PYTHONPATH=/srv

EXPOSE 8000

# Healthcheck hits the real endpoint an on-call engineer would check.
HEALTHCHECK --interval=15s --timeout=4s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health',timeout=3).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
