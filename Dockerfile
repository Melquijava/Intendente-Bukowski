FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-por tesseract-ocr-eng && tesseract --version && tesseract --list-langs && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY bukowski ./bukowski
ENV DATABASE_PATH=/data/bukowski.sqlite3 PYTHONUNBUFFERED=1
CMD ["python", "-m", "bukowski"]
