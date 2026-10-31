# ADR-0007: A focused SOAP client instead of a generated WSDL stack

**Status:** Accepted · **Date:** 2026-10-19

## Context

ONVIF is SOAP over HTTP with WS-Security. The usual Python approach is `zeep`,
which consumes the ONVIF WSDL/XSD set.

## Decision

Implement a focused SOAP client (`app/devices/onvif_soap.py`) over `httpx`
with explicit XML construction and `ElementTree` parsing, covering the ~15
operations a VMS actually needs.

## Rationale

- An appliance must install and run **fully offline**. Some WSDL stacks fetch
  schema imports over the network at runtime; that is an unacceptable hidden
  dependency for an air-gapped surveillance installation.
- Shipping and version-managing the full ONVIF WSDL/XSD tree is a significant
  redistribution and maintenance burden for operations we do not call.
- The attack surface is smaller and auditable: we control exactly what XML is
  produced and how responses are parsed.

## Consequences

**Good**
- No network dependency for schema resolution; no WSDL tree to redistribute.
- WS-Security UsernameToken with **password digest** is implemented directly,
  so the camera password never crosses the wire in clear text — asserted by
  `test_onvif_security_header_uses_password_digest`.
- Device clock skew is compensated with `GetSystemDateAndTime` before
  authenticating, which is necessary in practice because many cameras ship
  with wrong clocks and reject every digest otherwise.

**Bad / accepted cost**
- Only implemented operations work. Extending coverage means writing XML.
- No schema validation of responses, so unusual vendor payloads must be
  handled defensively in the parser.
- Vendor quirks that a mature WSDL stack absorbs will surface as parsing work
  during real-device qualification. This is expected and budgeted.
