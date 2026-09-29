FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-fra \
    tesseract-ocr-deu \
    tesseract-ocr-spa \
    tesseract-ocr-ita \
    libreoffice \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY model/ /app/
COPY runtime-requirements.txt /tmp/jin-runtime-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r /tmp/jin-runtime-requirements.txt
COPY jin_runtime/ /app/jin_runtime/

ENV JIN_FEEDBACK_PATH=/app/training/feedback/learning_feedback.jsonl \
    JIN_LEARNING_MODEL_DIR=/app/data/learning \
    JIN_CORPUS_ROUTER_MODEL=/app/data/learning/corpus_text_router.joblib \
    JIN_ROLE_OVERRIDE_THRESHOLD=0.93 \
    JIN_COMPONENT_FILL_THRESHOLD=0.88

EXPOSE 8000
CMD ["uvicorn", "jin_runtime.app:app", "--host", "0.0.0.0", "--port", "8000"]
