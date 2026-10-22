# Architecture Decision Records

Each record states the context, the decision, the alternatives rejected and
the consequences — **including the bad ones**. A decision that lists no
downside has not been thought through.

| ADR | Decision |
| --- | --- |
| [0001](ADR-0001-modular-monolith.md) | A modular control plane, not microservices |
| [0002](ADR-0002-python-fastapi.md) | Python + FastAPI for the control plane |
| [0003](ADR-0003-contract-versioning.md) | A shared contracts package with explicit stability rules |
| [0004](ADR-0004-postgresql.md) | PostgreSQL in production, SQLite for development only |
| [0005](ADR-0005-sync-orm.md) | Synchronous SQLAlchemy executed in FastAPI's threadpool |
| [0006](ADR-0006-media-gateway.md) | Delegate media transport to MediaMTX; isolate FFmpeg as a subprocess |
| [0007](ADR-0007-onvif-client.md) | A focused SOAP client instead of a generated WSDL stack |
| [0008](ADR-0008-credential-encryption.md) | AES-256-GCM credential vault with an out-of-database master key |
| [0009](ADR-0009-client-strategy.md) | Native WPF desktop client plus a React web client |
| [0010](ADR-0010-offline-licensing.md) | Ed25519-signed offline licence files, no phone-home |
| [0011](ADR-0011-identity-providers.md) | Local accounts first, OIDC second, LDAP deferred |
| [0012](ADR-0012-analytics-source-honesty.md) | Analytics provenance is a first-class, non-negotiable field |
| [0013](ADR-0013-recorder-process.md) | Recording runs in a separate OS process |
| [0014](ADR-0014-evidence-integrity.md) | Stream-copy exports with SHA-256 and chain of custody |
| [0015](ADR-0015-media-gateway-authorization.md) | Authenticate media-gateway requests and revoke established viewer sessions |
