FROM pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime

WORKDIR /srv

# System dependencies for OpenCV
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt requirements-detection.txt ./

RUN pip install --no-cache-dir \
    -r requirements.txt \
    -r requirements-detection.txt

# Application code
COPY app ./app
COPY pipeline ./pipeline

# Model and configuration
COPY yolov8n.pt ./yolov8n.pt
COPY store_layout.json ./store_layout.json

# Include CCTV footage and POS data in image
COPY "CCTV Footage" "./CCTV Footage"

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv \
    DB_PATH=/data/store.db \
    POS_PATH="/srv/CCTV Footage/pos.csv"

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=4s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health',timeout=3).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]