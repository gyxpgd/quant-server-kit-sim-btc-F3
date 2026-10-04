"""Small, portable standard-library utilities for the offline F1 research pack."""
import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

MINUTE = 60000
HOUR = 60 * MINUTE
DAY = 24 * HOUR
BJT = timezone(timedelta(hours=8))


def timestamp(value):
    dt = datetime.fromisoformat(value)
    return int(dt.replace(tzinfo=BJT).timestamp() * 1000)


def iso(value):
    return datetime.fromtimestamp(value / 1000, BJT).isoformat()


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            digest.update(block)
    return digest.hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    tmp.replace(path)
