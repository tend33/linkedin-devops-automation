"""Encrypted single-owner credentials. No profiles, posts or engagement are collected."""
import json
import os
import re
import time
from pathlib import Path
from cryptography.fernet import Fernet
from publishing import atomic_json


def token_path(base):
    return Path(base) / 'private' / 'linkedin-token.json'


def save_token(base, access_token, expires_in, member_id, scopes):
    if 'w_member_social' not in scopes or not re.fullmatch(r'[A-Za-z0-9_-]+', member_id):
        raise ValueError('Member identity or publishing permission is missing')
    expires_in = int(expires_in)
    if expires_in <= 0:
        raise ValueError('Token is expired')
    encrypted = Fernet(os.environ['TOKEN_ENCRYPTION_KEY'].encode()).encrypt(access_token.encode()).decode()
    atomic_json(token_path(base), {'token': encrypted, 'expires_at': time.time() + expires_in,
                                  'member_id': member_id, 'scopes': sorted(scopes), 'consented_at': time.time()})


def credentials(base):
    path = token_path(base)
    # A tombstone prevents falling back to an old environment token after disconnect.
    if (path.parent / 'disconnected').exists():
        raise ValueError('LinkedIn is disconnected; reconnect with OAuth')
    if path.exists():
        data = json.loads(path.read_text())
        token = Fernet(os.environ['TOKEN_ENCRYPTION_KEY'].encode()).decrypt(data['token'].encode()).decode()
    else:
        data = {'expires_at': float(os.environ.get('LINKEDIN_TOKEN_EXPIRES_AT', '0')),
                'member_id': os.environ.get('LINKEDIN_MEMBER_ID', ''),
                'scopes': os.environ.get('LINKEDIN_SCOPES', '').split()}
        token = os.environ.get('LINKEDIN_TOKEN', '')
    if not token or data['expires_at'] <= time.time() + 60:
        raise ValueError('LinkedIn token missing or expired; reconnect before publishing')
    if 'w_member_social' not in data['scopes'] or not re.fullmatch(r'[A-Za-z0-9_-]+', data['member_id']):
        raise ValueError('Configure the authorized member ID and w_member_social scope')
    return token, 'urn:li:person:' + data['member_id']


def disconnect(base):
    path = token_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path.parent / 'disconnected', {'disconnected_at': time.time()})
    path.unlink(missing_ok=True)
    for approval in (Path(base) / 'published').glob('*.approved.json'):
        approval.unlink()
