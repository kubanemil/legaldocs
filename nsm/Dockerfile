FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app

COPY enclave/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes -r requirements.txt \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin nsm \
    && mkdir -p /run/nsm /trust && chown 10001 /run/nsm /trust

COPY protocol/ /app/protocol/
COPY nsm/ /app/nsm/

# The NSM measures the enclave's build inputs itself, the way a hypervisor measures an image.
COPY enclave/ /measured/enclave/
COPY protocol/ /measured/protocol/

USER 10001
CMD ["python", "-m", "nsm.server"]
