FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app

COPY parent/requirements.txt /app/parent/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes -r parent/requirements.txt \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin parent

COPY alembic.ini /app/
COPY alembic/ /app/alembic/
COPY contracts/ReceiptRegistry.json /app/contracts/
COPY protocol/ /app/protocol/
COPY parent/ /app/parent/

USER 10001
