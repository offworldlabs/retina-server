"""Operator-typed radar endpoints: parsing, address policy and the pinned client."""

import asyncio
import base64
import socket
from ipaddress import ip_address, ip_network

import httpx
import pytest

from services import polled_endpoint
from services.polled_endpoint import (
    DENY_CIDRS_ENV,
    EndpointRefused,
    Refusal,
    deny_networks_from_env,
    is_public_address,
    parse_endpoint,
    pinned_client,
    vet_addresses,
)
from tests.radar_stub import StubServer, only_loopback, resolve_to_loopback, tls_contexts

# ── Parsing and normalisation ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "spellings, key",
    [
        (
            [
                "radar.example.com",
                "RADAR.Example.COM",
                "radar.example.com.",
                "radar.example.com:3000",
                "radar.example.com:03000",
                "  radar.example.com  ",
                "http://radar.example.com:3000",
                "http://radar.example.com:3000/",
                "HTTP://Radar.Example.Com.:3000",
                "https://radar.example.com:3000",
            ],
            "radar.example.com:3000",
        ),
        (
            ["http://radar.example.com", "http://radar.example.com:80/", "radar.example.com:80"],
            "radar.example.com:80",
        ),
        (["https://radar.example.com", "https://radar.example.com:443/"], "radar.example.com:443"),
        (
            ["bücher.example", "BÜCHER.example", "xn--bcher-kva.example", "XN--BCHER-KVA.EXAMPLE.:3000"],
            "xn--bcher-kva.example:3000",
        ),
        (
            [
                "2001:db8::1",
                "[2001:db8::1]",
                "[2001:DB8:0:0:0:0:0:1]:3000",
                "http://[2001:0db8::0001]:3000/",
            ],
            "[2001:db8::1]:3000",
        ),
        (["192.0.2.10", "192.0.2.10:3000", "192.0.2.10.", "http://192.0.2.10:3000"], "192.0.2.10:3000"),
        (
            ["http://user:secret@radar.example.com:3000", "radar.example.com", "other:pw@radar.example.com"],
            "radar.example.com:3000",
        ),
    ],
)
def test_spelling_variants_of_one_box_share_a_key(spellings, key):
    assert {parse_endpoint(s).endpoint_key for s in spellings} == {key}


def test_bare_host_takes_blah2s_default_port_over_http():
    endpoint = parse_endpoint("radar.example.com")
    assert (endpoint.scheme, endpoint.host, endpoint.port) == ("http", "radar.example.com", 3000)


def test_a_scheme_without_a_port_takes_the_schemes_default():
    assert parse_endpoint("https://radar.example.com").port == 443
    assert parse_endpoint("http://radar.example.com").port == 80


def test_explicit_port_wins():
    assert parse_endpoint("https://radar.example.com:8443").port == 8443


def test_ipv6_host_is_compressed_and_unbracketed_outside_the_key():
    endpoint = parse_endpoint("[2001:DB8::0:1]:3000")
    assert endpoint.host == "2001:db8::1"
    assert endpoint.endpoint_key == "[2001:db8::1]:3000"


def test_unicode_host_is_stored_in_its_ascii_form():
    assert parse_endpoint("bücher.example").host == "xn--bcher-kva.example"


def test_credentials_are_separated_from_the_key():
    endpoint = parse_endpoint("https://op%40home:p%3Ass@radar.example.com")
    assert endpoint.auth_user == "op@home"
    assert endpoint.auth_secret == "p:ss"
    assert "p:ss" not in endpoint.endpoint_key
    assert "op@home" not in endpoint.endpoint_key


def test_no_credentials_means_none():
    endpoint = parse_endpoint("radar.example.com")
    assert endpoint.auth_user is None
    assert endpoint.auth_secret is None


def test_a_user_without_a_secret_keeps_the_secret_none():
    endpoint = parse_endpoint("http://op@radar.example.com")
    assert (endpoint.auth_user, endpoint.auth_secret) == ("op", None)


def test_credentials_never_appear_in_the_repr():
    endpoint = parse_endpoint("http://opname:hunter2@radar.example.com")
    assert "hunter2" not in repr(endpoint)
    assert "opname" not in repr(endpoint)


