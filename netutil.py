"""SSRF-safe, redirect-free HTTP transport for Questionable Intel (stdlib only).

Both outbound paths — the settings test probe (``GET {base}/models``) and
question generation (``POST {base}/chat/completions``) — go through
:func:`open_no_redirect`, so the SSRF block, the no-redirect rule and the
size/time bounds cannot drift apart between the two paths.

The core invariant is *resolve-then-connect*: the host is resolved once,
every resolved address is checked against a blocklist, and the connection is
dialled to that pinned resolved IP (never re-resolved). The ``Host`` header and
TLS SNI keep the original hostname so virtual hosting and certificate
verification still work.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
import urllib.parse

# Blocklist. ``ipaddress`` categories (loopback / link_local /
# unspecified / multicast) cover most of these; the explicit networks close the
# gaps the categories do not name (``169.254.0.0/16`` is link-local, but the
# cloud metadata address inside it is the point; ``fd00:ec2::254`` is a ULA, not
# link-local; ``0.0.0.0/8`` is wider than the single unspecified address).
_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),   # IPv4 link-local / cloud metadata
    ipaddress.ip_network("fe80::/10"),        # IPv6 link-local
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IPv6 metadata
    ipaddress.ip_network("0.0.0.0/8"),        # unspecified + this-network
    ipaddress.ip_network("127.0.0.0/8"),      # IPv4 loopback
    ipaddress.ip_network("::1/128"),          # IPv6 loopback
    ipaddress.ip_network("::/128"),           # IPv6 unspecified
    ipaddress.ip_network("224.0.0.0/4"),      # IPv4 multicast
    ipaddress.ip_network("ff00::/8"),         # IPv6 multicast
)

# RFC1918 stays allowed: these are the whole point of the app (the
# on-LAN Ollama endpoint). Used by settings.validate for the cleartext rule.
_RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


class NetError(Exception):
    """Raised for any transport failure. The message carries the *reason* and
    (when known) the HTTP *status* only — never userinfo, never the
    ``Authorization`` header, never header bytes, never the URL or host."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        self.reason = reason
        self.status = status
        if status is not None:
            super().__init__(f"{reason} (status {status})")
        else:
            super().__init__(reason)


def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (ip.is_loopback or ip.is_link_local or ip.is_unspecified
            or ip.is_multicast):
        return True
    return any(ip in net for net in _BLOCKED_NETWORKS)


def rfc1918(ip: str) -> bool:
    """True when *ip* is an RFC1918 private address (10/8, 172.16/12, 192.168/16)."""
    addr = ipaddress.ip_address(ip)
    return any(addr in net for net in _RFC1918_NETWORKS)


def resolve_host(host: str) -> list[str]:
    """Resolve *host* and return the pinned IP address strings to dial.

    Raises :class:`NetError` if the host literal is a blocked category, if any
    resolved address is blocked, or if resolution fails. The returned addresses
    are what :func:`open_no_redirect` dials — validation and connection use the
    same address, so there is no rebinding window.
    """
    # 1. Host literal (an IP, v4 or v6) — check without touching DNS.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _blocked(literal):
            raise NetError("blocked target")
        return [host]

    # 2. Resolve and check every address. Any blocked address poisons the whole
    #    host (a hostname may round-robin a mix; one loopback A record wins).
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise NetError("cannot resolve host")
    addrs: list[str] = []
    for info in infos:
        ipstr = str(info[4][0])
        try:
            ip = ipaddress.ip_address(ipstr)
        except ValueError:
            continue
        if _blocked(ip):
            raise NetError("blocked target")
        if ipstr not in addrs:
            addrs.append(ipstr)
    if not addrs:
        raise NetError("cannot resolve host")
    return addrs


def open_no_redirect(url: str, *, data: bytes | None = None,
                     headers: dict | None = None, timeout: float = 5.0,
                     max_bytes: int = 1 << 20) -> tuple[int, bytes]:
    """Open *url* and return ``(status, body)``, without ever following a redirect.

    * Resolves the host, rejects blocked targets, and dials the pinned
      resolved IP. ``Host`` = original hostname (with port when non-default);
      for ``https`` the TLS handshake uses ``server_hostname`` = original
      hostname so SNI and certificate verification still pass.
    * Any 3xx raises :class:`NetError` (never followed).
    * Wall-clock deadline over connect+read via ``time.monotonic()``; reads at
      most ``max_bytes`` and raises on oversize.
    """
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise NetError("unsupported scheme")
    host = parts.hostname
    if not host:
        raise NetError("missing host")
    if parts.username is not None or parts.password is not None:
        raise NetError("userinfo not allowed")
    try:
        port = parts.port
    except ValueError:
        raise NetError("invalid port")
    if port is None:
        port = 443 if scheme == "https" else 80
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    # Resolve + block-check; dial the first pinned address. The address
    # we validated is the address we dial — no re-resolution, no rebinding.
    addrs = resolve_host(host)
    addr = addrs[0]

    deadline = time.monotonic() + timeout

    hdrs = dict(headers or {})
    # Host header keeps the original hostname (bracket IPv6 literals; append the
    # port when it is non-default) so reverse proxies / virtual hosts see the
    # name, not the dialled IP.
    host_hdr = host
    if ":" in host and not host.startswith("["):
        host_hdr = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    if port != default_port:
        host_hdr = f"{host_hdr}:{port}"
    hdrs["Host"] = host_hdr

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise NetError("deadline exceeded")

    conn = None
    resp = None
    try:
        conn = http.client.HTTPConnection(addr, port, timeout=remaining)
        if scheme == "https":
            # Dial the pinned IP, then wrap the already-connected socket with
            # SNI = original hostname, so cert checks use the real name.
            conn.connect()
            ctx = ssl.create_default_context()
            conn.sock = ctx.wrap_socket(conn.sock, server_hostname=host)

        method = "POST" if data is not None else "GET"
        conn.request(method, path, body=data, headers=hdrs)
        # Capture the socket before getresponse(): on Python 3.14 getresponse()
        # sets conn.sock to None, but the deadline loop needs the live socket.
        sock = conn.sock
        resp = conn.getresponse()
        status = resp.status

        # A 3xx is an error, never followed.
        if 300 <= status < 400:
            raise NetError("redirect not followed", status=status)

        # Read at most max_bytes, wall-clock deadline over the read.
        # read1() performs a single recv (returns promptly even on a drip-feed),
        # so the monotonic deadline below is checked every byte-chunk rather
        # than only after a whole buffer fills.
        body = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NetError("deadline exceeded")
            # read1 may close the socket once the body is fully consumed (e.g.
            # HTTP/1.0 connection-close). Detect that before touching the socket.
            if resp.isclosed():
                break
            sock.settimeout(remaining)
            try:
                chunk = resp.read1(min(65536, max_bytes + 1 - len(body)))
            except (socket.timeout, TimeoutError, OSError,
                    http.client.HTTPException):
                if time.monotonic() >= deadline:
                    raise NetError("deadline exceeded")
                raise NetError("socket error")
            if not chunk:
                break
            body += chunk
            if len(body) > max_bytes:
                raise NetError("response too large")
        return status, body
    except NetError:
        raise
    except (socket.timeout, TimeoutError):
        if time.monotonic() >= deadline:
            raise NetError("deadline exceeded")
        raise NetError("connection timed out")
    except ssl.SSLError:
        raise NetError("tls error")
    except (OSError, http.client.HTTPException):
        raise NetError("connection failed")
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
