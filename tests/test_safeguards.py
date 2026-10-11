import importlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash
import publishing as p
import linkedin_auth as auth

AUTHOR = 'urn:li:person:test_member'


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv('APPROVAL_SIGNING_KEY', 'a' * 48)
    monkeypatch.setenv('TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    monkeypatch.setenv('LINKEDIN_TOKEN', 'test-token')
    monkeypatch.setenv('LINKEDIN_MEMBER_ID', 'test_member')
    monkeypatch.setenv('LINKEDIN_TOKEN_EXPIRES_AT', str(time.time() + 3600))
    monkeypatch.setenv('LINKEDIN_SCOPES', 'w_member_social')
    (tmp_path / 'drafts').mkdir()
    (tmp_path / 'images').mkdir()
    (tmp_path / 'drafts/post.json').write_text(json.dumps({'post_text': 'Reviewed post', 'alt_text': 'Original image'}))
    (tmp_path / 'images/post.png').write_bytes(b'original image')
    return tmp_path


def approved(data, text_only=False):
    digest = p.snapshot(data, 'post.json', AUTHOR, text_only)[2]
    p.approve(data, 'post.json', AUTHOR, digest, 'owner', text_only)
    return digest


def test_unapproved_cannot_send(data):
    send = Mock()
    with pytest.raises(FileNotFoundError):
        p.publish_approved(data, 'post.json', AUTHOR, send)
    send.assert_not_called()


@pytest.mark.parametrize('change', ['text', 'image', 'account', 'mode', 'tamper', 'expiry'])
def test_approval_rejects_changes(data, monkeypatch, change):
    approved(data)
    author, mode = AUTHOR, False
    if change == 'text':
        (data / 'drafts/post.json').write_text(json.dumps({'post_text': 'Changed'}))
    elif change == 'image':
        (data / 'images/post.png').write_bytes(b'changed')
    elif change == 'account':
        author = 'urn:li:person:other'
    elif change == 'mode':
        mode = True
    elif change == 'tamper':
        path = p.state_paths(data, 'post.json')[0]
        record = json.loads(path.read_text()); record['approved_by'] = 'attacker'; p.atomic_json(path, record)
    else:
        monkeypatch.setattr(p.time, 'time', lambda: 99999999999)
    send = Mock()
    with pytest.raises(ValueError):
        p.publish_approved(data, 'post.json', author, send, mode)
    send.assert_not_called()


def test_stale_review_cannot_approve(data):
    digest = p.snapshot(data, 'post.json', AUTHOR)[2]
    (data / 'images/post.png').write_bytes(b'new')
    with pytest.raises(ValueError):
        p.approve(data, 'post.json', AUTHOR, digest, 'owner')


def test_success_receipt_and_duplicate_rejection(data):
    approved(data)
    send = Mock(return_value={'post_id': 'urn:li:share:123'})
    p.publish_approved(data, 'post.json', AUTHOR, send)
    receipt = json.loads(p.state_paths(data, 'post.json')[2].read_text())
    assert receipt['post_id'] == 'urn:li:share:123'
    assert not p.state_paths(data, 'post.json')[1].exists()
    with pytest.raises(ValueError):
        p.publish_approved(data, 'post.json', AUTHOR, send)
    assert send.call_count == 1


def test_uncertain_failure_retains_lock(data):
    approved(data)
    send = Mock(side_effect=TimeoutError('timeout after remote acceptance'))
    with pytest.raises(TimeoutError):
        p.publish_approved(data, 'post.json', AUTHOR, send)
    assert p.state_paths(data, 'post.json')[1].exists()
    with pytest.raises(FileExistsError):
        p.publish_approved(data, 'post.json', AUTHOR, send)
    assert send.call_count == 1


def test_concurrent_publish_single_sender(data):
    approved(data)
    entered, release = Event(), Event()
    def send(plan, image):
        entered.set()
        assert release.wait(5)
        return {'post_id': '123'}
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(p.publish_approved, data, 'post.json', AUTHOR, send)
        assert entered.wait(5)
        with pytest.raises(FileExistsError):
            p.publish_approved(data, 'post.json', AUTHOR, Mock())
        release.set()
        first.result()


def test_legacy_markers_and_path_restrictions(data):
    p.state_paths(data, 'post.json')[2].parent.mkdir()
    p.state_paths(data, 'post.json')[2].write_text('PUBLISHED_BY=old-ui')
    with pytest.raises(ValueError):
        approved(data)
    with pytest.raises(ValueError):
        p.snapshot(data, '../post.json', AUTHOR)
    with pytest.raises(ValueError):
        p.snapshot(data, 'post.json', 'urn:li:organization:123')


def test_encrypted_token_expiry_disconnect(data, monkeypatch):
    auth.save_token(data, 'test-secret-token', 3600, 'test_member', {'openid', 'profile', 'w_member_social'})
    assert 'test-secret-token' not in auth.token_path(data).read_text()
    assert auth.credentials(data) == ('test-secret-token', AUTHOR)
    approved(data)
    auth.disconnect(data)
    assert not auth.token_path(data).exists()
    assert not p.state_paths(data, 'post.json')[0].exists()
    with pytest.raises(ValueError, match='disconnected'):
        auth.credentials(data)
    (auth.token_path(data).parent / 'disconnected').unlink()
    monkeypatch.setenv('LINKEDIN_TOKEN_EXPIRES_AT', '0')
    with pytest.raises(ValueError, match='expired'):
        auth.credentials(data)


@pytest.fixture
def web(data, monkeypatch):
    monkeypatch.setenv('APP_BASE_DIR', str(data))
    monkeypatch.setenv('FLASK_SECRET_KEY', 's' * 48)
    monkeypatch.setenv('DASHBOARD_PASSWORD_HASH', generate_password_hash('correct-password'))
    monkeypatch.setenv('SESSION_COOKIE_SECURE', 'false')
    monkeypatch.setenv('LINKEDIN_CLIENT_ID', 'client')
    monkeypatch.setenv('LINKEDIN_CLIENT_SECRET', 'secret')
    monkeypatch.setenv('LINKEDIN_REDIRECT_URI', 'http://localhost/linkedin/callback')
    sys.modules.pop('web.app', None)
    module = importlib.import_module('web.app')
    module.app.config['TESTING'] = True
    return module, module.app.test_client()


def owner(client):
    with client.session_transaction() as s:
        s['owner'] = 'owner'; s['csrf'] = 'csrf'
    return {'csrf_token': 'csrf'}


def test_dashboard_auth_csrf_and_login(web):
    mod, client = web
    for path in ('/', '/images/post.png', '/preview/post.json', '/linkedin/callback'):
        assert client.get(path).status_code == 302
    assert client.post('/publish/post.json').status_code == 400
    assert client.get('/privacy').status_code == 200
    client.get('/login')
    with client.session_transaction() as s:
        csrf = s['csrf']
    assert client.post('/login', data={'csrf_token': csrf, 'password': 'correct-password'}).status_code == 302
    assert client.get('/').status_code == 200
    assert client.post('/publish/post.json', data={'csrf_token': 'invalid'}).status_code == 400
    assert client.get('/').headers['X-Frame-Options'] == 'DENY'
    policy = client.get('/linkedin/connect').headers['Content-Security-Policy']
    assert "form-action 'self' https://www.linkedin.com" in policy
    assert "script-src 'none'" in policy


def test_edit_invalidation_stale_publish_and_preview(web, data, monkeypatch):
    mod, client = web
    csrf = owner(client)
    approved(data)
    response = client.get('/preview/post.json')
    assert response.status_code == 200 and b'privacy notice' in response.data
    revision = __import__('hashlib').sha256((data / 'drafts/post.json').read_bytes()).hexdigest()
    old_digest = p.snapshot(data, 'post.json', AUTHOR)[2]
    assert client.post('/edit/post.json', data={**csrf, 'revision': revision, 'post_text': 'Edited post'}).status_code == 302
    assert not p.state_paths(data, 'post.json')[0].exists()
    run = Mock(); monkeypatch.setattr(mod, 'run_cli', run)
    client.post('/publish/post.json', data={**csrf, 'confirm': 'yes', 'digest': old_digest})
    run.assert_not_called()
    assert client.post('/publish/post.json', data=csrf).status_code == 400
    assert client.post('/edit/post.json', data={**csrf, 'revision': revision, 'post_text': 'stale'}).status_code == 409


def test_login_rate_limit(web):
    _, client = web
    client.get('/login')
    with client.session_transaction() as s:
        csrf = s['csrf']
    for _ in range(10):
        assert client.post('/login', data={'csrf_token': csrf, 'password': 'bad'}).status_code == 200
    assert client.post('/login', data={'csrf_token': csrf, 'password': 'bad'}).status_code == 429


def test_oauth_state_consent_success_and_replay(web, data, monkeypatch):
    mod, client = web
    csrf = owner(client)
    assert client.post('/linkedin/connect', data=csrf).status_code == 400
    response = client.post('/linkedin/connect', data={**csrf, 'consent': 'yes'})
    assert response.status_code == 302
    with client.session_transaction() as s:
        state = s['oauth_state']
    assert client.get('/linkedin/callback?state=wrong&code=code').status_code == 400
    # Invalid state consumed the session state; start a new authorization.
    client.post('/linkedin/connect', data={**csrf, 'consent': 'yes'})
    with client.session_transaction() as s:
        state = s['oauth_state']
    monkeypatch.setattr(mod.requests, 'post', Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {'access_token':'oauth-secret', 'expires_in':3600, 'scope':'openid profile w_member_social'})))
    monkeypatch.setattr(mod.requests, 'get', Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {'sub':'test_member'})))
    assert client.get('/linkedin/callback?state=' + state + '&code=code').status_code == 302
    assert auth.credentials(data) == ('oauth-secret', AUTHOR)
    with client.session_transaction() as s:
        s['oauth_state'] = state
    assert client.get('/linkedin/callback?state=' + state + '&code=code').status_code == 400