def test_raw_input_is_kept():
    assert parse_endpoint(" Radar.Example.com ").raw == " Radar.Example.com "


def test_endpoint_is_immutable():
    endpoint = parse_endpoint("radar.example.com")
    with pytest.raises(AttributeError):
        endpoint.port = 1  # type: ignore[misc]


def _refusal(raw: str) -> EndpointRefused:
    with pytest.raises(EndpointRefused) as info:
        parse_endpoint(raw)
    return info.value


@pytest.mark.parametrize("raw", ["", "   ", "x" * 2000 + ".example.com"])
def test_empty_or_oversized_input_is_refused(raw):
    assert _refusal(raw).code == Refusal.INVALID_ENDPOINT


@pytest.mark.parametrize("raw", ["ftp://radar.example.com", "file:///etc/passwd", "gopher://radar.example.com:70"])
def test_schemes_other_than_http_and_https_are_refused(raw):
    assert _refusal(raw).code == Refusal.UNSUPPORTED_SCHEME


@pytest.mark.parametrize(
    "raw",
    [
        "radar.example.com:0",
        "radar.example.com:65536",
        "radar.example.com:http",
        "radar.example.com:-1",
        "radar.example.com:+80",
        "http://radar.example.com:٣٠٠٠",
    ],
)
def test_bad_ports_are_refused(raw):
    assert _refusal(raw).code == Refusal.INVALID_PORT


@pytest.mark.parametrize("raw", ["radar.example.com/api", "http://radar.example.com/blah2/", "radar.example.com//"])
def test_a_path_prefix_is_refused(raw):
    refusal = _refusal(raw)
    assert refusal.code == Refusal.PATH_NOT_SUPPORTED
    assert "path" in refusal.message


@pytest.mark.parametrize("raw", ["http://radar.example.com/?x=1", "radar.example.com?x", "http://radar.example.com/?"])
def test_a_query_is_refused(raw):
    refusal = _refusal(raw)
    assert refusal.code == Refusal.PATH_NOT_SUPPORTED
    assert "query" in refusal.message


@pytest.mark.parametrize("raw", ["http://radar.example.com/#top", "radar.example.com#"])
def test_a_fragment_is_refused(raw):
    refusal = _refusal(raw)
    assert refusal.code == Refusal.PATH_NOT_SUPPORTED
    assert "fragment" in refusal.message


@pytest.mark.parametrize(
    "raw",
    [
        "my_host.example.com",
        "-bad.example.com",
        "a..b.example.com",
        "radar.example.com..",
        "ex%61mple.com",
        "radar example.com",
        "radar.exa\tmple.com",
        "radar.example.com\x00",
        "http://:3000",
        "http://user:pw@",
        "[radar.example.com]",
        "[2001:db8::1",
        "[2001:db8::1]x",
        "[fe80::1%25eth0]",
        "fe80::1%eth0",
    ],
)
def test_malformed_hosts_are_refused(raw):
    assert _refusal(raw).code == Refusal.INVALID_ENDPOINT


@pytest.mark.parametrize("raw", ["2130706433", "127.1", "0x7f.0.0.1", "192.0.2", "01.02.03.04", "radar.0x10"])
def test_a_host_ending_in_a_number_must_be_a_dotted_quad(raw):
    # getaddrinfo reads these as IPv4, so they would be one box under two keys.
    assert _refusal(raw).code == Refusal.INVALID_ENDPOINT


def test_fullwidth_digits_normalise_to_the_ipv4_key():
    assert parse_endpoint("１９２.０.２.１０").endpoint_key == "192.0.2.10:3000"


def test_refusal_messages_never_echo_the_secret():
    for raw in ("http://op:hunter2@radar.example.com/x", "ftp://op:hunter2@radar.example.com", "http://op:hunter2@:0"):
        refusal = _refusal(raw)
        assert "hunter2" not in refusal.message
        assert "hunter2" not in str(refusal)


# ── Address policy ────────────────────────────────────────────────────────────

# Well-known public resolvers: globally routable, and nobody's home.
PUBLIC = ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8", "64:ff9b::808:808", "::808:808"]


