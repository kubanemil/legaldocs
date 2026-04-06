FROM python:3.12-slim

WORKDIR /app

COPY parent/requirements.txt /app/parent/requirements.txt
RUN pip install --no-cache-dir -r /app/parent/requirements.txt

COPY parent/ /app/parent/
COPY contracts/ /app/contracts/
COPY alembic/ /app/alembic/
COPY alembic.ini /app/alembic.ini
