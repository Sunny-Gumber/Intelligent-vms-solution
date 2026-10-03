# Security Policy

Intelligent VMS is currently **Release Candidate / External Qualification Pending**.

## Reporting a vulnerability

Do not place passwords, camera credentials, tokens, private keys, customer information, exploit payloads containing real secrets, or sensitive deployment details in a public issue.

If GitHub shows a private **Report a vulnerability / Security Advisory** path for this repository, use that private channel. Otherwise contact the repository owner privately through GitHub before opening a public issue. Public issues are appropriate only after sensitive details have been removed.

## Secret handling

Committed source must not contain:

- production/test account passwords or API tokens;
- camera credentials or credential-bearing RTSP/HTTP URLs;
- JWT signing secrets or private signing keys;
- database credentials from real deployments;
- PFX/P12/JKS/keystores or private certificate material;
- customer/site identifiers, diagnostics, backups or logs containing sensitive data.

Runtime secrets belong in the deployment-specific protected secret/configuration mechanisms documented by the product. Example/test values must be obviously synthetic.

## Security architecture

Relevant documentation includes:

- `intelligent-vms-v1/docs/security/AUTH_RBAC.md`
- `intelligent-vms-v1/docs/security/LIVE_MONITORING_MEDIA_AUTH.md`
- `intelligent-vms-v1/docs/security/PHASE9_PRODUCTION_SECURITY.md`
- `intelligent-vms-v1/docs/reviews/PUBLIC_REPOSITORY_SECURITY_AUDIT.md`

A green software security gate does not establish real-site penetration resistance or Production Qualified status. External security/PKI/OIDC/site qualification remains required.
