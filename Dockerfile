FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-fra tesseract-ocr-deu \
    tesseract-ocr-spa tesseract-ocr-ita libreoffice poppler-utils xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY bundle_parts /tmp/bundle_parts
RUN cat /tmp/bundle_parts/part_* | base64 -d > /tmp/jin-model.tar.xz \
    && mkdir -p /tmp/src \
    && tar -xJf /tmp/jin-model.tar.xz -C /tmp/src \
    && cp -a /tmp/src/universal_document_ai_rl_v48/. /app/ \
    && rm -rf /tmp/jin-model.tar.xz /tmp/src /tmp/bundle_parts
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8000
CMD ["uvicorn", "uda.api:app", "--host", "0.0.0.0", "--port", "8000"]
