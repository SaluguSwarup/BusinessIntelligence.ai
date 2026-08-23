"""Loading, validating and caching a user's uploaded structured dataset."""
from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from ..config import get_settings
from ..db.repositories import DatasetRepository
from ..engines.metrics import DatasetSchema, detect_schema, prepare
from .object_storage import get_object_storage

_CACHE: Dict[str, Tuple[float, pd.DataFrame, DatasetSchema]] = {}


class DatasetError(ValueError):
    pass


def read_csv_bytes(raw: bytes) -> pd.DataFrame:
    from io import BytesIO

    for kwargs in ({}, {"sep": ";"}, {"sep": "\t"}, {"encoding": "latin-1"}):
        try:
            df = pd.read_csv(BytesIO(raw), **kwargs)
            if df.shape[1] > 1:
                return df
        except Exception:
            continue
    raise DatasetError("Could not parse the file as CSV. Check the delimiter and encoding.")


def validate_and_describe(df: pd.DataFrame) -> DatasetSchema:
    if len(df) == 0:
        raise DatasetError("The uploaded file has no rows.")
    try:
        schema = detect_schema(df)
    except ValueError as exc:
        raise DatasetError(str(exc)) from exc
    if not schema.available_kpis:
        raise DatasetError(
            "No numeric metric columns found. At minimum include a 'revenue' column "
            "(see sample_data/DATA_FORMAT.md)."
        )
    return schema


def store_upload(uid: str, filename: str, raw: bytes) -> Dict[str, Any]:
    s = get_settings()
    df = read_csv_bytes(raw)
    schema = validate_and_describe(df)

    digest = hashlib.sha256(raw).hexdigest()[:16]
    safe_name = os.path.basename(filename).replace(" ", "_") or "dataset.csv"
    # A unique key ensures deleting one repeated upload cannot remove another.
    key = f"{s.object_storage_prefix.strip('/')}/{uid}/{digest}_{uuid.uuid4().hex[:12]}_{safe_name}"
    get_object_storage().put(key, raw)

    repo = DatasetRepository()
    meta = {
        "filename": safe_name,
        "storage_key": key,
        "storage_backend": s.object_storage_backend,
        "size_bytes": len(raw),
        "checksum": digest,
        "schema": schema.to_dict(),
        "is_active": True,
    }
    ds = repo.create(uid, meta)
    repo.set_active(uid, ds["_id"])
    return ds


def load(dataset: Dict[str, Any]) -> Tuple[pd.DataFrame, DatasetSchema]:
    """Load + prepare a dataset, cached by immutable dataset checksum."""
    key = dataset.get("storage_key")
    if not key:  # legacy local records, retained to avoid breaking development data
        key = dataset.get("path", "")
    cache_key = dataset.get("checksum") or key
    cached = _CACHE.get(cache_key)
    if cached:
        return cached[1], cached[2]
    try:
        raw = get_object_storage().get(key) if dataset.get("storage_key") else open(key, "rb").read()
    except (OSError, FileNotFoundError) as exc:
        raise DatasetError("The stored dataset file is missing. Please re-upload it.") from exc
    df_raw = read_csv_bytes(raw)
    schema = detect_schema(df_raw)
    df = prepare(df_raw, schema)
    _CACHE[cache_key] = (0.0, df, schema)
    return df, schema


def clear_cache(key: Optional[str] = None) -> None:
    if key:
        _CACHE.pop(key, None)
    else:
        _CACHE.clear()
