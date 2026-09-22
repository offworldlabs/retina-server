"""Radar endpoints the server polls: parsing, address policy and a pinned client.

An operator types the address of a radar they run, and the server fetches from
it. That makes every fetch a request to an address a stranger chose, so nothing
here reaches the network until the host has resolved to publicly routable
addresses only, and the request then goes to the vetted address itself rather
than to a name that could resolve somewhere else a moment later.

Beyond blah2's default port, which parsing needs, nothing radar-specific lives
here; services/blah2_probe.py is the first caller.
"""

import asyncio
import functools
import ipaddress
import logging
import os
import re
import socket
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import unquote, urlsplit

import httpx
import idna

from core.env_parsing import parse_comma_list

logger = logging.getLogger(__name__)

# blah2's API port. Only a bare host gets it: a URL with a scheme but no port
# is a reverse proxy, which listens on the scheme's own default.
BLAH2_DEFAULT_PORT = 3000
_SCHEME_PORTS = {"http": 80, "https": 443}

# Far beyond any real address; refused before any parsing work is spent on it.
_MAX_RAW_LENGTH = 1024

# A last label that getaddrinfo would read as part of an IPv4 address.
_NUMERIC_LABEL = re.compile(r"(?:[0-9]+|0[xX][0-9a-fA-F]*)")

# Comma-separated CIDRs refused on top of everything not publicly routable,
# for addresses that are public but ours.
DENY_CIDRS_ENV = "POLLED_RADAR_DENY_CIDRS"

RESOLVE_TIMEOUT_S = 3.0
# Applies to each connect, read and write on its own.
SOCKET_TIMEOUT_S = 3.0
# One request end to end, including every address tried and the whole body.
REQUEST_DEADLINE_S = 5.0

# Prefixes that only say how an IPv4 packet is carried.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
# True when the address may be connected to.
AddressPolicy = Callable[[IPAddress], bool]
Resolver = Callable[[str, int], Awaitable[Sequence[IPAddress]]]


class Refusal(StrEnum):
    """Why an endpoint was refused, as a stable machine code."""

    INVALID_ENDPOINT = "invalid_endpoint"
    UNSUPPORTED_SCHEME = "unsupported_scheme"
    INVALID_PORT = "invalid_port"
    PATH_NOT_SUPPORTED = "path_not_supported"
    UNRESOLVABLE = "unresolvable"
    ADDRESS_NOT_PUBLIC = "address_not_public"
    # The deny list is malformed, so no address can be judged.
    POLICY_UNAVAILABLE = "policy_unavailable"
    UNREACHABLE = "unreachable"
    TLS_FAILED = "tls_failed"
    TIMED_OUT = "timed_out"
    REDIRECTED = "redirected"
    BAD_RESPONSE = "bad_response"
    TOO_LARGE = "too_large"