@pytest.mark.parametrize("address", PUBLIC)
def test_public_addresses_are_permitted(address):
    assert is_public_address(ip_address(address))


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "100.127.255.254",
        "0.0.0.0",
        "224.0.0.1",
        "239.255.255.250",
        "240.0.0.1",
        "255.255.255.255",
        "192.0.2.1",
        "198.18.0.1",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fd00:ec2::254",
        "fec0::1",
        "ff02::1",
        "ff0e::1",
        "2001:db8::1",
        "2001::1",
        "64:ff9b:1::808:808",
    ],
)
def test_non_public_addresses_are_refused(address):
    assert not is_public_address(ip_address(address))


@pytest.mark.parametrize(
    "address",
    [
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        "::ffff:100.64.0.1",
        "::7f00:1",
        "::a9fe:a9fe",
        "2002:7f00:1::",
        "2002:a9fe:a9fe::1",
        "64:ff9b::7f00:1",
        "64:ff9b::a9fe:a9fe",
        "64:ff9b::a00:1",
    ],
)
def test_ipv4_embedded_in_ipv6_is_judged_as_ipv4(address):
    assert not is_public_address(ip_address(address))


def test_the_deny_list_refuses_otherwise_public_addresses():
    deny = (ip_network("8.8.8.0/24"), ip_network("2606:4700::/32"))
    assert not is_public_address(ip_address("8.8.8.8"), deny)
    assert not is_public_address(ip_address("::ffff:8.8.8.8"), deny)
    assert not is_public_address(ip_address("64:ff9b::808:808"), deny)
    assert not is_public_address(ip_address("2606:4700:4700::1111"), deny)
    assert is_public_address(ip_address("1.1.1.1"), deny)


def test_deny_list_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv(DENY_CIDRS_ENV, " 8.8.8.0/24 , 2606:4700::/32,,")
    assert deny_networks_from_env() == (ip_network("8.8.8.0/24"), ip_network("2606:4700::/32"))


def test_deny_list_accepts_host_bits(monkeypatch):
    monkeypatch.setenv(DENY_CIDRS_ENV, "8.8.8.8/24,1.1.1.1")
    assert deny_networks_from_env() == (ip_network("8.8.8.0/24"), ip_network("1.1.1.1/32"))


def test_deny_list_is_empty_by_default(monkeypatch):
    monkeypatch.delenv(DENY_CIDRS_ENV, raising=False)
    assert deny_networks_from_env() == ()


def test_a_malformed_deny_list_raises(monkeypatch):
    monkeypatch.setenv(DENY_CIDRS_ENV, "8.8.8.0/24,not-a-network")
    with pytest.raises(ValueError):
        deny_networks_from_env()


# ── Resolution and vetting ────────────────────────────────────────────────────


def _resolver(*addresses: str):
    async def resolve(host: str, port: int):
        return [ip_address(a) for a in addresses]

    return resolve


async def _never_called(host: str, port: int):
    raise AssertionError("an IP literal must not be resolved")


async def _vet_refusal(raw: str, **kwargs) -> EndpointRefused:
    with pytest.raises(EndpointRefused) as info:
        await vet_addresses(parse_endpoint(raw), **kwargs)
    return info.value


async def test_an_ip_literal_skips_dns():
    assert await vet_addresses(parse_endpoint("8.8.8.8:3000"), resolver=_never_called) == (ip_address("8.8.8.8"),)


async def test_a_public_name_is_vetted():
    vetted = await vet_addresses(parse_endpoint("radar.example.com"), resolver=_resolver("8.8.8.8", "1.1.1.1"))
    assert vetted == (ip_address("8.8.8.8"), ip_address("1.1.1.1"))


async def test_duplicate_answers_are_collapsed():
    vetted = await vet_addresses(parse_endpoint("radar.example.com"), resolver=_resolver("8.8.8.8", "8.8.8.8"))
    assert vetted == (ip_address("8.8.8.8"),)


async def test_any_non_public_answer_refuses_the_whole_name():
    refusal = await _vet_refusal("radar.example.com", resolver=_resolver("8.8.8.8", "10.0.0.1"))
    assert refusal.code == Refusal.ADDRESS_NOT_PUBLIC
    assert "radar.example.com" in refusal.message
    assert "10.0.0.1" not in refusal.message


async def test_a_private_literal_is_refused():
    assert (await _vet_refusal("192.168.1.1")).code == Refusal.ADDRESS_NOT_PUBLIC


