FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Paris
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY bankwatch ./bankwatch
RUN pip install --no-cache-dir .

COPY rules ./rules
VOLUME ["/app/data"]
EXPOSE 8787

CMD ["bankwatch", "serve"]