class EndpointRefused(Exception):
    """An endpoint turned away, with a code and a message fit for its operator.

    The message may name the operator's own host but never a resolved address,
    a credential, or anything about another operator's endpoint.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PolledEndpoint:
    """One radar endpoint in its stored form.

    ``endpoint_key`` is the identity: every spelling of one box normalises to
    the same key. Scheme and credentials are how to reach it, not what it is.
    """

    scheme: str
    # Lowercase A-label, dotted-quad IPv4, or compressed IPv6 without brackets.
    host: str
    port: int
    endpoint_key: str
    auth_user: str | None = field(repr=False)
    auth_secret: str | None = field(repr=False)
    # Carries the credentials whenever the operator typed them.
    raw: str = field(repr=False)


def _invalid(message: str) -> EndpointRefused:
    return EndpointRefused(Refusal.INVALID_ENDPOINT, message)


def _split_scheme(text: str) -> str:
    """The input as a URL, taking http when no scheme is given."""
    if "://" in text:
        return text
    try:
        # A bare IPv6 literal is otherwise indistinguishable from host:port.
        ipaddress.IPv6Address(text)
    except ValueError:
        return "http://" + text
    return f"http://[{text}]"


def _port(text: str | None, scheme: str, defaulted_scheme: bool) -> int:
    if not text:
        return BLAH2_DEFAULT_PORT if defaulted_scheme else _SCHEME_PORTS[scheme]
    if not (text.isascii() and text.isdigit()) or not 0 < int(text) <= 65535:
        raise EndpointRefused(Refusal.INVALID_PORT, "The port must be a number from 1 to 65535.")
    return int(text)


def _host(text: str, bracketed: bool) -> tuple[str, str]:
    """The stored host and its form in the key."""
    if bracketed:
        if "%" in text:
            raise _invalid("IPv6 zone identifiers are not supported.")
        try:
            address = ipaddress.IPv6Address(text)
        except ValueError:
            raise _invalid("Square brackets must enclose an IPv6 address.") from None
        return address.compressed, f"[{address.compressed}]"
    try:
        # UTS 46 mapping lowercases and folds full-width forms, so it runs
        # before the IPv4 test below sees the result.
        name = idna.encode(text, uts46=True).decode("ascii")
    except idna.IDNAError:
        raise _invalid(f"{text!r} is not a valid hostname.") from None
    name = name.removesuffix(".")
    if _NUMERIC_LABEL.fullmatch(name.rpartition(".")[2]):
        try:
            name = str(ipaddress.IPv4Address(name))
        except ValueError:
            raise _invalid(f"{text!r} is not a valid IPv4 address or hostname.") from None
    return name, name


def parse_endpoint(raw: str) -> PolledEndpoint:
    """Parse what an operator typed: ``host``, ``host:port`` or an http(s) URL.

    Raises EndpointRefused for anything that is not a bare origin.
    """
    text = raw.strip()
    if not text or len(text) > _MAX_RAW_LENGTH:
        raise _invalid("Enter the radar's address as host, host:port or an http(s) URL.")
    if any(c.isspace() or not c.isprintable() for c in text):
        raise _invalid("The address must not contain spaces or control characters.")

    defaulted_scheme = "://" not in text
    try:
        parts = urlsplit(_split_scheme(text))
    except ValueError:
        raise _invalid("The address could not be read as a host or URL.") from None

    if parts.scheme not in _SCHEME_PORTS:
        raise EndpointRefused(Refusal.UNSUPPORTED_SCHEME, "Only http and https addresses are supported.")
    if parts.fragment or "#" in text:
        raise EndpointRefused(Refusal.PATH_NOT_SUPPORTED, "A fragment (#...) is not supported; enter the bare address.")
    if parts.query or "?" in text:
        raise EndpointRefused(Refusal.PATH_NOT_SUPPORTED, "A query string is not supported; enter the bare address.")
    if parts.path not in ("", "/"):
        raise EndpointRefused(
            Refusal.PATH_NOT_SUPPORTED, "A path after the host is not supported; the radar must be served at /."
        )

    userinfo, at, hostport = parts.netloc.rpartition("@")
    bracketed = hostport.startswith("[")
    if bracketed:
        host_text, close, rest = hostport[1:].partition("]")
        if not close or (rest and not rest.startswith(":")):
            raise _invalid("Square brackets must enclose an IPv6 address.")
        port_text = rest[1:] if rest else None
    else:
        host_text, colon, port_text = hostport.partition(":")
        port_text = port_text if colon else None
    if not host_text:
        raise _invalid("The address has no host.")

    port = _port(port_text, parts.scheme, defaulted_scheme)
    host, key_host = _host(host_text, bracketed)

    auth_user = auth_secret = None
    if at and userinfo:
        user, colon, secret = userinfo.partition(":")
        auth_user = unquote(user)
        auth_secret = unquote(secret) if colon else None

    return PolledEndpoint(
        scheme=parts.scheme,
        host=host,
        port=port,
        endpoint_key=f"{key_host}:{port}",
        auth_user=auth_user,
        auth_secret=auth_secret,
        raw=raw,
    )


# ── Address policy ────────────────────────────────────────────────────────────


def _carried_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 destination behind an IPv4-mapped, -compatible or NAT64 address."""
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address in _NAT64 or address in _IPV4_COMPATIBLE:
        return ipaddress.IPv4Address(int(address) & 0xFFFF_FFFF)
    return None


