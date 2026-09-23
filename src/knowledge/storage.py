"""Staging on the destination filesystem with its normal inherited permissions.

Python 3.13+ mkdtemp uses a private Windows ACL. Renaming such a directory into
persistent storage keeps that ACL, so later restricted app sessions cannot read
it. A normal mkdir inherits the application's data-directory permissions.
"""
from contextlib import contextmanager
from pathlib import Path
import shutil
import uuid


@contextmanager
def staging_directory(parent: Path, prefix: str):
    parent = parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    directory = parent / f".{prefix}-{uuid.uuid4().hex}"
    directory.mkdir()
    try:
        yield directory
    finally:
        # Only this newly created, resolved child can be removed; publication by
        # rename leaves nothing at the staging path. Never remove the destination.
        if directory.exists():
            if directory.is_symlink() or not directory.resolve().is_relative_to(parent):
                raise ValueError("staging directory escapes parent")
            shutil.rmtree(directory)
