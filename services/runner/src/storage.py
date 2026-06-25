"""MinIO upload helpers."""
from __future__ import annotations

import io
import os
from pathlib import Path

from minio import Minio


def _client() -> Minio:
    return Minio(
        os.environ.get("MINIO_ENDPOINT", "minio:9000"),
        access_key=os.environ["MINIO_ROOT_USER"],
        secret_key=os.environ["MINIO_ROOT_PASSWORD"],
        secure=False,
    )


def ensure_bucket(bucket: str) -> None:
    c = _client()
    if not c.bucket_exists(bucket):
        c.make_bucket(bucket)


def upload_file(bucket: str, key: str, path: Path, content_type: str = "application/octet-stream") -> str:
    """Upload a single file. Returns the s3:// URL."""
    c = _client()
    c.fput_object(bucket, key, str(path), content_type=content_type)
    return f"s3://{bucket}/{key}"


def upload_bytes(bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    c = _client()
    c.put_object(bucket, key, io.BytesIO(data), length=len(data), content_type=content_type)
    return f"s3://{bucket}/{key}"
