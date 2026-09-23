"""Bounded public HTTP reader; connect only to the validated public IP.

Redirects are revalidated, TLS still verifies the original hostname. This avoids
both internal-address URLs and a DNS change between validation and connection.
"""
from dataclasses import dataclass
import ipaddress
import socket
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

USER_AGENT = "StructuralCAD-RAG/1.0"
MAX_DOWNLOAD = 8 * 1024 * 1024


class WebFailure(ValueError):
    pass


@dataclass
class WebPage:
    url: str
    status: int
    headers: dict
    body: bytes


def normalize_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise WebFailure("網址格式不正確")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
                or parsed.password is not None or port not in {None, 80, 443}
                or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
            raise ValueError("not a public HTTP URL")
        authority = f"[{host}]" if ":" in host else host
        if port:
            authority += f":{port}"
        return urlunsplit((parsed.scheme, authority,
                           quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                           quote(parsed.query, safe="%/:?@!$&'()*+,;=-._~"), ""))
    except (ValueError, UnicodeError) as exc:
        raise WebFailure("僅支援公開 HTTP／HTTPS 網址") from exc


def public_addresses(host: str, port: int) -> list[str]:
    try:
        addresses = list(dict.fromkeys(row[4][0] for row in
                                      socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except OSError as exc:
        raise WebFailure("無法解析來源網站") from exc
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise WebFailure("不讀取本機、內網或保留位址")
    return sorted(addresses, key=lambda address: ":" in address)


def _request(url: str, max_bytes: int, deadline: float) -> WebPage:
    import certifi
    import urllib3
    parsed = urlsplit(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    address = public_addresses(host, port)[0]
    if parsed.scheme == "https":
        pool = urllib3.HTTPSConnectionPool(address, port=port, server_hostname=host,
                                           assert_hostname=host, cert_reqs="CERT_REQUIRED",
                                           ca_certs=certifi.where())
    else:
        pool = urllib3.HTTPConnectionPool(address, port=port)
    response = None
    try:
        response = pool.urlopen("GET", urlunsplit(("", "", parsed.path, parsed.query, "")),
                                headers={"Host": parsed.netloc, "User-Agent": USER_AGENT,
                                         "Accept": "text/html,application/pdf,text/plain,application/json",
                                         "Accept-Encoding": "identity"},
                                assert_same_host=False, redirect=False, retries=False,
                                preload_content=False,
                                timeout=urllib3.Timeout(connect=5, read=12, total=20))
        headers = {key.lower(): value for key, value in response.headers.items()}
        if response.status in {301, 302, 303, 307, 308}:
            return WebPage(url, response.status, headers, b"")
        if headers.get("content-length", "").isdigit() and int(headers["content-length"]) > max_bytes:
            raise WebFailure("來源檔案超過網路匯入大小限制")
        body = bytearray()
        for piece in response.stream(64 * 1024, decode_content=True):
            if time.monotonic() > deadline:
                raise WebFailure("來源網站讀取逾時")
            body.extend(piece)
            if len(body) > max_bytes:
                raise WebFailure("來源檔案超過網路匯入大小限制")
        return WebPage(url, response.status, headers, bytes(body))
    except WebFailure:
        raise
    except Exception as exc:
        raise WebFailure("來源網站連線失敗，未匯入") from exc
    finally:
        if response is not None:
            response.close()
        pool.close()


def public_get(url: str, *, max_bytes: int = MAX_DOWNLOAD, before_redirect=None) -> WebPage:
    url = normalize_url(url)
    deadline = time.monotonic() + 35
    for _ in range(4):
        if time.monotonic() > deadline:
            raise WebFailure("來源網站讀取逾時")
        response = _request(url, max_bytes, deadline)
        if response.status not in {301, 302, 303, 307, 308}:
            return response
        if not response.headers.get("location"):
            raise WebFailure("来源重新導向缺少網址")
        url = normalize_url(urljoin(url, response.headers["location"]))
        if before_redirect is not None:
            before_redirect(url)
    raise WebFailure("來源重新導向次數過多")
