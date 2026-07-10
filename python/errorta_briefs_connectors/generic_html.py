"""GenericHTMLConnector — third reference SourceConnector (F008c-html track).

The generic HTML connector is the audit-flagged demonstration of the
compliance gate's strict-by-default posture: without an explicit
``license_override`` it produces docs with ``license='Unknown'`` and
``redistribution_allowed=False``, which the downstream compliance gate
refuses. That refusal is the intended demonstration that brief authors
must consciously opt in to redistribution on a per-site basis (and bear
the responsibility of having confirmed the upstream license).

Politeness: per-host monotonic-clock gate with random.uniform(2.0, 3.0)
jitter; honors robots.txt ``crawl_delay`` when it is larger.

robots.txt: parsed via stdlib ``urllib.robotparser`` and cached per host.
Per RFC 9309 a fetch failure on robots.txt is treated as allowed.

canonical_id: ``"html:" + sha256(normalized_url).hexdigest()[:16]`` where
the URL is normalized by lowercasing scheme+host, stripping the fragment,
sorting query params, and stripping any trailing path slash.

Discovery: ``search(page=0)`` yields one SourceDoc per seed URL.
``search(page=1)`` is only valid when ``max_hops==1``; it parses anchors
from each fetched seed and yields up to 50 same-host links per seed,
deduped by canonical_id. ``max_hops > 1`` is rejected at ``__init__``
time for v0.3.

fetch(): re-runs the robots + politeness gate (defense in depth) and
returns raw HTML bytes. Chunking is delegated to ``errorta_extract.html``
(single source of truth — this connector does not chunk).

Content-type whitelist: ``text/html``, ``application/xhtml+xml``.
Body size cap: 10 MB. JS-only pages (extracted text < 200 chars) raise
``FatalError`` per-doc so the brief author sees the broken seed instead
of silently emitting an empty document.

User-Agent: ``Errorta/0.3 (+https://github.com/wiggins-j/errorta_app; brief-driven-collection)``.
"""
from __future__ import annotations

import hashlib
import logging
import random
import re
import time
import urllib.robotparser
from html.parser import HTMLParser
from typing import Any, Iterator, Optional
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse, parse_qsl, urlencode

import httpx

# Resolve base classes at import time (same pattern arxiv.py uses — the
# package __init__.py defines them before importing this module).
from errorta_briefs_connectors import (  # noqa: E402
    FatalError,
    RetryableError,
    SourceConnector,
    SourceDoc,
)


logger = logging.getLogger(__name__)


USER_AGENT = "Errorta/0.3 (+https://github.com/wiggins-j/errorta_app; brief-driven-collection)"

ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml")

MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MB
MIN_EXTRACTED_TEXT_CHARS = 200
MAX_LINKS_PER_SEED = 50

POLITENESS_MIN_S = 2.0
POLITENESS_MAX_S = 3.0