def is_public_address(address: IPAddress, deny: Sequence[IPNetwork] = ()) -> bool:
    """Whether an address is publicly routable and outside ``deny``."""
    if any(address in network for network in deny):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        carried = _carried_ipv4(address)
        if carried is not None:
            return is_public_address(carried, deny)
        if address.sixtofour is not None and not is_public_address(address.sixtofour, deny):
            return False
        # Deprecated, and still classed as global.
        if address.is_site_local:
            return False
    return address.is_global and not (address.is_multicast or address.is_unspecified or address.is_reserved)


def deny_networks_from_env() -> tuple[IPNetwork, ...]:
    """The extra deny list. Raises ValueError on an entry that is not a CIDR."""
    return tuple(ipaddress.ip_network(entry, strict=False) for entry in parse_comma_list(os.getenv(DENY_CIDRS_ENV, "")))


def _default_policy() -> AddressPolicy:
    # Read per call: main.py loads .env after the service imports.
    try:
        deny = deny_networks_from_env()
    except ValueError:
        # Fail closed: a typo must not quietly drop an address from the list.
        logger.error("%s has an entry that is not a CIDR; refusing every polled endpoint", DENY_CIDRS_ENV)
        raise EndpointRefused(
            Refusal.POLICY_UNAVAILABLE, "The server cannot check radar addresses at the moment; try again later."
        ) from None
    return functools.partial(is_public_address, deny=deny)


# ── Resolution ────────────────────────────────────────────────────────────────


