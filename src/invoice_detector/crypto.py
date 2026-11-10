"""Field-level encryption: AES-256-GCM with key IDs (rotation), a blind index,
and a separate derived key per company."""
import base64
import hashlib
import hmac
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def _derive(key: bytes, purpose: str, org_id: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=f"invoice-detector/{purpose}/{org_id}".encode()).derive(key)


class Vault:
    def __init__(self, keys: dict[int, bytes], index_key: bytes):
        self.keys = keys
        self.current = max(keys)  # encrypt with the newest key, decrypt with any
        self._index_key = index_key

    @classmethod
    def for_org(cls, keys: dict[int, bytes], index_key: bytes, org_id: str) -> "Vault":
        """Each company gets its own keys, derived from the master keys with HKDF.
        One company's ciphertext cannot be decrypted with another company's vault, and
        the same bank account produces a different blind index in each company."""
        return cls({kid: _derive(k, "enc", org_id) for kid, k in keys.items()},
                   _derive(index_key, "idx", org_id))

    def encrypt(self, plaintext: str, context: str) -> str:
        """`context` (e.g. 'invoices.bank:INV-1') is authenticated, so a ciphertext
        copied to another row or column fails to decrypt."""
        nonce = secrets.token_bytes(12)
        ct = AESGCM(self.keys[self.current]).encrypt(nonce, plaintext.encode(), context.encode())
        return f"{self.current}:{base64.b64encode(nonce + ct).decode()}"

    def decrypt(self, token: str, context: str) -> str:
        try:
            kid, b64 = token.split(":", 1)
            blob = base64.b64decode(b64)
            return AESGCM(self.keys[int(kid)]).decrypt(
                blob[:12], blob[12:], context.encode()).decode()
        except (InvalidTag, ValueError, KeyError) as exc:
            raise ValueError("Decryption failed: wrong key, wrong context or tampered data") from exc

    def rotate(self, token: str, context: str) -> str:
        return self.encrypt(self.decrypt(token, context), context)

    def blind_index(self, value: str) -> str:
        """Keyed hash for exact-match search and shared-account detection
        without decrypting. Reveals equality only."""
        return hmac.new(self._index_key, value.encode(), hashlib.sha256).hexdigest()