async def test_metadata_addresses_are_refused():
    assert (await _vet_refusal("169.254.169.254:80")).code == Refusal.ADDRESS_NOT_PUBLIC
    assert (await _vet_refusal("[fd00:ec2::254]:80")).code == Refusal.ADDRESS_NOT_PUBLIC


async def test_the_system_resolver_path_refuses_localhost():
    assert (await _vet_refusal("localhost:3000")).code == Refusal.ADDRESS_NOT_PUBLIC


async def test_the_default_policy_reads_the_deny_list_per_call(monkeypatch):
    monkeypatch.setenv(DENY_CIDRS_ENV, "8.8.8.0/24")
    assert (await _vet_refusal("8.8.8.8")).code == Refusal.ADDRESS_NOT_PUBLIC


async def test_a_malformed_deny_list_refuses_everything(monkeypatch):
    monkeypatch.setenv(DENY_CIDRS_ENV, "garbage")
    assert (await _vet_refusal("8.8.8.8")).code == Refusal.POLICY_UNAVAILABLE


async def test_an_injected_policy_replaces_the_default():
    vetted = await vet_addresses(parse_endpoint("127.0.0.1:3000"), policy=only_loopback)
    assert vetted == (ip_address("127.0.0.1"),)


async def test_an_injected_policy_still_refuses_what_it_refuses():
    refusal = await _vet_refusal("radar.example.com", resolver=_resolver("8.8.8.8"), policy=only_loopback)
    assert refusal.code == Refusal.ADDRESS_NOT_PUBLIC


async def test_a_resolver_failure_is_unresolvable():
    async def fail(host, port):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    assert (await _vet_refusal("radar.example.com", resolver=fail)).code == Refusal.UNRESOLVABLE


async def test_an_empty_answer_is_unresolvable():
    assert (await _vet_refusal("radar.example.com", resolver=_resolver())).code == Refusal.UNRESOLVABLE


async def test_a_slow_resolver_is_cut_off(monkeypatch):
    monkeypatch.setattr(polled_endpoint, "RESOLVE_TIMEOUT_S", 0.05)

    async def hang(host, port):
        await asyncio.sleep(10)

    assert (await _vet_refusal("radar.example.com", resolver=hang)).code == Refusal.UNRESOLVABLE


# ── Pinned client ─────────────────────────────────────────────────────────────


def _permit_all(address) -> bool:
    return True


def _response(status: int = 200, body: bytes = b"{}", headers: dict[str, str] | None = None) -> httpx.Response:
    # Streamed, as a real transport answers; content= would arrive already read.
    headers = {"content-length": str(len(body)), **(headers or {})}
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