def test_cli_blocks_generate_publish(data, monkeypatch):
    import linkedin_auto
    monkeypatch.setattr(sys, 'argv', ['linkedin_auto.py', '--topic', 'test', '--publish'])
    with pytest.raises(SystemExit) as exc:
        linkedin_auto.main()
    assert exc.value.code == 2


def test_create_does_not_retry_or_leak_error(monkeypatch):
    import linkedin_auto
    post = Mock(return_value=SimpleNamespace(status_code=429, text='secret response'))
    monkeypatch.setattr(linkedin_auto.requests, 'post', post)
    with pytest.raises(RuntimeError) as exc:
        linkedin_auto.create_linkedin_post('token', '202608', AUTHOR, 'post')
    assert 'secret' not in str(exc.value) and post.call_count == 1
    assert post.call_args.kwargs['allow_redirects'] is False


def test_cli_approved_text_publication_needs_no_ai_key(data, monkeypatch):
    import linkedin_auto as cli
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setenv('LINKEDIN_VERSION', '202608')
    approved(data, text_only=True)
    monkeypatch.setattr(sys, 'argv', ['linkedin_auto.py', '--base-dir', str(data), '--draft-json', str(data / 'drafts/post.json'), '--text-only', '--publish'])
    send = Mock(return_value=SimpleNamespace(status_code=201, text='', headers={'x-restli-id': 'urn:li:share:321'}))
    monkeypatch.setattr(cli.requests, 'post', send)
    cli.main()
    assert send.call_count == 1
    assert send.call_args.kwargs['json']['author'] == AUTHOR
    assert send.call_args.kwargs['json']['commentary'] == 'Reviewed post'
    assert json.loads(p.state_paths(data, 'post.json')[2].read_text())['post_id'] == 'urn:li:share:321'


def test_generation_needs_no_linkedin_credentials(tmp_path, monkeypatch):
    import linkedin_auto as cli
    for key in ('LINKEDIN_TOKEN', 'LINKEDIN_MEMBER_ID', 'LINKEDIN_VERSION'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    monkeypatch.setattr(sys, 'argv', ['linkedin_auto.py', '--base-dir', str(tmp_path), '--topic', 'DevOps example', '--text-only'])
    monkeypatch.setattr(cli, 'OpenAI', Mock())
    monkeypatch.setattr(cli, 'generate_content_plan', Mock(return_value={'post_text': 'A practical engineering lesson. ' * 20, 'alt_text': '', 'image_prompt': 'test'}))
    publish = Mock(); monkeypatch.setattr(cli, 'create_linkedin_post', publish)
    cli.main()
    assert len(list((tmp_path / 'drafts').glob('*.json'))) == 1
    publish.assert_not_called()
