FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
# tesseract is only used when an official PDF has no text layer (OCR fallback)
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-st.txt ./
ARG WITH_ST=0
RUN pip install -r requirements.txt && if [ "$WITH_ST" = "1" ]; then pip install -r requirements-st.txt; fi
COPY app ./app
COPY eval ./eval
COPY corpus ./corpus
COPY loadtest ./loadtest
RUN useradd -r -u 10001 ecc && mkdir -p /app/data && chown -R ecc /app/data
USER ecc
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
