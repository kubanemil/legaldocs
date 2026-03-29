#!/usr/bin/env bash
set -euo pipefail

# Generate mTLS certificates for LegalDocs
# Produces: ca.{key,crt}, enclave.{key,crt}, parent.{key,crt}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
CERTS_DIR="${PROJECT_ROOT}/certs"

mkdir -p "$CERTS_DIR"

echo "Generating certificates in ${CERTS_DIR}..."

# 1. Generate CA key and self-signed certificate
openssl genrsa -out "${CERTS_DIR}/ca.key" 2048 2>/dev/null
openssl req -new -x509 -key "${CERTS_DIR}/ca.key" \
    -out "${CERTS_DIR}/ca.crt" \
    -days 3650 \
    -subj "/CN=LegalDocs CA" 2>/dev/null

# 2. Generate enclave server key and certificate with SAN
openssl genrsa -out "${CERTS_DIR}/enclave.key" 2048 2>/dev/null
openssl req -new -key "${CERTS_DIR}/enclave.key" \
    -out "${CERTS_DIR}/enclave.csr" \
    -subj "/CN=enclave" 2>/dev/null

cat > "${CERTS_DIR}/enclave_ext.cnf" <<EOF
[v3_req]
subjectAltName = DNS:enclave,DNS:localhost
EOF

openssl x509 -req -in "${CERTS_DIR}/enclave.csr" \
    -CA "${CERTS_DIR}/ca.crt" \
    -CAkey "${CERTS_DIR}/ca.key" \
    -CAcreateserial \
    -out "${CERTS_DIR}/enclave.crt" \
    -days 3650 \
    -extensions v3_req \
    -extfile "${CERTS_DIR}/enclave_ext.cnf" 2>/dev/null

# 3. Generate parent client key and certificate
openssl genrsa -out "${CERTS_DIR}/parent.key" 2048 2>/dev/null
openssl req -new -key "${CERTS_DIR}/parent.key" \
    -out "${CERTS_DIR}/parent.csr" \
    -subj "/CN=parent" 2>/dev/null

openssl x509 -req -in "${CERTS_DIR}/parent.csr" \
    -CA "${CERTS_DIR}/ca.crt" \
    -CAkey "${CERTS_DIR}/ca.key" \
    -CAcreateserial \
    -out "${CERTS_DIR}/parent.crt" \
    -days 3650 2>/dev/null

# Clean up CSRs and temp files
rm -f "${CERTS_DIR}"/*.csr "${CERTS_DIR}"/*.cnf "${CERTS_DIR}"/*.srl

echo "Certificates generated successfully:"
ls -la "${CERTS_DIR}"/*.key "${CERTS_DIR}"/*.crt
