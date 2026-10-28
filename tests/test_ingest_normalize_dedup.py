"""Pruebas de Fase C: Seguridad anti-SSRF, anti-XXE/Billion Laughs, sanitización XSS y deduplicación."""

from __future__ import annotations

import unittest

from mayab_news.ingest.http_client import (
    IngestionSecurityError,
    looks_like_waf_or_html_challenge,
    validate_public_http_url,
)
from mayab_news.ingest.xml_parser import (
    FeedParseError,
    RawFeedItem,
    parse_rss_or_atom_bytes,
)
from mayab_news.nlp.dedup import (
    compute_simhash_hex,
    is_near_duplicate,
    simhash_hamming_distance,
)
from mayab_news.nlp.normalize import (
    MAX_EXCERPT_CHARS,
    canonicalize_url,
    normalize_feed_item,
    strip_html_to_plain_text,
    truncate_excerpt,
)


class TestSecurityIngestionAndNormalization(unittest.TestCase):
    def test_ssrf_blocks_loopback_private_and_cloud_metadata(self) -> None:
        """Modelo de Amenazas #1: Bloqueo de IPs privadas, loopback, IPv6 local y metadatos cloud."""
        blocked_urls = [
            "http://127.0.0.1/rss.xml",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/feed",
            "http://192.168.1.254/rss",
            "http://172.16.5.4/atom.xml",
            "http://[::1]/rss",
            "file:///etc/passwd",
            "ftp://example.com/feed.xml",
            "https://usuario:password@example.com/rss",
            "https://example.com:22/rss",
        ]
        for bad_url in blocked_urls:
            with self.subTest(url=bad_url):
                with self.assertRaises(IngestionSecurityError) as ctx:
                    validate_public_http_url(bad_url)
                self.assertEqual(ctx.exception.error_kind, "SSRF_BLOCKED")

        # Simular DNS Rebinding donde un dominio público resuelve a 127.0.0.1
        with self.assertRaises(IngestionSecurityError):
            validate_public_http_url(
                "https://malicious.example.org/rss",
                dns_resolver=lambda host, port: ["127.0.0.1"],
            )

        # Un dominio que resuelve a IP global pública debe pasar
        parsed = validate_public_http_url(
            "https://www.lajornadamaya.mx/rss",
            dns_resolver=lambda host, port: ["104.26.10.22"],
        )
        self.assertEqual(parsed.hostname, "www.lajornadamaya.mx")

    def test_xxe_and_billion_laughs_blocked_by_defusedxml(self) -> None:
        """Modelo de Amenazas #1: Bloqueo estricto de entidades XML externas (XXE) y bombas DTD."""
        xxe_payload = b"""<?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
        <rss version="2.0">
          <channel>
            <item>
              <title>&xxe;</title>
              <link>https://example.org/1</link>
            </item>
          </channel>
        </rss>
        """
        with self.assertRaises(FeedParseError):
            parse_rss_or_atom_bytes(xxe_payload)

        billion_laughs_payload = b"""<?xml version="1.0"?>
        <!DOCTYPE lolz [
         <!ENTITY lol "lol">
         <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
        ]>
        <rss version="2.0"><channel><title>&lol2;</title></channel></rss>
        """
        with self.assertRaises(FeedParseError):
            parse_rss_or_atom_bytes(billion_laughs_payload)

    def test_waf_html_challenge_detection(self) -> None:
        """Sección 5.3: Detectar cuando un portal .gob.mx o WAF devuelve HTML en lugar de XML."""
        cf_challenge = b"<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"
        self.assertTrue(looks_like_waf_or_html_challenge(cf_challenge, "text/html"))

        valid_rss = b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
        self.assertFalse(looks_like_waf_or_html_challenge(valid_rss, "application/rss+xml"))

    def test_rss_and_atom_valid_parsing(self) -> None:
        rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
          <channel>
            <title>Medio Yucateco</title>
            <item>
              <title>Anuncian programa de reforestaci&#243;n en M&#233;rida</title>
              <link>https://medio.example.mx/nota-1?utm_source=rss&amp;fbclid=1234</link>
              <description><![CDATA[<p>El <b>Ayuntamiento de Mérida</b> informó que plantarán 10 mil árboles nativos.</p><script>alert('xss')</script>]]></description>
              <dc:creator>Redacción Mérida</dc:creator>
              <pubDate>Tue, 27 Oct 2026 08:30:00 -0600</pubDate>
            </item>
          </channel>
        </rss>
        """.encode("utf-8")
        items = parse_rss_or_atom_bytes(rss_xml)
        self.assertEqual(len(items), 1)
        norm = normalize_feed_item(items[0], source_id="yuc_test")
        self.assertEqual(norm.canonical_url, "https://medio.example.mx/nota-1")
        self.assertEqual(norm.published_at_utc, "2026-10-27T14:30:00Z")
        self.assertNotIn("<", norm.excerpt_plain)
        self.assertNotIn("alert", norm.excerpt_plain)
        self.assertIn("Ayuntamiento de Mérida", norm.excerpt_plain)

    def test_xss_sanitization_and_javascript_url_rejection(self) -> None:
        """Modelo de Amenazas #2: Cero etiquetas HTML en texto y bloqueo de javascript:/data:."""
        dirty_html = (
            'Hola <img src=x onerror="alert(1)"> mundo '
            "<script>evil()</script>&lt;script&gt;nested()&lt;/script&gt; fin"
        )
        clean = strip_html_to_plain_text(dirty_html)
        self.assertEqual(clean, "Hola mundo fin")
        self.assertNotIn("<", clean)
        self.assertNotIn(">", clean)

        for dangerous_link in ("javascript:alert(1)", "data:text/html,<script>", "vbscript:msgbox"):
            with self.assertRaises(ValueError):
                canonicalize_url(dangerous_link)

    def test_excerpt_truncation_respects_240_limit_and_opt_out(self) -> None:
        """Supuesto S4 y Sección 7.2: Límite estricto <=240 chars y modo headline_and_link_only."""
        long_text = (
            "Investigadores del Centro de Investigación Científica de Yucatán presentaron "
            "un nuevo estudio sobre la recarga del acuífero kárstico en la península de Yucatán "
            "tras las lluvias extraordinarias registradas durante la temporada de huracanes en la región."
        )
        self.assertGreater(len(long_text), MAX_EXCERPT_CHARS)

        truncated = truncate_excerpt(long_text)
        self.assertLessEqual(len(truncated), MAX_EXCERPT_CHARS)
        self.assertTrue(truncated.endswith("…"))

        # Modo opt-out (headline_and_link_only=True) devuelve extracto vacío
        raw_item = RawFeedItem(
            title_raw="Estudio del acuífero en Yucatán",
            link_raw="https://cicy.example.mx/estudio",
            summary_raw=long_text,
            author_raw="CICY",
            published_at_utc="2026-10-27T12:00:00Z",
        )
        norm_opt_out = normalize_feed_item(
            raw_item, source_id="yuc_cicy_ciencia", headline_and_link_only=True
        )
        self.assertEqual(norm_opt_out.excerpt_plain, "")

    def test_near_duplicate_syndicated_wire_detection(self) -> None:
        """Sección 2.1: Detectar cables sindicados casi idénticos y distinguir notas diferentes."""
        wire_a = (
            "Mérida, Yucatán. La Secretaría de Seguridad Pública informó sobre el cierre "
            "temporal del kilómetro 28 del Anillo Periférico de Mérida por obras de repavimentación "
            "durante la madrugada de este miércoles."
        )
        wire_b = (
            "La Secretaría de Seguridad Pública informó sobre el cierre temporal del kilómetro 28 "
            "del Anillo Periférico de Mérida por obras de repavimentación durante la madrugada "
            "de este miércoles."
        )
        different_news = (
            "La Secretaría de Seguridad Pública informó sobre un operativo vial en el puerto de "
            "Progreso con motivo del inicio de las vacaciones de verano en la costa yucateca."
        )

        dup_ab, jaccard_ab, hamming_ab = is_near_duplicate(wire_a, wire_b)
        self.assertTrue(dup_ab)
        self.assertGreaterEqual(jaccard_ab, 0.82)

        dup_diff, jaccard_diff, _ = is_near_duplicate(wire_a, different_news)
        self.assertFalse(dup_diff)
        self.assertLess(jaccard_diff, 0.35)

        # Verificar determinismo de SimHash
        self.assertEqual(compute_simhash_hex(wire_a), compute_simhash_hex(wire_a))
        self.assertEqual(simhash_hamming_distance("000000000000000f", "0000000000000000"), 4)


if __name__ == "__main__":
    unittest.main()
