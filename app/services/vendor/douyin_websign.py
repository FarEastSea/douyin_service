"""Douyin ``x-secsdk-web-signature`` implementation.

Derived from Evil0ctal/Douyin_TikTok_Download_API, revision
9fa3e5406694c80ec0a97399f410948f130d0f9e, under Apache-2.0.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable, Sequence
from urllib.parse import quote


SALT = "A96D855A08C0A9707F8BEF0D9A527E4E"
SIGNATURE_PARAM = "x-secsdk-web-signature"
UIFID_PARAM = "uifid"
TIMESTAMP_PARAM = "timestamp"
EXPIRE_HEADER = "x-secsdk-web-expire"


def encode_pairs(pairs: Iterable[tuple[str, str]]) -> str:
    """Serialize exactly like the signer input used by Douyin's Web SDK."""
    return "&".join(
        f"{quote(str(key), safe='*-._')}={quote(str(value), safe='*-._')}"
        for key, value in pairs
    )


def sign(
    pairs: Sequence[tuple[str, str]],
    uifid: str,
    *,
    timestamp: int | None = None,
) -> tuple[str, str, dict[str, str]]:
    """Return the signed query, signature value and matching request headers."""
    normalized_uifid = str(uifid or "").strip()
    if not normalized_uifid:
        raise ValueError("抖音 Web 签名缺少 UIFID")
    stamp = str(int(time.time() if timestamp is None else timestamp))
    covered = list(pairs)
    if not any(name.casefold() == UIFID_PARAM for name, _ in covered):
        covered.append((UIFID_PARAM, normalized_uifid))
    covered.append((TIMESTAMP_PARAM, stamp))
    query = encode_pairs(covered)
    signature = hashlib.md5(
        f"{normalized_uifid}_{stamp}_{SALT}_{query}".encode()
    ).hexdigest()
    headers = {
        UIFID_PARAM: normalized_uifid,
        SIGNATURE_PARAM: signature,
        EXPIRE_HEADER: stamp,
    }
    return f"{query}&{SIGNATURE_PARAM}={signature}", signature, headers
