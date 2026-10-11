"""One approval and publication boundary for CLI, Jenkins and the dashboard."""
import hashlib
import hmac
import json
import os
import re
import tempfile
import time
from pathlib import Path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def approval_key():
    key = os.environ.get('APPROVAL_SIGNING_KEY', '')
    if len(key) < 32:
        raise RuntimeError('Set APPROVAL_SIGNING_KEY to a random secret of at least 32 characters')
    return key.encode()


def draft_path(base, filename):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*\.json', filename):
        raise ValueError('Invalid draft filename')
    path = Path(base) / 'drafts' / filename
    if path.is_symlink() or not path.is_file():
        raise ValueError('Draft not found')
    return path


def snapshot(base, filename, author, text_only=False):
    path = draft_path(base, filename)
    plan = json.loads(path.read_text())
    text = plan.get('post_text')
    if not isinstance(text, str) or not text.strip() or len(text) > 3000:
        raise ValueError('Post text must contain 1–3000 characters')
    if not re.fullmatch(r'urn:li:person:[A-Za-z0-9_-]+', author):
        raise ValueError('Only the authenticated member account is supported')
    image = Path(base) / 'images' / (path.stem + '.png')
    image_bytes = None
    if not text_only:
        if image.is_symlink() or not image.is_file():
            raise ValueError('Image is missing; explicitly select text-only mode if intended')
        image_bytes = image.read_bytes()
    payload = {'post_text': text, 'alt_text': plan.get('alt_text'),
               'image_title': plan.get('image_title'), 'author': author,
               'visibility': 'PUBLIC', 'text_only': bool(text_only),
               'image_sha256': hashlib.sha256(image_bytes).hexdigest() if image_bytes is not None else None}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return plan, image_bytes, digest


def state_paths(base, filename):
    stem = Path(filename).stem
    return tuple(Path(base) / 'published' / (stem + suffix)
                 for suffix in ('.approved.json', '.publishing', '.published'))


def approve(base, filename, author, expected_digest, approved_by, text_only=False):
    _, _, digest = snapshot(base, filename, author, text_only)
    if not expected_digest or not hmac.compare_digest(digest, expected_digest):
        raise ValueError('Draft, image or account changed since review; review again')
    approval, lock, marker = state_paths(base, filename)
    if lock.exists() or marker.exists():
        raise ValueError('Draft already published or requires publication reconciliation')
    if not approved_by:
        raise ValueError('Approver is required')
    record = {'digest': digest, 'approved_by': approved_by, 'approved_at': time.time(),
              'expires_at': time.time() + 86400}
    record['signature'] = hmac.new(approval_key(), json.dumps(record, sort_keys=True).encode(), hashlib.sha256).hexdigest()
    atomic_json(approval, record)


def publish_approved(base, filename, author, publisher, text_only=False):
    # Snapshot bytes once: these exact bytes are checked and sent, avoiding a check/read race.
    plan, image_bytes, digest = snapshot(base, filename, author, text_only)
    approval, lock, marker = state_paths(base, filename)
    if marker.exists():
        raise ValueError('This draft was already published')
    record = json.loads(approval.read_text())
    signature = record.pop('signature', '')
    wanted = hmac.new(approval_key(), json.dumps(record, sort_keys=True).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, wanted) or record.get('digest') != digest or record.get('expires_at', 0) <= time.time():
        raise ValueError('Approval is invalid, expired or belongs to another draft version')
    lock.parent.mkdir(parents=True, exist_ok=True)
    # Never auto-remove a failed lock: the remote API may have accepted a timed-out request.
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({'digest': digest, 'started_at': time.time()}, f)
        f.flush()
        os.fsync(f.fileno())
    if marker.exists():
        raise ValueError('This draft was already published')
    result = publisher(plan, image_bytes)
    atomic_json(marker, {'digest': digest, 'approved_by': record['approved_by'],
                         'published_at': time.time(), **result})
    approval.unlink(missing_ok=True)
    lock.unlink()
    return result
