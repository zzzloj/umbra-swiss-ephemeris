FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

COPY requirements.txt ./

# PySwisseph has no wheel for every architecture. Compile it in this disposable
# stage so gcc never reaches the runtime image.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt \
    && rm -rf /var/lib/apt/lists/*

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=builder /install /usr/local

COPY app.py ./

RUN useradd --create-home --shell /usr/sbin/nologin umbra
USER umbra

EXPOSE 8080

CMD ["uvicorn", "app:api", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