class Recorder:
    """A MockTransport handler that records requests and plays back responses."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


async def _get(raw: str, handler, *, addresses=("192.0.2.10",), path="/api/config", max_bytes=1024, times=1):
    async with pinned_client(
        parse_endpoint(raw),
        resolver=_resolver(*addresses),
        policy=_permit_all,
        transport=httpx.MockTransport(handler),
    ) as client:
        bodies = [await client.get(path, max_bytes=max_bytes) for _ in range(times)]
        return client, bodies


async def _get_refusal(raw: str, handler, **kwargs) -> EndpointRefused:
    with pytest.raises(EndpointRefused) as info:
        await _get(raw, handler, **kwargs)
    return info.value


async def test_the_request_goes_to_the_vetted_address_with_the_name_in_host():
    recorder = Recorder(_response())
    client, bodies = await _get("radar.example.com", recorder)
    request = recorder.requests[0]
    assert bodies == [b"{}"]
    assert request.method == "GET"
    assert (request.url.scheme, request.url.host, request.url.port) == ("http", "192.0.2.10", 3000)
    assert request.url.path == "/api/config"
    assert request.headers["host"] == "radar.example.com:3000"
    assert client.address == "192.0.2.10"


async def test_the_default_port_is_left_out_of_host():
    recorder = Recorder(_response())
    await _get("http://radar.example.com", recorder)
    assert recorder.requests[0].url.port in (None, 80)
    assert recorder.requests[0].headers["host"] == "radar.example.com"


async def test_https_verifies_against_the_name_through_sni():
    recorder = Recorder(_response())
    await _get("https://radar.example.com", recorder)
    request = recorder.requests[0]
    assert (request.url.scheme, request.url.host) == ("https", "192.0.2.10")
    assert request.extensions["sni_hostname"] == "radar.example.com"
    assert request.headers["host"] == "radar.example.com"


async def test_an_ipv6_address_is_bracketed_in_the_url():
    recorder = Recorder(_response())
    client, _ = await _get("radar.example.com", recorder, addresses=("2001:db8::10",))
    assert recorder.requests[0].url.host == "2001:db8::10"
    assert client.address == "2001:db8::10"


async def test_an_ipv6_literal_endpoint_brackets_the_host_header():
    recorder = Recorder(_response())
    await _get("[2001:db8::10]:3000", recorder)
    assert recorder.requests[0].headers["host"] == "[2001:db8::10]:3000"


async def test_credentials_go_as_basic_auth():
    recorder = Recorder(_response())
    await _get("https://op:s3cret@radar.example.com", recorder)
    expected = "Basic " + base64.b64encode(b"op:s3cret").decode()
    assert recorder.requests[0].headers["authorization"] == expected


async def test_no_credentials_means_no_authorization_header():
    recorder = Recorder(_response())
    await _get("radar.example.com", recorder)
    assert "authorization" not in recorder.requests[0].headers


async def test_compression_is_declined():
    recorder = Recorder(_response())
    await _get("radar.example.com", recorder)
    assert recorder.requests[0].headers["accept-encoding"] == "identity"


async def test_a_compressed_response_is_refused():
    recorder = Recorder(_response(body=b"\x1f\x8b", headers={"content-encoding": "gzip"}))
    assert (await _get_refusal("radar.example.com", recorder)).code == Refusal.BAD_RESPONSE


async def test_cookies_are_not_carried_between_requests():
    recorder = Recorder(
        _response(headers={"set-cookie": "session=abc; Path=/"}),
        _response(),
    )
    await _get("radar.example.com", recorder, times=2)
    assert "cookie" not in recorder.requests[1].headers


async def test_a_redirect_is_refused_and_not_followed():
    recorder = Recorder(_response(302, b"", {"location": "http://169.254.169.254/"}))
    refusal = await _get_refusal("radar.example.com", recorder)
    assert refusal.code == Refusal.REDIRECTED
    assert len(recorder.requests) == 1


async def test_an_error_status_is_refused():
    refusal = await _get_refusal("radar.example.com", Recorder(_response(500, b"")))
    assert refusal.code == Refusal.BAD_RESPONSE
    assert "500" in refusal.message


async def test_a_declared_oversized_body_is_refused():
    refusal = await _get_refusal("radar.example.com", Recorder(_response(body=b"x" * 2048)))
    assert refusal.code == Refusal.TOO_LARGE


async def test_an_undeclared_oversized_body_is_refused():
    async def chunks():
        for _ in range(64):
            yield b"x" * 64

    refusal = await _get_refusal("radar.example.com", Recorder(httpx.Response(200, content=chunks())))
    assert refusal.code == Refusal.TOO_LARGE


async def test_a_body_at_the_cap_is_accepted():
    _, bodies = await _get("radar.example.com", Recorder(_response(body=b"x" * 1024)))
    assert bodies == [b"x" * 1024]


async def test_the_next_vetted_address_is_tried_when_one_will_not_connect():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "192.0.2.10":
            raise httpx.ConnectError("refused")
        return _response()

    client, _ = await _get("radar.example.com", handler, addresses=("192.0.2.10", "192.0.2.11"))
    assert client.address == "192.0.2.11"


async def test_later_requests_stay_on_the_address_that_answered():
    hosts = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "192.0.2.10":
            raise httpx.ConnectError("refused")
        return _response()

    await _get("radar.example.com", handler, addresses=("192.0.2.10", "192.0.2.11"), times=3)
    assert hosts == ["192.0.2.10", "192.0.2.11", "192.0.2.11", "192.0.2.11"]


async def test_no_address_connecting_is_unreachable():
    refusal = await _get_refusal("radar.example.com", Recorder(httpx.ConnectError("refused")))
    assert refusal.code == Refusal.UNREACHABLE
    assert "192.0.2.10" not in refusal.message


async def test_a_read_timeout_is_timed_out():
    refusal = await _get_refusal("radar.example.com", Recorder(httpx.ReadTimeout("slow")))
    assert refusal.code == Refusal.TIMED_OUT


async def test_an_answer_that_is_not_http_is_a_bad_response():
    refusal = await _get_refusal("radar.example.com", Recorder(httpx.RemoteProtocolError("not HTTP")))
    assert refusal.code == Refusal.BAD_RESPONSE


async def test_a_dropped_connection_is_unreachable():
    refusal = await _get_refusal("radar.example.com", Recorder(httpx.ReadError("reset")))
    assert refusal.code == Refusal.UNREACHABLE


async def test_a_trickling_body_is_cut_off_by_the_deadline(monkeypatch):
    monkeypatch.setattr(polled_endpoint, "REQUEST_DEADLINE_S", 0.2)

    async def trickle():
        for _ in range(100):
            await asyncio.sleep(0.05)
            yield b"x"

    refusal = await _get_refusal("radar.example.com", Recorder(httpx.Response(200, content=trickle())))
    assert refusal.code == Refusal.TIMED_OUT


async def test_vetting_happens_before_any_request():
    recorder = Recorder(_response())
    with pytest.raises(EndpointRefused) as info:
        async with pinned_client(
            parse_endpoint("radar.example.com"),
            resolver=_resolver("10.0.0.1"),
            transport=httpx.MockTransport(recorder),
        ):
            pass
    assert info.value.code == Refusal.ADDRESS_NOT_PUBLIC
    assert recorder.requests == []


# ── Pinned client over real sockets ───────────────────────────────────────────


async def _body():
    return b'{"ok": true}'


async def test_a_real_request_carries_the_name_to_the_pinned_address():
    async with StubServer({"/api/config": _body}) as stub:
        endpoint = parse_endpoint(f"radar.example.com:{stub.port}")
        async with pinned_client(endpoint, resolver=resolve_to_loopback, policy=only_loopback) as client:
            assert await client.get("/api/config", max_bytes=1024) == b'{"ok": true}'
    assert stub.requests[0].headers["host"] == f"radar.example.com:{stub.port}"
    assert client.address == "127.0.0.1"


async def test_proxy_environment_is_ignored(monkeypatch):
    # A closed port: honouring the proxy would fail the request.
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    async with StubServer({"/api/config": _body}) as stub:
        endpoint = parse_endpoint(f"radar.example.com:{stub.port}")
        async with pinned_client(endpoint, resolver=resolve_to_loopback, policy=only_loopback) as client:
            assert await client.get("/api/config", max_bytes=1024) == b'{"ok": true}'


async def test_tls_verifies_the_certificate_against_the_name_not_the_address():
    server_ctx, client_ctx, seen_sni = tls_contexts("radar.example.com")
    async with StubServer({"/api/config": _body}, ssl_context=server_ctx) as stub:
        endpoint = parse_endpoint(f"https://radar.example.com:{stub.port}")
        transport = httpx.AsyncHTTPTransport(verify=client_ctx)
        async with pinned_client(
            endpoint, resolver=resolve_to_loopback, policy=only_loopback, transport=transport
        ) as client:
            assert await client.get("/api/config", max_bytes=1024) == b'{"ok": true}'
    assert seen_sni == ["radar.example.com"]
    assert stub.requests[0].headers["host"] == f"radar.example.com:{stub.port}"


async def test_tls_to_a_name_the_certificate_does_not_cover_is_refused():
    server_ctx, client_ctx, _ = tls_contexts("radar.example.com")
    async with StubServer({"/api/config": _body}, ssl_context=server_ctx) as stub:
        endpoint = parse_endpoint(f"https://other.example.net:{stub.port}")
        transport = httpx.AsyncHTTPTransport(verify=client_ctx)
        with pytest.raises(EndpointRefused) as info:
            async with pinned_client(
                endpoint, resolver=resolve_to_loopback, policy=only_loopback, transport=transport
            ) as client:
                await client.get("/api/config", max_bytes=1024)
    assert info.value.code == Refusal.TLS_FAILED
    assert stub.requests == []
