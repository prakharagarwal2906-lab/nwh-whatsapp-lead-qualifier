from __future__ import annotations

import hmac
import ipaddress


def token_valid(presented: str | None, valid_tokens: list[str]) -> bool:
    """Constant-time comparison against every currently valid token (supports rotation)."""
    if not presented or not valid_tokens:
        return False
    ok = False
    for t in valid_tokens:
        # Evaluate all tokens so timing doesn't reveal which one matched.
        ok |= hmac.compare_digest(presented.encode(), t.encode())
    return ok


def ip_allowed(client_ip: str | None, allowed: list[str]) -> bool:
    """Empty allowlist means the check is disabled (local/dev)."""
    if not allowed:
        return True
    if not client_ip:
        return False
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(ip in ipaddress.ip_network(net, strict=False) for net in allowed)
