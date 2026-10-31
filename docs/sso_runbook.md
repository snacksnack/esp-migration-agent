# SSO runbook: putting the review gate behind your identity provider

Audience: the customer's IT or identity admin. Owner on our side: the FDE
running the migration. Time: about 20 minutes. Decision record: ADR-0021.

The migration spec is signed off in the review gate. After this setup, only
people your IdP assigns to the app can sign off, every approval and audit
record carries the identity your IdP vouched for, and deactivating someone
in your IdP ends their access at their next action. Okta is shown below;
any OpenID Connect provider (Entra ID, Google Workspace, Keycloak) works the
same way.

## What we need from you

| Item | Example | Where it goes |
| --- | --- | --- |
| Issuer URL | `https://integrator-1234567.okta.com` | `ESP_OIDC_ISSUER` |
| Client ID | `0oa1b2c3d4...` | `ESP_OIDC_CLIENT_ID` |
| Client secret | (send over a secret-sharing link, never email) | `ESP_OIDC_CLIENT_SECRET` |
| Approver group | `ESP Migration Approvers` | stays in your IdP |

## What you register

| Setting | Value |
| --- | --- |
| Sign-in redirect URI | `https://esp.hihelloreid.com/api/auth/sso/callback` |
| Initiate login URI (app tile) | `https://esp.hihelloreid.com/api/auth/sso/start` |
| Scopes requested | `openid email profile offline_access` |
| Grant types | Authorization Code, Refresh Token |

The refresh token is how the gate re-checks your IdP before each change. It
stays on the server and never reaches the browser. Without it, sign-in is
refused on purpose.

## Okta steps

1. **Org.** In a trial or sandbox org, use the Okta Integrator free plan
   (developer.okta.com). The org URL, with no path, is the issuer: the gate
   uses Okta's org authorization server.
2. **App integration.** Admin Console → Applications → Create App
   Integration → OIDC - OpenID Connect → Web Application.
   - Name: `ESP Migration Review Gate`.
   - Grant type: check **Refresh Token** (Authorization Code is already on).
   - Sign-in redirect URIs: the callback above. For a local dry run, add
     `http://localhost:8542/api/auth/sso/callback` too.
   - Controlled access: **Limit access to selected groups** → the approver
     group. People outside the group are refused by Okta and never reach the
     gate.
3. **Refresh token behavior.** On the app's General tab → Refresh Token:
   **Rotate token after every use**, grace period 30 seconds (the default).
4. **App tile (optional).** Still on the General tab, under Login: set
   *Login initiated by* to **Either Okta or App**, check *Display application
   icon to users*, and set *Initiate login URI* to the start URL above.
   Approvers can then open the gate from their Okta dashboard.
5. **Approvers.** Directory → Groups → create `ESP Migration Approvers` and
   add the people who sign off on the migration (normally one data owner and
   one backup). For a demo persona, use Directory → People → Add person,
   choose "I will set password", and uncheck "User must change password".
6. **Hand over.** Send the issuer URL, client ID, and client secret as listed
   above.

## What we do with it

```bash
fly secrets set ESP_OIDC_ISSUER=https://integrator-1234567.okta.com \
  ESP_OIDC_CLIENT_ID=... ESP_OIDC_CLIENT_SECRET=... -a esp-migration-demo
# Optional: SSO-only. Remove the shared demo password.
fly secrets unset ESP_REVIEW_PASSWORD -a esp-migration-demo
```

`ESP_PUBLIC_URL` is already set in `fly.toml`. The redirect URI is derived
from it, so it must match step 2 exactly. Setting secrets restarts the
machine, and the next page load shows **Sign in with Okta**.

## Acceptance walkthrough (do this together)

1. Open the gate anonymously. The spec is readable, and any change asks for
   sign-in.
2. **Sign in with Okta** as an approver (or click the tile). The header
   reads "Signed in as Dana Ops, verified by https://...okta.com".
3. Resolve the open mappings and approve. The receipt and the approved
   spec's `approval.identity` carry the Okta issuer and user id (`sub`). The
   audit log shows each resolution and the approval under that identity.
4. Run the migration and reconciliation as usual. The cutover plan and the
   reconciliation report's **Approval** section name the verified approver.
5. **Offboard test.** In Okta, Directory → People → the approver → More
   actions → **Deactivate** (or Suspend). Back in the gate, the next change
   answers "Okta no longer vouches for your sign-in", the header returns to
   anonymous, and the audit log records `access revoked by the IdP`. Signing
   in again is refused by Okta. Reactivate the user afterward.

## Operating notes

- **How quickly access ends.** At the approver's next change, at most
  `ESP_OIDC_RECHECK_SECONDS` (30s) after the last check. Reading the spec is
  public on the demo, so revocation governs changes, not viewing. Instant
  push revocation (SCIM) is a planned follow-up.
- **If the IdP is down,** changes are refused with a 503 and nothing is
  written. Reading still works.
- **Sessions** last 8 hours (`ESP_OIDC_SESSION_HOURS`).
- **Rotating the client secret:** create a second secret in Okta, update it
  with `fly secrets set`, then deactivate the old one.
- **The audit log** lives in Postgres on the hosted demo (ADR-0022): the
  database refuses UPDATE, DELETE and TRUNCATE, every record is
  hash-chained, and it survives restarts (the spec does not; each start
  logs a `service_start` record saying so). Check it with
  `uv run --group ui python -m review_ui.db_setup verify`: chain intact and
  all three rewrites refused, as the app role. Each record is also in the
  Fly log stream. Without a database it falls back to an append-only file.
- **SSO sessions** survive deploys and restarts (refresh tokens encrypted at
  rest), so "a restart ends every SSO session" applies only to deploys
  without a database.
