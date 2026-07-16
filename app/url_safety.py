"""Network-target validation shared by every scrape fetch tier."""
import asyncio
import ipaddress
import os
import socket
from typing import Optional, Tuple
from urllib.parse import urlsplit


_NAT64_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


class UnsafeUrlError(ValueError):
    """A URL would leave the public internet or bypass target policy."""


def _allow_private_networks() -> bool:
    return os.environ.get("ALLOW_PRIVATE_NETWORKS", "").lower() in (
        "1", "true", "yes"
    )


async def _resolve(host: str, port: int):
    return await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )


def _is_public_address(address: ipaddress._BaseAddress) -> bool:
    """Use a fail-closed definition of public, including IPv6 transition forms."""
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        or getattr(address, "is_site_local", False)
    ):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        # Avoid alternate encodings that can disagree across URL parsers,
        # resolvers, proxies, and the eventual network stack.
        if address.ipv4_mapped or address.sixtofour or address.teredo:
            return False
        if any(address in network for network in _NAT64_NETWORKS):
            return False
    return True


async def ensure_public_url(url: str) -> Optional[Tuple[str, ...]]:
    """Reject unsafe targets and return their validated public IP addresses."""
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeUrlError("URL has an invalid port") from exc

    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError("URL must use http:// or https://")
    if not parsed.hostname:
        raise UnsafeUrlError("URL must include a hostname")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("URL credentials are not supported")

    if _allow_private_networks():
        return None

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeUrlError("Refusing to access a local network address")

    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            records = await _resolve(host.encode("idna").decode("ascii"), port)
        except (OSError, UnicodeError) as exc:
            raise UnsafeUrlError(f"Cannot resolve target host {host!r}") from exc
        addresses = list({ipaddress.ip_address(record[4][0]) for record in records})

    if not addresses:
        raise UnsafeUrlError(f"Cannot resolve target host {host!r}")
    if any(not _is_public_address(address) for address in addresses):
        raise UnsafeUrlError("Refusing to access a non-public network address")
    return tuple(sorted(str(address) for address in addresses))
