import secrets

from invoice_detector.config import Settings
from invoice_detector.crypto import Vault
from invoice_detector.db import Database


def make_settings(db_path=":memory:", api_key=None):
    return Settings(secret_key="test-secret-" + "x" * 40,
                    encryption_keys={1: secrets.token_bytes(32)},
                    index_key=secrets.token_bytes(32),
                    db_path=db_path, anthropic_api_key=api_key)


def make_db():
    return Database(":memory:", Vault.from_settings(make_settings()))
