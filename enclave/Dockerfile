FROM python:3.12-slim

WORKDIR /app

COPY enclave/requirements.txt /app/enclave/requirements.txt
RUN pip install --no-cache-dir -r /app/enclave/requirements.txt

COPY enclave/ /app/enclave/

ENV PYTHONPATH=/app/enclave
CMD ["python", "enclave/server.py"]
