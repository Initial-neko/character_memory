# Public HTTP transport TLS fixture

`test-only.crt` is a self-signed certificate for `public.example`, paired with
`test-only.key`. These are public, test-only assets and must never be used by a
service. Tests run local TLS servers and trust only this fixture through a
temporary `SSL_CERT_FILE`. Runtime TLS verification remains enabled.

Generated with OpenSSL for deterministic offline tests; CI does not invoke
OpenSSL or download a certificate/model. Regenerate the pair before expiry.
