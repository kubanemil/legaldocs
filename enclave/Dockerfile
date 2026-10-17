# Every byte of this file, requirements.txt and the enclave and protocol sources is measured into PCR0.
FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app

COPY enclave/requirements.txt /app/enclave/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes -r enclave/requirements.txt \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin enclave \
    && mkdir -p /run/nsm && chown 10001 /run/nsm

COPY protocol/ /app/protocol/
COPY enclave/ /app/enclave/

USER 10001
EXPOSE 8443
CMD ["python", "-m", "enclave.server"]
