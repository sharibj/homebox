FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock \
    && useradd --uid 10001 --create-home app \
    && mkdir -p /data/uploads && chown -R app:app /data
COPY assistant ./assistant
COPY tests ./tests
USER 10001
EXPOSE 8000
CMD ["uvicorn", "assistant.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
