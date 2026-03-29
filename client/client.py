"""Client SDK for interacting with the LegalDocs API.

Provides attestation verification and envelope encryption so that
documents are encrypted before leaving the client machine.
"""

import base64
import json
import os
import struct
import time
from typing import Optional

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, utils
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class LegalDocsClient:
    """Client SDK for the LegalDocs pipeline.

    Fetches attestation from the parent API, verifies the enclave's
    identity via CA certificate chain, and encrypts documents using
    hybrid RSA/AES envelope encryption.
    """

    def __init__(
        self,
        api_url: str,
        tenant_id: str,
        ca_cert: Optional[str] = None,
        client_cert: Optional[str] = None,
        client_key: Optional[str] = None,
    ) -> None:
        """Configure API connection.

        Args:
            api_url: Base URL of the parent API (e.g. http://localhost:8000).
            tenant_id: Tenant identifier sent via X-Tenant-ID header.
            ca_cert: Path to CA certificate for TLS verification (optional).
            client_cert: Path to client certificate for mTLS (optional).
            client_key: Path to client private key for mTLS (optional).
        """
        self.api_url = api_url.rstrip("/")
        self.tenant_id = tenant_id
        self.ca_cert = ca_cert
        self.client_cert = client_cert
        self.client_key = client_key

    def _tenant_headers(self) -> dict:
        """Return headers containing the tenant identifier."""
        return {"X-Tenant-ID": self.tenant_id}

    def get_attestation(self) -> dict:
        """Fetch attestation document from the parent API.

        Returns:
            Attestation dict with keys: payload, signature, certificate.
        """
        url = f"{self.api_url}/attestation"
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def verify_attestation(attestation: dict, ca_cert_path: str) -> bytes:
        """Verify attestation signature against the CA certificate.

        Loads the enclave certificate from the attestation, verifies it
        was signed by the CA, then verifies the attestation signature
        using the enclave certificate's public key.

        Args:
            attestation: Attestation dict from get_attestation().
            ca_cert_path: Path to the CA certificate PEM file.

        Returns:
            The enclave's RSA public key PEM bytes (from the attestation
            payload, suitable for encrypt_document).

        Raises:
            ValueError: If verification fails.
        """
        # Load CA certificate
        with open(ca_cert_path, "rb") as f:
            ca_cert = x509.load_pem_x509_certificate(f.read())

        # Load enclave certificate from attestation
        enclave_cert_pem = attestation["certificate"].encode("utf-8")
        enclave_cert = x509.load_pem_x509_certificate(enclave_cert_pem)

        # Verify enclave cert was signed by the CA
        ca_public_key = ca_cert.public_key()
        try:
            ca_public_key.verify(
                enclave_cert.signature,
                enclave_cert.tbs_certificate_bytes,
                padding.PKCS1v15(),
                enclave_cert.signature_hash_algorithm,
            )
        except Exception as exc:
            raise ValueError(
                "Enclave certificate was not signed by the trusted CA"
            ) from exc

        # Verify attestation signature against the enclave certificate's key
        signature = base64.b64decode(attestation["signature"])
        payload_bytes = json.dumps(
            attestation["payload"], separators=(",", ":"), sort_keys=True
        ).encode("utf-8")

        enclave_public_key = enclave_cert.public_key()
        try:
            enclave_public_key.verify(
                signature,
                payload_bytes,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except Exception as exc:
            raise ValueError(
                "Attestation signature verification failed"
            ) from exc

        # Return the public key from the attestation payload
        public_key_pem = attestation["payload"]["public_key"].encode("utf-8")
        # Validate it loads correctly
        serialization.load_pem_public_key(public_key_pem)

        return public_key_pem

    @staticmethod
    def encrypt_document(plaintext: bytes, public_key_pem: bytes) -> bytes:
        """Envelope-encrypt a document using the enclave's public key.

        Wire format (binary):
          [4 bytes: encrypted_dek_length (big-endian)]
          [N bytes: RSA-OAEP encrypted DEK]
          [12 bytes: AES-GCM nonce]
          [remaining: AES-256-GCM ciphertext with appended 16-byte auth tag]

        Args:
            plaintext: Document bytes to encrypt.
            public_key_pem: PEM-encoded RSA public key from the enclave.

        Returns:
            Binary envelope-encrypted blob.
        """
        # Load the public key
        public_key = serialization.load_pem_public_key(public_key_pem)

        # Generate random 32-byte AES key (DEK) and 12-byte nonce
        dek = os.urandom(32)
        nonce = os.urandom(12)

        # Encrypt plaintext with AES-256-GCM (ciphertext includes 16-byte tag)
        aesgcm = AESGCM(dek)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)

        # Encrypt DEK with RSA-OAEP (SHA-256, MGF1-SHA-256)
        encrypted_dek = public_key.encrypt(
            dek,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

        # Pack into wire format
        header = struct.pack(">I", len(encrypted_dek))
        return header + encrypted_dek + nonce + ciphertext

    def submit_document(
        self,
        ciphertext: bytes,
        filename: Optional[str] = None,
    ) -> str:
        """Submit an encrypted document for processing.

        Base64-encodes the ciphertext and POSTs it to the parent API.
        Tenant is identified via the X-Tenant-ID header.

        Args:
            ciphertext: Encrypted document bytes (from encrypt_document).
            filename: Optional original filename.

        Returns:
            Document ID string assigned by the server.
        """
        b64_content = base64.b64encode(ciphertext).decode("utf-8")
        payload: dict = {
            "encrypted_content": b64_content,
        }
        if filename is not None:
            payload["filename"] = filename

        url = f"{self.api_url}/documents"
        response = requests.post(
            url, json=payload, headers=self._tenant_headers(), timeout=30
        )
        response.raise_for_status()
        return response.json()["id"]

    def get_document_status(self, document_id: str) -> dict:
        """Fetch the current status of a submitted document.

        Args:
            document_id: The document ID returned by submit_document.

        Returns:
            Full response dict including id, status, encrypted_result,
            error_message, created_at, and updated_at fields.
        """
        url = f"{self.api_url}/documents/{document_id}"
        response = requests.get(
            url, headers=self._tenant_headers(), timeout=30
        )
        response.raise_for_status()
        return response.json()

    def list_documents(self, status: Optional[str] = None) -> list[dict]:
        """List documents for the current tenant.

        Args:
            status: Optional status filter (e.g. "COMPLETED", "PENDING").

        Returns:
            List of document dicts for the tenant.
        """
        url = f"{self.api_url}/documents"
        params: dict = {}
        if status is not None:
            params["status"] = status
        response = requests.get(
            url, params=params, headers=self._tenant_headers(), timeout=30
        )
        response.raise_for_status()
        return response.json()["documents"]

    def get_document_results(
        self,
        document_id: str,
        poll_interval: float = 1.0,
        max_retries: int = 60,
    ) -> dict:
        """Poll for document processing completion and return decrypted results.

        Repeatedly checks document status until it reaches COMPLETED or
        FAILED. On completion, decrypts the encrypted result via the
        parent API's /decrypt proxy endpoint.

        Args:
            document_id: The document ID returned by submit_document.
            poll_interval: Seconds between status polls (default 1.0).
            max_retries: Maximum number of poll attempts (default 60).

        Returns:
            Parsed extraction dict with ``parties`` and ``dates`` lists.

        Raises:
            RuntimeError: If processing fails or polling times out.
        """
        for _ in range(max_retries):
            status_resp = self.get_document_status(document_id)
            status = status_resp.get("status")

            if status == "COMPLETED":
                encrypted_result_b64 = status_resp.get("encrypted_result")
                if encrypted_result_b64 is None:
                    raise RuntimeError(
                        "Document completed but no encrypted_result returned"
                    )
                return self.decrypt_results(encrypted_result_b64)

            if status == "FAILED":
                error_msg = status_resp.get("error_message", "Unknown error")
                raise RuntimeError(
                    f"Document processing failed: {error_msg}"
                )

            time.sleep(poll_interval)

        raise RuntimeError(
            f"Timed out waiting for document {document_id} after "
            f"{max_retries} retries"
        )

    def decrypt_results(self, encrypted_result_b64: str) -> dict:
        """Decrypt an encrypted result blob via the parent API proxy.

        Posts the base64-encoded encrypted data to the ``/decrypt``
        endpoint, which forwards it to the enclave for decryption.

        Args:
            encrypted_result_b64: Base64-encoded encrypted result blob.

        Returns:
            Parsed JSON dict (e.g. ``{"parties": [...], "dates": [...]}``)
        """
        url = f"{self.api_url}/decrypt"
        payload = {"encrypted_data": encrypted_result_b64}
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()

        decrypted_b64 = response.json()["decrypted_data"]
        decrypted_bytes = base64.b64decode(decrypted_b64)
        return json.loads(decrypted_bytes)