# Strip <script>/<style> blocks so we can estimate visible-text length
# without dragging in a full HTML parser dependency. The regex is anchored
# only on tag names — case-insensitive and DOTALL so it spans newlines.
_SCRIPT_OR_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class _AnchorCollector(HTMLParser):
    """Minimal HTMLParser that records every <a href=...> value seen."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "a":
            return
        for k, v in attrs:
            if k.lower() == "href" and v:
                self.hrefs.append(v)


def _normalize_url(url: str) -> str:
    """Canonical form for hashing: lowercase scheme+host, drop fragment,
    sort query params, strip a trailing path slash (but keep "/" itself).
    """
    p = urlparse(url)
    scheme = p.scheme.lower()
    netloc = p.netloc.lower()
    path = p.path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    # Sort query params for stable identity across permutation-only variants.
    if p.query:
        items = sorted(parse_qsl(p.query, keep_blank_values=True))
        query = urlencode(items)
    else:
        query = ""
    return urlunparse((scheme, netloc, path, p.params, query, ""))


def _canonical_id_for(url: str) -> str:
    norm = _normalize_url(url)
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    return f"html:{digest}"


def _extract_visible_text(body: bytes) -> str:
    """Best-effort visible-text estimate for the JS-only detector.

    Not a replacement for ``errorta_extract.html.extract`` — only used to
    decide whether the page is so empty after stripping that the brief
    author almost certainly hit a JS-only SPA seed.
    """
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return ""
    text = _SCRIPT_OR_STYLE_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


class GenericHTMLConnector(SourceConnector):
    """Reference connector for arbitrary HTML pages declared in a brief.

    Config keys (validated in __init__):
        seed_urls       list[str]      Required. Initial pages to fetch.
        license_override str | None    Optional. Brief author asserts the
                                       upstream license. Without it, every
                                       doc is refused by the compliance gate.
        max_hops        int            Default 0. 0 = seeds only; 1 = seeds
                                       plus one hop of same-host anchors.
                                       >1 is rejected with FatalError.
        same_host_only  bool           Default True. Restrict hop-1 links to
                                       the seed's host.

    Test hooks:
        http_client     httpx.Client   Optional pre-configured client (e.g.
                                       with a MockTransport). When provided,
                                       the connector does not construct its
                                       own.
    """

    name = "generic_html"

    def __init__(self, config: dict) -> None:
        seed_urls = config.get("seed_urls")
        if not seed_urls or not isinstance(seed_urls, list):
            raise FatalError(
                "generic_html config requires non-empty 'seed_urls' list"
            )
        self.seed_urls: list[str] = list(seed_urls)

        max_hops = config.get("max_hops", 0)
        if not isinstance(max_hops, int) or max_hops < 0:
            raise FatalError(
                f"generic_html max_hops must be a non-negative int, got {max_hops!r}"
            )
        if max_hops > 1:
            raise FatalError(
                "generic_html max_hops>1 not supported in v0.3 "
                "(prevents unbounded recursive crawls)"
            )
        self.max_hops: int = max_hops

        self.same_host_only: bool = bool(config.get("same_host_only", True))
        self.license_override: Optional[str] = config.get("license_override")

        http_client = config.get("http_client")
        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            self._client = httpx.Client(
                timeout=30.0, headers={"User-Agent": USER_AGENT}
            )
            self._owns_client = True

        # Per-host monotonic timestamp of last successful request.
        self._last_request_at: dict[str, float] = {}
        # Per-host RobotFileParser cache. Missing key means "not yet fetched".
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        # Page-0 seeds' raw bytes, kept so a follow-up search(page=1) can
        # extract anchors without re-fetching. None means "not yet fetched".
        self._seed_bodies: dict[str, bytes] = {}

    # ------------------------------------------------------------------ search

    def search(self, *, page: int = 0) -> Iterator[SourceDoc]:
        if page == 0:
            for url in self.seed_urls:
                yield self._make_doc(url)
            return

        if page == 1:
            if self.max_hops < 1:
                return
            yield from self._discover_hop1()
            return

        # Page index out of range — yield nothing rather than raising. Runner
        # uses an empty page as the end-of-iteration signal.
        return

    def _discover_hop1(self) -> Iterator[SourceDoc]:
        seen: set[str] = {_canonical_id_for(u) for u in self.seed_urls}
        for seed in self.seed_urls:
            body = self._seed_bodies.get(seed)
            if body is None:
                # Hop-1 was requested before page 0 ran; fetch the seed now.
                body = self._raw_get(seed)
                self._seed_bodies[seed] = body
            parser = _AnchorCollector()
            try:
                parser.feed(body.decode("utf-8", errors="replace"))
            except Exception:
                continue
            seed_host = urlparse(seed).netloc.lower()
            emitted_for_seed = 0
            for href in parser.hrefs:
                if emitted_for_seed >= MAX_LINKS_PER_SEED:
                    break
                absolute = urljoin(seed, href)
                absolute, _frag = urldefrag(absolute)
                p = urlparse(absolute)
                if p.scheme not in ("http", "https"):
                    continue
                if self.same_host_only and p.netloc.lower() != seed_host:
                    continue
                cid = _canonical_id_for(absolute)
                if cid in seen:
                    continue
                seen.add(cid)
                emitted_for_seed += 1
                yield self._make_doc(absolute)

    def _make_doc(self, url: str) -> SourceDoc:
        license_value = self.license_override if self.license_override else "Unknown"
        redistribution = self.license_override is not None
        cid = _canonical_id_for(url)
        return SourceDoc(
            canonical_id=cid,
            title=url,  # Title is unknown pre-fetch; downstream extractor refines.
            source_url=url,
            publication_date=None,
            sensitivity_class="Public",
            redistribution_allowed=redistribution,
            license=license_value,
            extra={
                "source_url": url,
                "sensitivity_class": "Public",
                "redistribution_allowed": redistribution,
                "license": license_value,
                "license_override_applied": self.license_override is not None,
                "file_ext": ".html",
            },
        )

    # ------------------------------------------------------------------- fetch

    def fetch(self, doc: SourceDoc) -> bytes:
        url = doc.source_url
        body = self._raw_get(url)
        # Defense in depth: if a page-0 fetch satisfied a seed already, keep
        # it around so a later hop-1 discovery doesn't have to re-issue.
        if url in self.seed_urls:
            self._seed_bodies[url] = body
        text = _extract_visible_text(body)
        if len(text) < MIN_EXTRACTED_TEXT_CHARS:
            raise FatalError(
                f"generic_html: page at {url} has only {len(text)} chars of visible "
                f"text after stripping scripts/styles — likely a JS-only SPA seed. "
                f"Brief author should pick a different entry point."
            )
        return body

    # ---------------------------------------------------------------- identity

    def canonical_id(self, doc: SourceDoc) -> str:
        return doc.canonical_id

    def metadata(self, doc: SourceDoc) -> dict:
        return doc.extra

    def status(self) -> dict:
        return {
            "source": "generic_html",
            "reachable": True,
            "rate_limit": "2-3s jitter per host",
            "license_override": self.license_override,
        }

    # ------------------------------------------------------------- internals

    def _raw_get(self, url: str) -> bytes:
        """robots + politeness gated GET with content-type and size checks."""
        if not self._robots_allows(url):
            logger.info(
                "generic_html: robots.txt disallow blocked fetch of %s", url
            )
            raise FatalError(
                f"generic_html: robots.txt disallows fetch of {url}"
            )

        self._sleep_for_politeness(url)
        resp = self._client.get(url)
        self._classify_response(resp)

        ctype = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if ctype and ctype not in ALLOWED_CONTENT_TYPES:
            raise FatalError(
                f"generic_html: refused content-type {ctype!r} for {url}; "
                f"only {ALLOWED_CONTENT_TYPES} are accepted"
            )
        body = resp.content
        if len(body) > MAX_BODY_BYTES:
            raise FatalError(
                f"generic_html: body for {url} is {len(body)} bytes, "
                f"over the {MAX_BODY_BYTES}-byte cap"
            )
        return body

    def _classify_response(self, resp: httpx.Response) -> None:
        status = resp.status_code
        if 200 <= status < 300:
            return
        if status == 429 or status == 503:
            retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
            raise RetryableError(
                f"generic_html: upstream returned {status}",
                retry_after_s=retry_after,
            )
        if 500 <= status < 600:
            retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
            raise RetryableError(
                f"generic_html: upstream returned {status}",
                retry_after_s=retry_after,
            )
        raise FatalError(f"generic_html: upstream returned {status}")

    @staticmethod
    def _parse_retry_after(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    # ----------------------------------------------------------------- robots

    def _robots_allows(self, url: str) -> bool:
        """Return True if robots.txt allows fetching ``url`` for our UA.

        Per RFC 9309 a missing or unreachable robots.txt MUST be treated as
        full allow. We additionally cache one parser per host.
        """
        p = urlparse(url)
        host_key = f"{p.scheme}://{p.netloc}"
        rp = self._robots.get(host_key)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            robots_url = f"{host_key}/robots.txt"
            rp.set_url(robots_url)
            try:
                resp = self._client.get(robots_url)
                if 200 <= resp.status_code < 300:
                    rp.parse(resp.text.splitlines())
                else:
                    # Treat any non-2xx as "no robots policy".
                    rp.parse([])
            except Exception as exc:  # pragma: no cover - defensive
                logger.info(
                    "generic_html: robots.txt fetch failed for %s (%s); "
                    "treating as allow per RFC 9309",
                    host_key,
                    exc,
                )
                rp.parse([])
            self._robots[host_key] = rp
        return rp.can_fetch(USER_AGENT, url)

    # -------------------------------------------------------------- politeness

    def _sleep_for_politeness(self, url: str) -> None:
        """Per-host monotonic-clock gate with random 2-3s jitter.

        Honors robots.txt crawl_delay when larger than the jitter draw.
        """
        host = urlparse(url).netloc.lower()
        gate = random.uniform(POLITENESS_MIN_S, POLITENESS_MAX_S)
        # If robots.txt set a crawl-delay, honor it when greater.
        host_key = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        rp = self._robots.get(host_key)
        if rp is not None:
            try:
                cd = rp.crawl_delay(USER_AGENT)
                if cd is not None and float(cd) > gate:
                    gate = float(cd)
            except Exception:  # pragma: no cover - defensive
                pass

        now = time.monotonic()
        last = self._last_request_at.get(host)
        if last is not None:
            elapsed = now - last
            remaining = gate - elapsed
            if remaining > 0:
                time.sleep(remaining)
                now = time.monotonic()
        self._last_request_at[host] = now
