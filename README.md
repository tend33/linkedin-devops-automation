# DevOps content assistant for LinkedIn

A private, single-owner workflow: scheduled AI draft generation → editable human review → explicit approval → official LinkedIn API publication. No browser automation, scraping, engagement automation, connection requests or messaging. This project is independent of LinkedIn and does not confer API access or a compliance certification.

## Setup

1. Install Docker Compose. Clone the repository into a private directory, copy `.env.example` to `.env`, and restrict `.env` permissions (`chmod 600 .env`). Keep the project/data directories private. Populate the AI key and chosen supported models.
2. Generate **independent** `FLASK_SECRET_KEY` and `APPROVAL_SIGNING_KEY`, a Fernet `TOKEN_ENCRYPTION_KEY`, and a hashed dashboard password using the commands in `.env.example`. Escape `$` as `$$` in Compose `.env` values or use single-quoted dotenv values (password hashes contain dollar signs). Never commit real keys. Dashboard startup fails without secure configuration.
3. Register/configure your LinkedIn developer app. Confirm approved Share on LinkedIn publishing permission (`w_member_social`) and Sign In with LinkedIn using OpenID Connect (`openid profile`); configure the exact callback URL in `LINKEDIN_REDIRECT_URI`. Do not request feed, connection, engagement or organization scopes. Set a currently supported `LINKEDIN_VERSION`; the example value is not a guarantee of availability.
4. `docker compose build` then `docker compose up -d dashboard`. The dashboard binds only to host localhost. Use an authenticated HTTPS reverse proxy/private network, or an SSH tunnel to localhost. For **localhost HTTP only**, set `SESSION_COOKIE_SECURE=false`; keep it true with HTTPS. Configure LinkedIn's redirect URI to the actual browser-visible callback. Do not expose Flask/Jenkins directly to the public internet.
5. Log in, read the privacy notice and user agreement, then connect through LinkedIn OAuth. A lite profile is fetched once from `userinfo`; only the member identifier is retained (name/photo fields are discarded); the access token is stored encrypted under `private/`. No token is sent to the AI provider. Reconnect when expired; automatic refresh is not assumed to be available to every app. Disconnect deletes local credentials and approvals and blocks legacy token fallback. Revoke the app in LinkedIn settings as well.
6. Generate a draft, edit and save it, verify the saved text/image/sources/account, then explicitly approve public publication. An approval is signed, bound to those exact bytes and the account, and expires after 24 hours. Changes require review again.

## Existing Synology/Jenkins installation

Back up `.env` and runtime data privately. Stop **both** old publishing workflows before upgrading. Preserve `published/*.published` and `.publishing` files: the new backend honors them. Do not erase them or allow an old checkout/container to continue publishing.

Set `LINKEDIN_PROJECT_DIR` to your existing absolute project path and rebuild the image. Update **both** Jenkins job definitions to these Jenkinsfiles and set the project path on the agent (or `PROJECT_DIR` parameter). Docker host and agent must see the same path; this matters for bind mounts. All entry points must share the same data directory, approval secret and token encryption key. Set Jenkins home/agent paths in `jenkins/compose.yaml` if migrating an existing controller. Restrict Jenkins job/build/cancel/configure permissions to the owner; Docker socket access is effectively host administrator access. Jenkins publication archives the exact preview/image/research and records the authenticated approver, then uses the same backend as the dashboard. Generation remains scheduled and draft-only.

For short-term migration of an existing personal OAuth token, provide `LINKEDIN_TOKEN`, `LINKEDIN_MEMBER_ID`, **actual** `LINKEDIN_TOKEN_EXPIRES_AT` (Unix seconds), and **actually granted** `LINKEDIN_SCOPES`. Unknown expiry blocks publication. These are operator assertions, not verification of LinkedIn app authorization. Prefer dashboard OAuth, then remove the old plaintext token from `.env`. `LINKEDIN_URN` overrides are removed: this release supports the connected member only, not organization posting.

## CLI review

Generation no longer needs a LinkedIn token and cannot be combined with direct publishing:

```sh
docker compose run --rm linkedin-auto --base-dir /app --auto-topic
```

For an existing draft, inspect its JSON, image and research, obtain a digest with `--draft-json /app/drafts/NAME.json --review-digest`, then record your deliberate approval using `--approve --expected-digest DIGEST --approved-by OWNER`. Only a separate `--publish` call can send that approved draft. Use `--text-only` consistently for review, approval and publication if omitting images. CLI access and signing keys must be restricted to trusted operators: code cannot prove a human actually read the preview.

## Failure recovery

A single atomic per-draft lock protects all entry points. Failed or interrupted requests deliberately retain `.publishing`, even for definite API rejection. A timeout may have succeeded remotely. Check your LinkedIn activity and private server logs **before** any retry. If posted, preserve/create the receipt; if confirmed not posted, remove the lock manually and review/approve again. Never automatically retry creation on 401/403/429/5xx or rotate accounts/tokens to evade limits. API error messages omit response bodies and tokens. Same content under another filename is not deduplicated; do not recreate an already published draft.

## Privacy and release readiness

The built-in notice describes a private owner-operated deployment. Before serving other people, implement separate identities/permissions and accurate operator/contact, deletion and retention policies, review applicable API program terms, and obtain required approvals. Use disk/backup encryption and restrict filesystem access; credentials are encrypted but draft, identity and receipt metadata are not a substitute for full storage encryption. Delete unneeded local data and Jenkins artifacts/backups. Disconnect does not delete already published posts or cancel a request already in flight.

Read [COMPLIANCE.md](COMPLIANCE.md) for the requirements and remaining operator checks. Before public release, complete a full reachable-branch/tag Git-history secret scan (for example Gitleaks), rotate any exposed secrets, inspect generated artifacts/personal data, and choose a license. No open-source license is selected automatically because that is the owner's licensing decision.

## Tests

```sh
python -m pip install -r requirements.txt pytest
python -m pytest -q
```

Tests use mock requests and temporary credentials. They do not publish to LinkedIn. Runtime OAuth/product entitlements, NAS permissions, Docker build, Jenkins execution, reverse-proxy TLS, and dependency vulnerability auditing require deployment validation.
