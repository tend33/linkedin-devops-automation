# LinkedIn API safeguards and operator checks

Source-based engineering review, 11 October 2026. These controls reduce policy and security risk; they do not certify compliance or replace LinkedIn's approval of the developer application.

| Requirement | Implementation | Remaining operator obligation |
|---|---|---|
| Official authorized access | OAuth authorization code flow; official member Posts and Images APIs | Confirm developer app product approval and actual scopes; applicable API program terms |
| Member choice and prefilled content | Visible privacy notice, editable drafts, saved-version review, explicit public-post approval | Verify facts, authenticity, copyright/image rights and avoid spam |
| Protect credentials | Password-hash login, CSRF, secure session cookies, encrypted token, expiry rejection, no credential response logging | HTTPS/private access, encryption of disks/backups, strong secrets, controlled Docker/Jenkins access |
| Minimum data and consent | `openid profile w_member_social`, one-time lite profile lookup retaining only the member ID, consent screen; no profile/feed/connection retrieval | Publish accurate privacy/user terms and retention practices before other-user access |
| Withdrawal/deletion | Disconnect deletes token and pending approvals and blocks fallback | Revoke app on LinkedIn; remove local metadata, artifacts and backup copies when required; delete published posts on LinkedIn |
| Prevent unintended/duplicate publication | Signed version/account-bound approval, 24-hour expiry, shared atomic lock and receipt | All publishers must upgrade together and share storage; reconcile uncertain outcomes; no rename-and-repost |
| Respect technical rules/limits | Configurable API version; no automatic creation retries, token/account rotation or browser fallback | Monitor versions and quotas; stop/reconnect on expiry/revocation; review API changes |

LinkedIn's API Terms §§3.1(4), 5.1–5.2 require editability/notice for prefilled content, readily available application privacy/user terms, and informed member consent. §§2.2, 7 address credentials and security; §§1.3–1.4 address documentation, limits and eligibility. A self-serve permission is not blanket authorization to offer a business fundamentally dependent on the APIs: evaluate eligibility or a vetted/partner program before commercializing.

- [LinkedIn API Terms of Use](https://www.linkedin.com/legal/l/api-terms-of-use)
- [LinkedIn User Agreement](https://www.linkedin.com/legal/user-agreement)
- [Marketing API additional terms, if applicable](https://www.linkedin.com/legal/l/marketing-api-terms)
- [Posts API permissions and schema](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api)
- [OAuth authorization-code flow and expiry](https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow)

Public repository distribution can include your source without distributing credentials or member data. Recipients must obtain their own legitimate developer authorization for their independent deployment; do not share your app secret/token or use separate deployments to circumvent quotas. Do not advertise this integration as LinkedIn-certified or endorsed.

This change does not verify developer portal permissions, live OAuth configuration, full Git-history secrets, dependency CVEs, production operation or legal sufficiency for public multi-user/commercial use.

## Source and secret screening performed

The connected repository exposed one branch (`main`). Eight commits reachable from its reviewed head and all 22 unique historical text blobs were screened for common OpenAI/GitHub/AWS token formats, private-key headers and literal API-secret assignments. No matches were found. This is a limited pattern screen, not Gitleaks or a complete credential/entropy audit; tag inventory was not available through the connector endpoint. Secrets in NAS runtime files, ignored files, other refs or external backups were not inspected. A complete release scan remains an operator task.
