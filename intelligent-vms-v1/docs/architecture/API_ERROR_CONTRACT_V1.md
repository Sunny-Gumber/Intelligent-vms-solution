# API Error Contract v1

## Purpose

All Intelligent VMS API errors expose a stable machine-readable `error` object
without removing or changing the legacy FastAPI `detail` value. This lets
existing `/api/v1` callers continue to work while new clients migrate to one
consistent contract.

## Response shape

```json
{
  "detail": "Resource not found",
  "error": {
    "schema_version": "v1",
    "code": "HTTP_404",
    "message": "Resource not found"
  }
}
```

Existing structured detail values are preserved. When a detail object already
contains `code` and `message`, those values become the stable error code and
message as well.

Request-validation failures use:

- code: `REQUEST_VALIDATION_ERROR`;
- message: `Request validation failed`;
- legacy `detail`: the original FastAPI validation-error list.

Unexpected server errors use:

- code: `INTERNAL_SERVER_ERROR`;
- message/detail: `Internal server error`;
- no exception text or stack trace in the HTTP response.

The server logs unexpected exceptions with request method and exception class.
Raw request paths and exception messages are intentionally excluded because path
parameters or exception text may contain customer identifiers, credentials, or
other sensitive data.

## Compatibility rule

Do not remove or silently change `detail` inside API v1. New clients should
prefer `error.code` and `error.message`. A future breaking cleanup of legacy
`detail` requires a separately versioned API contract.

## Authentication headers

HTTP exception headers such as `WWW-Authenticate` are preserved by the custom
handler, so the response-envelope change does not weaken authentication flows.
