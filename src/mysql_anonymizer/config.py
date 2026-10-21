from __future__ import annotations

import json
import os
import tempfile
from typing import Any

import mysql.connector


def load_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return obj


def save_json_atomic(path: str, data: dict[str, Any]) -> None:
    target = os.path.abspath(path)
    directory = os.path.dirname(target)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
            temp_path = f.name
        os.replace(temp_path, target)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def connect(settings: dict[str, Any]):
    options = dict(settings)
    env = options.pop("password_env", None)
    if env:
        if env not in os.environ:
            raise ValueError(f"Environment variable {env} is not set")
        options["password"] = os.environ[env]
    # Prefer the Python implementation for long-running copy jobs. The optional
    # C extension runs inside the GUI process and a native crash bypasses the
    # worker's exception handling and traceback logging. Advanced users can
    # explicitly set use_pure=false in connection settings to opt back in.
    options.setdefault("use_pure", True)
    return mysql.connector.connect(**options)
