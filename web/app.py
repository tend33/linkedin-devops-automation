import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests
from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash

# Support both `python web/app.py` and WSGI imports.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from publishing import approve, atomic_json, draft_path, snapshot, state_paths
from linkedin_auth import credentials, disconnect, save_token, token_path

BASE_DIR = Path(os.environ.get('APP_BASE_DIR', str(ROOT))).resolve()
DRAFT_DIR = BASE_DIR / 'drafts'
IMAGE_DIR = BASE_DIR / 'images'
app = Flask(__name__)
secret = os.environ.get('FLASK_SECRET_KEY', '')
password_hash = os.environ.get('DASHBOARD_PASSWORD_HASH', '')
if len(secret) < 32 or secret == 'change-this-later' or not password_hash.startswith(('scrypt:', 'pbkdf2:')):
    raise RuntimeError('Set a random FLASK_SECRET_KEY (32+ characters) and DASHBOARD_PASSWORD_HASH')
app.config.update(SECRET_KEY=secret, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', 'true').lower() == 'true',
                  PERMANENT_SESSION_LIFETIME=timedelta(minutes=30), MAX_CONTENT_LENGTH=64 * 1024)


def csrf_token():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    return session['csrf']
app.jinja_env.globals['csrf_token'] = csrf_token


@app.before_request
def protect():
    if request.method == 'POST':
        if not hmac.compare_digest(session.get('csrf', ''), request.form.get('csrf_token', '')) or not session.get('csrf'):
            abort(400, 'Invalid CSRF token')
    if request.endpoint not in {'login', 'static', 'privacy', 'terms'} and not session.get('owner'):
        return redirect(url_for('login'))


@app.after_request
def headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'none'; frame-ancestors 'none'; form-action 'self' https://www.linkedin.com"
    if app.config['SESSION_COOKIE_SECURE']:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response


