"""Build a callback URL from the current public tunnel and the stored secret."""

import ipaddress
import string

import httpx


def build_webhook_url(tunnel_url: str, token: str) -> str:
    if not token or any(char not in string.ascii_letters + string.digits + "-_" for char in token):
        raise ValueError("WEBHOOK_TOKEN must contain only letters, digits, '-' and '_'")
    raw = tunnel_url.strip()
    if not raw or any(char.isspace() or ord(char) < 32 for char in raw) or "\\" in raw:
        raise ValueError("Enter the public HTTPS origin of the tunnel")
    try:
        url = httpx.URL(raw)
    except (httpx.InvalidURL, ValueError):
        raise ValueError("Enter the public HTTPS origin of the tunnel") from None
    if (
        url.scheme != "https"
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
        or url.raw_path != b"/"
        or "?" in raw
        or "#" in raw
    ):
        raise ValueError("Enter an HTTPS origin without path, credentials, query or fragment")
    host = url.host.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("The tunnel must have a public host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("The tunnel must have a public host") from None
    else:
        if not address.is_global:
            raise ValueError("The tunnel must have a public host")
    return str(url.copy_with(path=f"/webhooks/umnico/{token}"))