async def resolve_host(host: str, port: int) -> list[IPAddress]:
    """Every address the system resolver gives for ``host``."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(info[4][0]) for info in infos]


async def vet_addresses(
    endpoint: PolledEndpoint, *, resolver: Resolver = resolve_host, policy: AddressPolicy | None = None
) -> tuple[IPAddress, ...]:
    """The endpoint's addresses, refused unless every one passes the policy.

    One bad answer refuses the name: a resolver that can return a private
    address among public ones can return it alone next time.
    """
    if policy is None:
        policy = _default_policy()
    try:
        addresses: Sequence[IPAddress] = [ipaddress.ip_address(endpoint.host)]
    except ValueError:
        try:
            async with asyncio.timeout(RESOLVE_TIMEOUT_S):
                addresses = await resolver(endpoint.host, endpoint.port)
        except (OSError, TimeoutError, UnicodeError) as exc:
            logger.debug("polled endpoint %s did not resolve: %r", endpoint.endpoint_key, exc)
            addresses = []
    unique = tuple(dict.fromkeys(addresses))
    if not unique:
        raise EndpointRefused(Refusal.UNRESOLVABLE, f"{endpoint.host} could not be resolved to an address.")
    if not all(policy(address) for address in unique):
        logger.debug("polled endpoint %s resolved to a refused address: %s", endpoint.endpoint_key, unique)
        raise EndpointRefused(
            Refusal.ADDRESS_NOT_PUBLIC,
            f"{endpoint.host} is, or resolves to, an address that is not publicly reachable "
            "(private, loopback, link-local or reserved).",
        )
    return unique


# ── Pinned client ─────────────────────────────────────────────────────────────


def _is_tls_failure(exc: BaseException) -> bool:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ssl.SSLError):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


class PinnedClient:
    """GETs to one endpoint, sent only to the addresses vetted for it.

    The URL carries the address itself, so nothing re-resolves the name between
    the check and the connect. The name travels in the Host header and, over
    TLS, as the SNI name the certificate is verified against.
    """

    def __init__(self, endpoint: PolledEndpoint, addresses: Sequence[IPAddress], http: httpx.AsyncClient) -> None:
        self._endpoint = endpoint
        # Every address vetted for the name, in the resolver's order.
        self.addresses = tuple(addresses)
        self._http = http
        # The address that answered, in canonical text form, once one has.
        self.address: str | None = None
        host = f"[{endpoint.host}]" if ":" in endpoint.host else endpoint.host
        default_port = _SCHEME_PORTS[endpoint.scheme]
        self._headers = {
            "Host": host if endpoint.port == default_port else f"{host}:{endpoint.port}",
            # Counted raw bytes are then the body's real size.
            "Accept-Encoding": "identity",
        }
        self._extensions = {"sni_hostname": endpoint.host} if endpoint.scheme == "https" else {}
        self._auth = (
            httpx.BasicAuth(endpoint.auth_user, endpoint.auth_secret or "") if endpoint.auth_user is not None else None
        )

    async def get(self, path: str, *, max_bytes: int) -> bytes:
        """The body of a 200 answer to ``GET path``, refusing anything else."""
        try:
            async with asyncio.timeout(REQUEST_DEADLINE_S):
                return await self._get(path, max_bytes)
        except TimeoutError:
            raise self._refused(Refusal.TIMED_OUT, "did not answer in time.") from None
        except httpx.RemoteProtocolError:
            raise self._refused(Refusal.BAD_RESPONSE, "did not answer with valid HTTP.") from None
        except httpx.TimeoutException:
            raise self._refused(Refusal.TIMED_OUT, "did not answer in time.") from None
        except httpx.HTTPError as exc:
            logger.debug("polled endpoint %s failed mid-request: %r", self._endpoint.endpoint_key, exc)
            raise self._refused(Refusal.UNREACHABLE, "dropped the connection.") from None

    def _refused(self, code: Refusal, reason: str) -> EndpointRefused:
        return EndpointRefused(code, f"{self._endpoint.host} {reason}")

    async def _get(self, path: str, max_bytes: int) -> bytes:
        candidates = self.addresses if self.address is None else (ipaddress.ip_address(self.address),)
        for address in candidates:
            host = f"[{address}]" if address.version == 6 else str(address)
            request = self._http.build_request(
                "GET",
                f"{self._endpoint.scheme}://{host}:{self._endpoint.port}{path}",
                headers=self._headers,
                extensions=self._extensions,
            )
            try:
                response = await self._http.send(request, auth=self._auth, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if _is_tls_failure(exc):
                    logger.debug("polled endpoint %s failed TLS at %s: %r", self._endpoint.endpoint_key, address, exc)
                    raise self._refused(
                        Refusal.TLS_FAILED, "did not complete a TLS handshake with a certificate valid for that name."
                    ) from None
                logger.debug("polled endpoint %s did not connect at %s: %r", self._endpoint.endpoint_key, address, exc)
                continue
            self.address = str(address)
            try:
                return await self._read(response, path, max_bytes)
            finally:
                await response.aclose()
        raise self._refused(Refusal.UNREACHABLE, "did not accept a connection.")

    async def _read(self, response: httpx.Response, path: str, max_bytes: int) -> bytes:
        if response.is_redirect or 300 <= response.status_code < 400:
            raise self._refused(
                Refusal.REDIRECTED, f"answered with a redirect (HTTP {response.status_code}), which is not followed."
            )
        if response.status_code != 200:
            raise self._refused(Refusal.BAD_RESPONSE, f"answered HTTP {response.status_code} for {path}.")
        if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
            raise self._refused(Refusal.BAD_RESPONSE, "sent a compressed body although none was accepted.")
        too_large = self._refused(Refusal.TOO_LARGE, f"sent more than {max_bytes} bytes for {path}.")
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise too_large
        body = bytearray()
        async for chunk in response.aiter_raw():
            body += chunk
            if len(body) > max_bytes:
                raise too_large
        return bytes(body)


@asynccontextmanager
async def pinned_client(
    endpoint: PolledEndpoint,
    *,
    resolver: Resolver = resolve_host,
    policy: AddressPolicy | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[PinnedClient]:
    """Vet the endpoint's addresses, then yield a client pinned to them.

    Vetting runs on every entry, so a caller that reconnects re-vets.
    """
    addresses = await vet_addresses(endpoint, resolver=resolver, policy=policy)
    async with httpx.AsyncClient(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        # An empty allow-list refuses every cookie.
        cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
        timeout=SOCKET_TIMEOUT_S,
    ) as http:
        yield PinnedClient(endpoint, addresses, http)
