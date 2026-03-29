"""Cryptographic operations for the enclave.

Implements hybrid RSA/AES envelope encryption for legal documents.
Wire format (binary):
  [4 bytes: encrypted_dek_length (big-endian)]
  [N bytes: RSA-OAEP encrypted DEK]
  [12 bytes: AES-GCM nonce]
  [remaining: AES-256-GCM ciphertext with appended 16-byte auth tag]
"""

import os
import struct

from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CryptoManager:
    """Manages RSA key pair lifecycle and provides envelope encrypt/decrypt."""

    def __init__(self, key_size: int = 2048) -> None:
        self._private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=key_size,
        )
        self._public_key = self._private_key.public_key()

    def get_public_key_pem(self) -> bytes:
        """Return PEM-encoded public key."""
        return self._public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def decrypt_document(self, encrypted_blob: bytes) -> bytes:
        """Decrypt an envelope-encrypted document using the private key."""
        # Parse wire format
        if len(encrypted_blob) < 4:
            raise ValueError("Encrypted blob too short to contain DEK length header")

        dek_len = struct.unpack(">I", encrypted_blob[:4])[0]
        offset = 4

        if len(encrypted_blob) < offset + dek_len + 12:
            raise ValueError("Encrypted blob too short for declared DEK + nonce")

        encrypted_dek = encrypted_blob[offset : offset + dek_len]
        offset += dek_len

        nonce = encrypted_blob[offset : offset + 12]
        offset += 12

        ciphertext = encrypted_blob[offset:]

        # Decrypt DEK with RSA-OAEP
        dek = self._private_key.decrypt(
            encrypted_dek,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

        # Decrypt ciphertext with AES-256-GCM
        aesgcm = AESGCM(dek)
        return aesgcm.decrypt(nonce, ciphertext, None)

    @staticmethod
    def encrypt_document(plaintext: bytes, public_key_pem: bytes) -> bytes:
        """Envelope-encrypt a document using the given public key.

        Returns the binary wire format blob.
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
