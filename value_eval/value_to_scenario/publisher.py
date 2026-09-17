from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional


def publish_directory(
    *,
    source: Path,
    target: Path,
    archive_root: Path,
    fingerprint: str,
) -> Optional[Path]:
    """发布已通过校验的目录，并保留发布前的旧输出。"""
    if not source.is_dir():
        raise FileNotFoundError(f"publish source does not exist: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    archive_root.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.publish-{fingerprint}"
    if temporary.exists():
        shutil.rmtree(temporary)
    shutil.copytree(source, temporary)

    archived: Optional[Path] = None
    if target.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archived = archive_root / f"{target.name}_{timestamp}"
        suffix = 1
        while archived.exists():
            archived = archive_root / f"{target.name}_{timestamp}_{suffix:02d}"
            suffix += 1
        shutil.move(str(target), str(archived))
    try:
        os.replace(temporary, target)
    except Exception:
        if archived is not None and archived.exists() and not target.exists():
            shutil.move(str(archived), str(target))
        raise
    return archived
