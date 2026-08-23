"""Durable storage for uploaded datasets.

MongoDB GridFS is the production default, avoiding a second storage account.
The S3 protocol is also supported for installations that later need dedicated
object storage. Local disk remains available for demos only.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Optional

from ..config import get_settings


class ObjectStorage(ABC):
    @abstractmethod
    def put(self, key: str, raw: bytes) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...


class LocalObjectStorage(ObjectStorage):
    def __init__(self, root: str):
        self.root = root

    def _path(self, key: str) -> str:
        path = os.path.abspath(os.path.join(self.root, key))
        root = os.path.abspath(self.root)
        if os.path.commonpath([root, path]) != root:
            raise ValueError("invalid storage key")
        return path

    def put(self, key: str, raw: bytes) -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(raw)

    def get(self, key: str) -> bytes:
        try:
            with open(self._path(key), "rb") as fh:
                return fh.read()
        except FileNotFoundError as exc:
            raise FileNotFoundError("The stored dataset file is missing. Please re-upload it.") from exc

    def delete(self, key: str) -> None:
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass


class S3ObjectStorage(ObjectStorage):
    def __init__(self):
        s = get_settings()
        if not s.object_storage_bucket:
            raise ValueError("OBJECT_STORAGE_BUCKET is required when OBJECT_STORAGE_BACKEND=s3")
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - dependency is installed in production
            raise RuntimeError("boto3 is required for S3 object storage") from exc
        self.bucket = s.object_storage_bucket
        self.client = boto3.client(
            "s3", region_name=s.object_storage_region or None,
            endpoint_url=s.object_storage_endpoint_url or None,
            aws_access_key_id=s.object_storage_access_key_id or None,
            aws_secret_access_key=s.object_storage_secret_access_key or None,
        )

    def put(self, key: str, raw: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=raw, ContentType="text/csv")

    def get(self, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc:
            raise FileNotFoundError("The stored dataset file is unavailable. Please re-upload it.") from exc

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


class GridFSObjectStorage(ObjectStorage):
    """Store CSVs in the same MongoDB deployment as their metadata."""
    def __init__(self):
        s = get_settings()
        if not s.mongodb_uri:
            raise ValueError("MONGODB_URI is required when OBJECT_STORAGE_BACKEND=gridfs")
        try:
            from gridfs import GridFSBucket
            from pymongo import MongoClient
        except ImportError as exc:  # pragma: no cover - pymongo is required
            raise RuntimeError("pymongo is required for GridFS storage") from exc
        self.client = MongoClient(s.mongodb_uri, serverSelectionTimeoutMS=5000)
        self.bucket = GridFSBucket(self.client[s.mongodb_db], bucket_name="datasets")

    def put(self, key: str, raw: bytes) -> None:
        from io import BytesIO
        self.bucket.upload_from_stream(
            key, BytesIO(raw), metadata={"content_type": "text/csv"},
        )

    def get(self, key: str) -> bytes:
        try:
            return self.bucket.open_download_stream_by_name(key).read()
        except Exception as exc:
            raise FileNotFoundError("The stored dataset file is unavailable. Please re-upload it.") from exc

    def delete(self, key: str) -> None:
        # Keys are unique per upload. Delete all matches defensively in case an
        # interrupted retry left an orphaned GridFS file with the same name.
        for item in self.bucket.find({"filename": key}):
            self.bucket.delete(item._id)


@lru_cache
def get_object_storage() -> ObjectStorage:
    s = get_settings()
    if s.object_storage_backend == "local":
        return LocalObjectStorage(s.upload_dir)
    if s.object_storage_backend == "s3":
        return S3ObjectStorage()
    if s.object_storage_backend == "gridfs":
        return GridFSObjectStorage()
    raise ValueError("OBJECT_STORAGE_BACKEND must be 'local', 'gridfs', or 's3'")


def reset_object_storage() -> None:
    get_object_storage.cache_clear()
