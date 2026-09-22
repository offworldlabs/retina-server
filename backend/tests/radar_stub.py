"""A throwaway HTTP(S) server on 127.0.0.1 standing in for a polled radar.

Real sockets rather than a mock transport, so the tests see what the client
actually puts on the wire: the request target, the Host header, and for TLS the
SNI name and whether certificate verification passed.
"""

import asyncio
import datetime
import ipaddress
import ssl
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

# A route answers with a body (status 200) or with (status, body).
Route = Callable[[], Awaitable[bytes | tuple[int, bytes]]]

LOOPBACK = ipaddress.ip_address("127.0.0.1")


def only_loopback(address) -> bool:
    """An address policy that admits the stub and nothing else."""
    return address == LOOPBACK


async def resolve_to_loopback(host: str, port: int):
    return [LOOPBACK]


@dataclass
class SeenRequest:
    method: str
    path: str
    headers: dict[str, str]


class StubServer:
    def __init__(self, routes: dict[str, Route], *, ssl_context: ssl.SSLContext | None = None) -> None:
        self.routes = routes
        self.requests: list[SeenRequest] = []
        self.port = 0
        self._ssl = ssl_context
        self._handlers: set[asyncio.Task] = set()

    @property
    def paths(self) -> list[str]:
        return [r.path for r in self.requests]

    async def __aenter__(self) -> "StubServer":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=self._ssl)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        # A route left hanging would otherwise hold wait_closed open.
        for task in self._handlers:
            task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._handlers.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            request_line, *header_lines = head.decode("latin-1").split("\r\n")
            method, path, _ = request_line.split(" ", 2)
            headers = {}
            for line in header_lines:
                name, sep, value = line.partition(":")
                if sep:
                    headers[name.strip().lower()] = value.strip()
            self.requests.append(SeenRequest(method, path, headers))
            route = self.routes.get(path)
            answer = await route() if route else (404, b"")
            status, body = answer if isinstance(answer, tuple) else (200, answer)
            writer.write(
                f"HTTP/1.1 {status} X\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            self._handlers.discard(task)
            writer.close()


def tls_contexts(hostname: str) -> tuple[ssl.SSLContext, ssl.SSLContext, list[str | None]]:
    """A server context holding a certificate for ``hostname`` alone, a client
    context trusting only its issuer, and the list the server records SNI in.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "radar stub CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    seen_sni: list[str | None] = []
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with tempfile.TemporaryDirectory() as tmp:
        cert_path, key_path = Path(tmp) / "cert.pem", Path(tmp) / "key.pem"
        cert_path.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            leaf_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        server_ctx.load_cert_chain(cert_path, key_path)
    server_ctx.sni_callback = lambda _sock, name, _ctx: seen_sni.append(name)

    client_ctx = ssl.create_default_context(cadata=ca_cert.public_bytes(serialization.Encoding.PEM).decode())
    return server_ctx, client_ctx, seen_sni
