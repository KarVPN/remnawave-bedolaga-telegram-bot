# CodeQL review for the Bedolaga 5 / Remnawave 3 integration

This integration imports a large upstream update. CodeQL compares it with the older
fork, so its PR result contains upstream findings as well as KarVPN changes. The
analysis and the security-and-quality query suite remain enabled.

## Addressed in this PR

- Subscription fetches validate every redirect destination before making the next
  request. This closes the literal-private-IP redirect gap left by checking only
  the final response URL. DNS connections still use `PublicOnlyResolver`, which
  rejects non-global DNS results. Tests cover loopback, metadata, private IPv4,
  IPv6, credentials, non-HTTP destinations, relative redirects and redirect limits.
- HTML script/style bodies are removed before tag stripping, including closing
  tags with whitespace or attributes. The cabinet frontend must still sanitize
  article HTML; this storage filter is only defense in depth.
- Prometheus escaped label parsing uses disjoint alternatives; malformed long tag
  names and unclosed rich-message blocks no longer cause repeated backtracking.
  Regression tests exercise the reported pathological input shapes.
- GitHub blacklist URL conversion checks the parsed hostname instead of replacing
  a domain substring at an arbitrary position in the URL.
- A malformed Pal24 callback returns a stable public error instead of exception
  details. Database retries reject a non-positive attempt count and never raise
  an optional exception value.
- The referral QR cache key uses SHA-256. This only changes a generated filename.

## Payment-protocol hashing findings

The `py/weak-sensitive-data-hashing` findings in these adapters do **not** describe
storage of user passwords. They point to external payment request signatures or
webhook signatures. Replacing their digest without changing the provider protocol
would break payments. The existing signature checks and constant-time comparisons
are retained.

| CodeQL alert | Location | Contract and disposition |
| --- | --- | --- |
| 9 | `app/external/cryptobot.py` | SHA-256 of the high-entropy API token derives an HMAC-SHA-256 key. This is not password hashing. See [Crypto Pay API](https://help.send.tg/en/articles/10279948-crypto-pay-api), "Verifying webhook updates". |
| 10, 11 | `app/services/freekassa_service.py` | Provider-defined MD5 signatures for payment links and notifications. See [Freekassa SCI](https://docs.freekassa.net/), sections 1.5 and 1.7. |
| 12 | `app/external/heleket.py` | Provider-defined MD5 of base64 request bytes plus the API key. See [Heleket request format](https://doc.heleket.com/general/request-format). |
| 13 | `app/services/kassa_ai_service.py` | Existing KassaAI webhook contract is MD5 of `shop_id:amount:secret2:order_id`; it is separate from its HMAC-SHA-256 request signature. Preserve this compatibility contract. |
| 14 | `app/services/mulenpay_service.py` | Provider-defined SHA-1 request signature. See [MulenPay payment request](https://docs.mulenpay.com/pays/pays_request.html), `sign`. |
| 15 | `app/external/pal24_client.py` | Existing Pal24/Cardlink callback contract uses uppercase MD5 of `OutSum:InvId:token`. See [Cardlink API](https://cardlink.link/en/reference/api), payment postback `SignatureValue`. |
| 16 | `app/webserver/payments.py` | MulenPay webhook signature protocol. The adapter verifies the supplied signature, while unsigned legacy callbacks only trigger an authenticated provider lookup. Regression tests cover both paths. |
| 18 | `tests/webserver/test_mulenpay_webhook.py` | Synthetic webhook-signing fixture with a test secret, not production password storage. |

These are individually reviewed protocol constraints, not a general exemption for
MD5/SHA-1. New uses in local credentials, sessions or authorization still require
review. Any provider migration to a newer signature scheme should be a separately
validated adapter change.

## Reviewed model limitations

The owner explicitly approved these individual false-positive dispositions on
2026-10-15. Their rationale is also recorded on the GitHub alerts.

- **#468, `py/full-ssrf`:** the URL remains user-controlled by design, but every
  destination is validated before its request. Literal non-global IPs, credentials
  and non-HTTP schemes are rejected, redirects are handled explicitly, and the
  actual `aiohttp.TCPConnector` uses `PublicOnlyResolver` to refuse private DNS
  results before connecting. Tests cover both the real connector and redirect
  destinations. The generic data-flow query does not model these custom guards.
- **#78/#79, `py/unsafe-cyclic-import`:** `pricing_utils` imports `RenewalPricing`
  only under `typing.TYPE_CHECKING`, and its annotation is quoted. Runtime imports
  of `PricingEngine` are local to functions. Fresh Python 3.14 processes importing
  the modules in either order both resolved the classes and functions successfully;
  the complete test suite also passed.

These dispositions apply to the reviewed implementation and its regression tests.
Changes to the resolver, redirect checks or runtime import structure need a fresh
review; the CodeQL query suite remains enabled.
