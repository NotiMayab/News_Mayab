"""Cliente HTTP condicional para feeds RSS/Atom con controles anti-SSRF, límite de 2 MB y detección WAF."""

from __future__ import annotations

import ipaddress
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

MAX_FEED_BYTES = 2 * 1024 * 1024  # 2 MiB límite duro por feed (Modelo de Amenazas #1)
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_REDIRECTS = 3
USER_AGENT = (
    "NewsMayabRSSReader/1.0 "
    "(+https://newsmayab.example.org/metodologia/; agregador-transparente-sin-ia)"
)


class IngestionSecurityError(Exception):
    """Error de seguridad o protocolo al descargar un feed (SSRF, esquema inválido, exceso de tamaño)."""

    def __init__(self, message: str, error_kind: str) -> None:
        super().__init__(message)
        self.error_kind = error_kind


@dataclass(frozen=True, slots=True)
class FeedFetchResult:
    status_code: int
    not_modified: bool
    xml_bytes: bytes
    etag: str | None
    last_modified: str | None
    latency_ms: int
    error_kind: str | None = None
    error_detail: str | None = None


def validate_public_http_url(
    url: str,
    dns_resolver: Callable[[str, int], list[ str ]] | None = None,
) -> urllib.parse.SplitResult:
    """Valida que una URL sea http(s) y que ninguna de sus IPs resueltas sea privada, loopback o link-local."""
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise IngestionSecurityError(
            f"Esquema de URL prohibido: {parsed.scheme!r}", "SSRF_BLOCKED"
        )
    if not parsed.hostname:
        raise IngestionSecurityError("URL sin hostname válido", "SSRF_BLOCKED")
    if parsed.username or parsed.password:
        raise IngestionSecurityError(
            "Credenciales embebidas en URL no permitidas", "SSRF_BLOCKED"
        )

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in (80, 443, 8080, 8443):
        raise IngestionSecurityError(f"Puerto no estándar bloqueado: {port}", "SSRF_BLOCKED")

    ips = _resolve_host_ips(parsed.hostname, port, dns_resolver)
    if not ips:
        raise IngestionSecurityError(
            f"No se pudo resolver ninguna IP para {parsed.hostname}", "DNS_OR_CONNECTION"
        )

    for ip_str in ips:
        ip_obj = ipaddress.ip_address(ip_str)
        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
            or not ip_obj.is_global
        ):
            raise IngestionSecurityError(
                f"Bloqueo anti-SSRF: {parsed.hostname} resuelve a IP no pública ({ip_str})",
                "SSRF_BLOCKED",
            )
    return parsed


def _resolve_host_ips(
    hostname: str,
    port: int,
    dns_resolver: Callable[[str, int], list[str]] | None = None,
) -> list[str]:
    if dns_resolver is not None:
        return dns_resolver(hostname, port)

    # Si ya es una literal IP, la devolvemos directamente para validación
    try:
        ipaddress.ip_address(hostname)
        return [hostname]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise IngestionSecurityError(f"Error DNS en {hostname}: {exc}", "DNS_OR_CONNECTION") from exc

    resolved: list[str] = []
    for info in infos:
        sockaddr = info[4]
        ip_str = str(sockaddr[0])
        if ip_str not in resolved:
            resolved.append(ip_str)
    return resolved


class _NoAutoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Desactiva el seguimiento automático de redirecciones para validar cada salto contra SSRF."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def looks_like_waf_or_html_challenge(payload: bytes, content_type: str = "") -> bool:
    """Detecta páginas HTML de bloqueo WAF (Cloudflare, FortiWeb, Imperva) devueltas en vez de XML."""
    head = payload[:2048].lstrip()
    head_lower = head.lower()

    # Si empieza legítimamente como XML/RSS/Atom, no es reto WAF
    if (
        head_lower.startswith(b"<?xml")
        or head_lower.startswith(b"<rss")
        or head_lower.startswith(b"<feed")
        or head_lower.startswith(b"<rdf:rdf")
    ):
        return False

    waf_markers = (
        b"just a moment...",
        b"cf-chl-bypass",
        b"attention required! | cloudflare",
        b"fortiweb",
        b"_incapsula_resource",
        b"access denied",
        b"<!doctype html",
        b"<html",
    )
    return any(marker in head_lower for marker in waf_markers) or "text/html" in content_type.lower()


