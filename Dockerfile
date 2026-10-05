FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/omniguardian
COPY pyproject.toml ./
COPY constraints.txt ./
COPY app ./app
RUN pip install -c constraints.txt . && useradd --create-home --uid 10001 guardian
COPY migrations ./migrations
COPY scripts ./scripts
COPY tests ./tests
USER guardian
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-proxy-headers"]