def login_attempt():
    # Shared across WSGI workers, no trust in client-supplied forwarded IP headers.
    private = BASE_DIR / 'private'
    private.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(private / 'login-rate.sqlite') as db:
        db.execute('CREATE TABLE IF NOT EXISTS attempts (ip TEXT PRIMARY KEY, window INTEGER, count INTEGER)')
        window = int(time.time() // 900)
        db.execute('DELETE FROM attempts WHERE window < ?', (window,))
        db.execute('INSERT INTO attempts VALUES (?, ?, 1) ON CONFLICT(ip) DO UPDATE SET count=count+1', (request.remote_addr or 'unknown', window))
        count = db.execute('SELECT count FROM attempts WHERE ip=?', (request.remote_addr or 'unknown',)).fetchone()[0]
    return count <= 10


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if not login_attempt():
            abort(429, 'Too many login attempts. Try again in 15 minutes.')
        if check_password_hash(password_hash, request.form.get('password', '')):
            session.clear()
            session['owner'] = os.environ.get('DASHBOARD_USERNAME', 'owner')
            session.permanent = True
            return redirect(url_for('dashboard'))
        flash('Invalid password.')
    return render_template('login.html')


@app.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
def dashboard():
    drafts = []
    for path in sorted(DRAFT_DIR.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(draft_path(BASE_DIR, path.name).read_text())
            drafts.append({'filename': path.name, 'prefix': path.stem,
                           'post_text': data.get('post_text', ''), 'published': state_paths(BASE_DIR, path.name)[2].exists()})
        except (ValueError, OSError):
            continue
    return render_template('dashboard.html', drafts=drafts)


def run_cli(args):
    return subprocess.run([sys.executable, str(ROOT / 'linkedin_auto.py'), '--base-dir', str(BASE_DIR), *args],
                          capture_output=True, text=True, timeout=600)


@app.route('/generate', methods=['GET', 'POST'])
def generate():
    if request.method == 'POST':
        topic = request.form.get('topic', '').strip()
        args = ['--auto-topic'] if request.form.get('auto_topic') else ['--topic', topic]
        if not topic and not request.form.get('auto_topic'):
            abort(400, 'Enter a topic')
        try:
            result = run_cli(args)
            flash('Draft generated.' if result.returncode == 0 else 'Generation failed; check private server logs.')
        except subprocess.TimeoutExpired:
            flash('Generation timed out; check the draft list before trying again.')
        return redirect(url_for('dashboard'))
    return render_template('generate.html')


def get_snapshot(filename, text_only=False):
    _, author = credentials(BASE_DIR)
    return author, snapshot(BASE_DIR, filename, author, text_only)


@app.get('/preview/<filename>')
def preview(filename):
    try:
        path = draft_path(BASE_DIR, filename)
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        abort(404)
    image = IMAGE_DIR / (path.stem + '.png')
    text_only = not image.is_file()
    digest = author = None
    try:
        author, (_, _, digest) = get_snapshot(filename, text_only)
    except (ValueError, KeyError):
        flash('Connect LinkedIn before approving this draft.')
    # Restrict untrusted AI-provided links to HTTP(S).
    if isinstance(data.get('research'), dict):
        data['research']['sources'] = [s for s in data['research'].get('sources', [])
                                     if isinstance(s, dict) and urlparse(str(s.get('url', ''))).scheme in {'http', 'https'}]
    _, lock, marker = state_paths(BASE_DIR, filename)
    revision = __import__('hashlib').sha256(path.read_bytes()).hexdigest()
    return render_template('preview.html', draft=data, filename=filename, image_name=image.name if not text_only else None,
                           published=marker.exists(), publishing=lock.exists(), digest=digest, author=author,
                           text_only=text_only, revision=revision)


@app.post('/edit/<filename>')
def edit(filename):
    try:
        path = draft_path(BASE_DIR, filename)
        _, lock, marker = state_paths(BASE_DIR, filename)
        if lock.exists() or marker.exists():
            abort(409, 'Published or in-progress drafts cannot be edited')
        if __import__('hashlib').sha256(path.read_bytes()).hexdigest() != request.form.get('revision'):
            abort(409, 'Draft changed; reload before editing')
        data = json.loads(path.read_text())
        text = request.form.get('post_text', '').strip()
        if not 1 <= len(text) <= 3000:
            abort(400, 'Post must contain 1–3000 characters')
        data.update(post_text=text, alt_text=request.form.get('alt_text', ''), image_title=request.form.get('image_title', ''))
        atomic_json(path, data)
        path.with_suffix('.txt').write_text(text)
        state_paths(BASE_DIR, filename)[0].unlink(missing_ok=True)
        flash('Saved. Review the updated draft before publishing.')
    except (ValueError, OSError):
        abort(400, 'Unable to edit draft')
    return redirect(url_for('preview', filename=filename))


@app.post('/publish/<filename>')
def publish(filename):
    if request.form.get('confirm') != 'yes':
        abort(400, 'Explicit approval is required')
    text_only = request.form.get('text_only') == 'true'
    try:
        author, _ = get_snapshot(filename, text_only)
        approve(BASE_DIR, filename, author, request.form.get('digest', ''), session['owner'], text_only)
        args = ['--draft-json', str(draft_path(BASE_DIR, filename)), '--publish']
        if text_only:
            args.append('--text-only')
        result = run_cli(args)
        flash('Published to LinkedIn.' if result.returncode == 0 else 'Publishing stopped. Check LinkedIn and private logs before any retry; never delete a safety lock blindly.')
    except (ValueError, OSError, KeyError):
        flash('Approval or publishing failed. Reconnect if needed and review the current draft.')
    except subprocess.TimeoutExpired:
        flash('Publishing timed out. Check LinkedIn before any retry.')
    return redirect(url_for('preview', filename=filename))


@app.get('/images/<filename>')
def images(filename):
    path = IMAGE_DIR / filename
    if Path(filename).name != filename or path.is_symlink():
        abort(404)
    return send_from_directory(IMAGE_DIR, filename)


@app.route('/linkedin/connect', methods=['GET', 'POST'])
def connect():
    if request.method == 'GET':
        return render_template('connect.html')
    if request.form.get('consent') != 'yes':
        abort(400, 'Consent is required')
    redirect_uri = os.environ['LINKEDIN_REDIRECT_URI']
    parsed = urlparse(redirect_uri)
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1'}):
        abort(400, 'Use an HTTPS redirect URI')
    state = secrets.token_urlsafe(32)
    # Persist one-use state server-side; replay cannot be authorized by replaying an old session cookie.
    atomic_json(BASE_DIR / 'private' / ('oauth-' + state + '.json'), {'created': time.time(), 'owner': session['owner']})
    session['oauth_state'] = state
    query = urlencode({'response_type': 'code', 'client_id': os.environ['LINKEDIN_CLIENT_ID'],
                       'redirect_uri': redirect_uri, 'scope': 'openid profile w_member_social', 'state': state})
    return redirect('https://www.linkedin.com/oauth/v2/authorization?' + query)


@app.get('/linkedin/callback')
def callback():
    expected = session.pop('oauth_state', '')
    if not expected or not hmac.compare_digest(expected, request.args.get('state', '')):
        abort(400, 'Invalid OAuth state')
    state_path = BASE_DIR / 'private' / ('oauth-' + expected + '.json')
    # Atomic rename consumes the state even across workers.
    claimed = state_path.with_suffix('.used')
    try:
        state_path.rename(claimed)
        state = json.loads(claimed.read_text())
        claimed.unlink()
        if time.time() - state['created'] > 600 or state['owner'] != session['owner']:
            abort(400, 'OAuth state expired')
    except FileNotFoundError:
        abort(400, 'OAuth state already used')
    if request.args.get('error') or not request.args.get('code'):
        flash('LinkedIn authorization was cancelled.')
        return redirect(url_for('dashboard'))
    try:
        response = requests.post('https://www.linkedin.com/oauth/v2/accessToken', data={
            'grant_type': 'authorization_code', 'code': request.args['code'],
            'redirect_uri': os.environ['LINKEDIN_REDIRECT_URI'], 'client_id': os.environ['LINKEDIN_CLIENT_ID'],
            'client_secret': os.environ['LINKEDIN_CLIENT_SECRET']}, timeout=30, allow_redirects=False)
        if response.status_code != 200:
            raise ValueError('Token exchange rejected')
        data = response.json()
        scopes = set(data.get('scope', '').split())
        if not {'openid', 'profile', 'w_member_social'}.issubset(scopes):
            raise ValueError('Required scopes missing')
        identity = requests.get('https://api.linkedin.com/v2/userinfo', headers={'Authorization': 'Bearer ' + data['access_token']},
                                timeout=30, allow_redirects=False)
        if identity.status_code != 200:
            raise ValueError('Member identity unavailable')
        save_token(BASE_DIR, data['access_token'], data['expires_in'], identity.json()['sub'], scopes)
        (token_path(BASE_DIR).parent / 'disconnected').unlink(missing_ok=True)
        flash('LinkedIn connected. Review and approve each post before publishing.')
    except (requests.RequestException, ValueError, KeyError):
        flash('LinkedIn connection failed. Check app permissions and configuration; no credentials were logged.')
    return redirect(url_for('dashboard'))


@app.post('/linkedin/disconnect')
def disconnect_account():
    disconnect(BASE_DIR)
    flash('Local LinkedIn credentials and pending approvals deleted. Also revoke this app in LinkedIn Settings → Data privacy → Other applications.')
    return redirect(url_for('dashboard'))


@app.get('/privacy')
def privacy():
    return render_template('policy.html', kind='Privacy notice')


@app.get('/terms')
def terms():
    return render_template('policy.html', kind='User agreement')


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