def fetch_feed_safely(
    rss_url: str,
    etag: str | None = None,
    last_modified: str | None = None,
    max_bytes: int = MAX_FEED_BYTES,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    dns_resolver: Callable[[str, int], list[str]] | None = None,
) -> FeedFetchResult:
    """Descarga un feed RSS/Atom con validación anti-SSRF en cada redirección y límite de bytes."""
    start_ts = time.monotonic()
    current_url = rss_url
    opener = urllib.request.build_opener(_NoAutoRedirectHandler())

    for hop in range(MAX_REDIRECTS + 1):
        try:
            validate_public_http_url(current_url, dns_resolver=dns_resolver)
        except IngestionSecurityError as exc:
            return FeedFetchResult(
                status_code=0,
                not_modified=False,
                xml_bytes=b"",
                etag=etag,
                last_modified=last_modified,
                latency_ms=int((time.monotonic() - start_ts) * 1000),
                error_kind=exc.error_kind,
                error_detail=str(exc),
            )

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8",
            "Accept-Encoding": "identity",
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        req = urllib.request.Request(current_url, headers=headers, method="GET")
        try:
            with opener.open(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
                resp_headers = resp.headers
                content_type = resp_headers.get("Content-Type", "")
                new_etag = resp_headers.get("ETag") or etag
                new_last_mod = resp_headers.get("Last-Modified") or last_modified

                chunks: list[bytes] = []
                total_read = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total_read += len(chunk)
                    if total_read > max_bytes:
                        return FeedFetchResult(
                            status_code=status,
                            not_modified=False,
                            xml_bytes=b"",
                            etag=etag,
                            last_modified=last_modified,
                            latency_ms=int((time.monotonic() - start_ts) * 1000),
                            error_kind="PAYLOAD_TOO_LARGE",
                            error_detail=f"El feed superó el límite de {max_bytes} bytes",
                        )
                    chunks.append(chunk)

                payload = b"".join(chunks)
                if looks_like_waf_or_html_challenge(payload, content_type):
                    return FeedFetchResult(
                        status_code=status,
                        not_modified=False,
                        xml_bytes=b"",
                        etag=etag,
                        last_modified=last_modified,
                        latency_ms=int((time.monotonic() - start_ts) * 1000),
                        error_kind="WAF_OR_GEO_BLOCK",
                        error_detail="El servidor devolvió HTML/reto WAF en lugar de XML",
                    )

                return FeedFetchResult(
                    status_code=status,
                    not_modified=False,
                    xml_bytes=payload,
                    etag=new_etag,
                    last_modified=new_last_mod,
                    latency_ms=int((time.monotonic() - start_ts) * 1000),
                )

        except urllib.error.HTTPError as http_err:
            code = http_err.code
            if code == 304:
                return FeedFetchResult(
                    status_code=304,
                    not_modified=True,
                    xml_bytes=b"",
                    etag=etag,
                    last_modified=last_modified,
                    latency_ms=int((time.monotonic() - start_ts) * 1000),
                )
            if code in (301, 302, 303, 307, 308):
                location = http_err.headers.get("Location") if http_err.headers else None
                if not location:
                    return FeedFetchResult(
                        status_code=code,
                        not_modified=False,
                        xml_bytes=b"",
                        etag=etag,
                        last_modified=last_modified,
                        latency_ms=int((time.monotonic() - start_ts) * 1000),
                        error_kind="HTTP_4XX",
                        error_detail="Redirección sin cabecera Location",
                    )
                if hop == MAX_REDIRECTS:
                    return FeedFetchResult(
                        status_code=code,
                        not_modified=False,
                        xml_bytes=b"",
                        etag=etag,
                        last_modified=last_modified,
                        latency_ms=int((time.monotonic() - start_ts) * 1000),
                        error_kind="SSRF_BLOCKED",
                        error_detail=f"Excedido el máximo de {MAX_REDIRECTS} redirecciones",
                    )
                current_url = urllib.parse.urljoin(current_url, location)
                continue

            err_body = b""
            try:
                err_body = http_err.read(2048)
            except Exception:
                pass
            cf_mitigated = (
                http_err.headers.get("cf-mitigated", "") if http_err.headers else ""
            )
            if code in (403, 503) and (
                cf_mitigated or looks_like_waf_or_html_challenge(err_body)
            ):
                kind = "WAF_OR_GEO_BLOCK"
            elif 400 <= code < 500:
                kind = "HTTP_4XX"
            else:
                kind = "HTTP_5XX"

            return FeedFetchResult(
                status_code=code,
                not_modified=False,
                xml_bytes=b"",
                etag=etag,
                last_modified=last_modified,
                latency_ms=int((time.monotonic() - start_ts) * 1000),
                error_kind=kind,
                error_detail=f"HTTP {code}: {http_err.reason}",
            )
        except TimeoutError as exc:
            return FeedFetchResult(
                status_code=0,
                not_modified=False,
                xml_bytes=b"",
                etag=etag,
                last_modified=last_modified,
                latency_ms=int((time.monotonic() - start_ts) * 1000),
                error_kind="TIMEOUT",
                error_detail=str(exc),
            )
        except urllib.error.URLError as url_err:
            reason = url_err.reason
            kind = "TIMEOUT" if isinstance(reason, TimeoutError) else "DNS_OR_CONNECTION"
            return FeedFetchResult(
                status_code=0,
                not_modified=False,
                xml_bytes=b"",
                etag=etag,
                last_modified=last_modified,
                latency_ms=int((time.monotonic() - start_ts) * 1000),
                error_kind=kind,
                error_detail=str(reason),
            )

    return FeedFetchResult(
        status_code=0,
        not_modified=False,
        xml_bytes=b"",
        etag=etag,
        last_modified=last_modified,
        latency_ms=int((time.monotonic() - start_ts) * 1000),
        error_kind="SSRF_BLOCKED",
        error_detail="Límite de redirecciones alcanzado",
    )
