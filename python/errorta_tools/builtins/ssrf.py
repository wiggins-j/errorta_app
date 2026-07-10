"""F039 — SSRF guard for the web_fetch / web_search egress path.

Resolves the target host and rejects loopback / private / link-local (incl. the
cloud metadata endpoint 169.254.169.254) / reserved / multicast addresses, and
enforces an optional per-room domain allowlist. The fetch path re-runs this
check on every redirect hop so a 302 to a private host can't slip through.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class SsrfError(ValueError):
    """A URL was rejected by the SSRF guard. Carries a stable reason code."""


def _host_matches_allowlist(host: str, allowed_domains: list[str]) -> bool:
    h = host.lower().rstrip(".")
    for d in allowed_domains:
        d = str(d).lower().lstrip(".").rstrip(".")
        if not d:
            continue
        if h == d or h.endswith("." + d):
            return True
    return False


def _ip_is_blocked(ip: ipaddress._BaseAddress, *, allow_private: bool = False) -> bool:
    # link-local stays blocked even when private/loopback is allowed — it covers
    # 169.254.169.254 (cloud metadata), which a trusted operator endpoint should
    # never legitimately be.
    if (
        ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    if allow_private:
        return False
    return ip.is_private or ip.is_loopback


def _literal_ip(host: str) -> ipaddress._BaseAddress | None:
    """Canonicalize any IP-literal host — including obfuscated IPv4 forms
    (octal ``0177.0.0.1``, decimal ``2130706433``, hex ``0x7f000001``, short
    ``127.1``). ``ipaddress.ip_address`` rejects those, but C resolvers
    (``inet_aton``, glibc) accept them and would reach loopback. We use
    ``inet_aton`` to detect+canonicalize them so the range check sees the real
    address. Returns None for genuine DNS names."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if ":" in host:  # malformed IPv6 — not a name; reject downstream as DNS fail
        return None
    try:
        packed = socket.inet_aton(host)  # accepts octal/decimal/hex/short IPv4
    except OSError:
        return None
    return ipaddress.ip_address(packed)


def resolve_validated_target(
    url: str,
    *,
    allowed_domains: list[str] | None = None,
    allow_private_host: bool = False,
) -> tuple[str, list[str]]:
    """Validate a single URL/hop and return ``(host, validated_ips)``.

    ``validated_ips`` are the exact addresses every range check passed, as
    strings. The fetch path connects to one of THESE (see F086 pinning in
    web.py) rather than re-resolving the host, which closes the DNS-rebinding
    window: a low-TTL attacker domain cannot answer with a public IP here and a
    private IP at httpx connect time, because httpx never re-resolves — it dials
    the validated IP literal while preserving the hostname for TLS SNI / Host.

    ``allow_private_host`` relaxes the loopback/private-range block for an
    operator-configured TRUSTED endpoint (self-hosted SearXNG on localhost/LAN).
    It does NOT relax link-local/reserved/multicast, so the cloud-metadata
    endpoint stays blocked. Never set it for a member-supplied URL.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise SsrfError("ssrf_bad_scheme")
    host = parts.hostname
    if not host:
        raise SsrfError("ssrf_no_host")
    host = host.rstrip(".")  # canonicalize trailing-dot FQDNs
    if not host:
        raise SsrfError("ssrf_no_host")
    if allowed_domains:
        if not _host_matches_allowlist(host, allowed_domains):
            raise SsrfError("ssrf_domain_not_allowed")
    # A literal IP host (incl. obfuscated IPv4) is checked directly; a genuine
    # name is resolved — every A/AAAA record must be public.
    literal = _literal_ip(host)
    if literal is not None:
        candidates = [literal]
    else:
        try:
            infos = socket.getaddrinfo(host, parts.port or None, proto=socket.IPPROTO_TCP)
        except OSError:
            raise SsrfError("ssrf_dns_failure") from None
        candidates = []
        for info in infos:
            addr = info[4][0]
            try:
                candidates.append(ipaddress.ip_address(addr))
            except ValueError:
                continue
        if not candidates:
            raise SsrfError("ssrf_dns_failure")
    for ip in candidates:
        if _ip_is_blocked(ip, allow_private=allow_private_host):
            raise SsrfError("ssrf_blocked_address")
    return host, [str(ip) for ip in candidates]


def assert_fetch_url_allowed(
    url: str,
    *,
    allowed_domains: list[str] | None = None,
    allow_private_host: bool = False,
) -> str:
    """Validate a single URL/hop. Returns the resolved host on success;
    raises SsrfError (stable reason code) on any violation. Back-compat wrapper
    around :func:`resolve_validated_target`."""
    host, _ips = resolve_validated_target(
        url,
        allowed_domains=allowed_domains,
        allow_private_host=allow_private_host,
    )
    return host


def pin_url_to_ip(url: str, ip: str) -> tuple[str, str]:
    """Rewrite ``url`` so its host is the validated IP literal, returning
    ``(pinned_url, original_host)``. IPv6 is bracketed. The caller sends the
    request with ``Host: original_host`` and the ``sni_hostname`` extension set
    to ``original_host`` so TLS still validates the real certificate."""
    parts = urlsplit(url)
    original_host = (parts.hostname or "").rstrip(".")
    host_for_url = f"[{ip}]" if ":" in ip else ip
    netloc = host_for_url
    if parts.port:
        netloc = f"{host_for_url}:{parts.port}"
    if parts.username:
        cred = parts.username
        if parts.password:
            cred += f":{parts.password}"
        netloc = f"{cred}@{netloc}"
    pinned = parts._replace(netloc=netloc).geturl()
    return pinned, original_host
