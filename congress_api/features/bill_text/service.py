"""Bill text retrieval service: resolve, then serve from the persistent cache or
fetch-parse-build-publish (spec §10).

Per call:

1. Resolve the version and fetch the GovInfo package summary. The summary
   carries ``lastModified``; if the store already holds a servable package file
   built from that same lastModified, the XML download is skipped
   (``resolved.xml_bytes is None``).
2. On a hit, open the published file read-only and serve it --
   ``cache.index_hit`` true, no parse.
3. On a miss, download, parse, build into a temp file, close-and-validate,
   publish atomically (loser adopts), and serve the PUBLISHED file. If the
   store cannot produce a servable file the index is built in memory instead:
   the cache never fails the user's call.

``CONGRESSMCP_CACHE_ENABLED=false`` skips the store entirely: in-memory index,
discarded per call, full re-fetch and re-parse every time (§10 tunables).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace

from mcp.server.mcpserver import Context

from . import trace
from .cache import CacheSettings
from .client import ResolvedBillText, fetch_govinfo_package, resolve_and_fetch_bill_text
from .index import BillTextIndex
from .parser import ParsedBill, parse_bill_xml
from .store import PackageBuildError, PackageStore

logger = logging.getLogger(__name__)


@dataclass
class LoadedBillText:
    resolved: ResolvedBillText
    parsed: ParsedBill
    index: BillTextIndex
    timing: dict[str, float | None] = field(default_factory=dict)
    # True when the index was served from a published package file rather than
    # parsed and built on this call (§9 ``cache.index_hit``).
    index_hit: bool = False


_store: PackageStore | None = None
_store_key: tuple | None = None


def get_store() -> PackageStore | None:
    """The process-wide PackageStore for the current settings, or None when the
    persistent cache is disabled. Re-created if the environment's cache settings
    change (tests; operators toggling the env between calls)."""
    global _store, _store_key
    settings = CacheSettings.from_env()
    key = (settings.cache_dir, settings.enabled)
    if key != _store_key:
        if _store is not None:
            _store.close()
        _store = PackageStore(settings) if settings.enabled else None
        _store_key = key
    return _store


def reset_store() -> None:
    """Drop the cached PackageStore (tests)."""
    global _store, _store_key
    if _store is not None:
        _store.close()
    _store = None
    _store_key = None


async def load_bill_text(ctx: Context, congress: int, bill_type: str, number: int, version: str | None) -> LoadedBillText:
    store = get_store()

    def cached(package_id: str, last_modified: str | None) -> bool:
        # Asked by the client between the package summary and the XML download:
        # do we already hold a servable index for these exact source bytes?
        return store is not None and store.fresh_path(package_id, last_modified) is not None

    # Timing legs (§4): fetch_ms is every network leg (resolution, package
    # summary, XML download, any refetch); parse_ms is the parse or null when no
    # parse ran; index_ms is building the index on a miss or opening the
    # published file on a hit.
    fetch_s = 0.0
    t0 = time.perf_counter()
    resolved = await resolve_and_fetch_bill_text(
        ctx, congress, bill_type, number, version, skip_download=cached if store is not None else None
    )
    fetch_s += time.perf_counter() - t0

    index: BillTextIndex | None = None
    index_hit = False
    open_s = 0.0
    if resolved.xml_bytes is None:
        # The store said it had the file a moment ago. Open it; if it vanished in
        # between (eviction, `cache clear`, another process), refetch -- the
        # recovery table's "file missing -> treat as miss, refetch".
        t_open = time.perf_counter()
        index = store.open(resolved.package_id, resolved.last_modified) if store is not None else None
        open_s = time.perf_counter() - t_open
        if index is not None:
            index_hit = True
        else:
            t_refetch = time.perf_counter()
            last_modified, xml_bytes = await fetch_govinfo_package(resolved.package_id)
            fetch_s += time.perf_counter() - t_refetch
            resolved = replace(resolved, last_modified=last_modified, xml_bytes=xml_bytes)

    # Stamp which exact bytes produced this response for replay (debug tracing only;
    # the sha256 is computed solely when CONGRESSMCP_TRACE_DIR is set).
    trace.set_source(resolved.package_id, resolved.version, resolved.xml_bytes)

    parse_ms: float | None = None
    if index is None:
        assert resolved.xml_bytes is not None
        t_parse = time.perf_counter()
        parsed = parse_bill_xml(resolved.xml_bytes, resolved.package_id, resolved.version, resolved.last_modified)
        t_index = time.perf_counter()
        parse_ms = round((t_index - t_parse) * 1000, 1)
        if store is not None:
            try:
                index, _published = store.build_and_publish(parsed, last_modified=resolved.last_modified)
            except PackageBuildError as exc:
                logger.warning("%s; serving an in-memory index for this call", exc)
            except Exception as exc:  # the cache must never fail the user's call
                logger.warning("persistent cache failed for %s (%s); serving in memory", resolved.package_id, exc)
        if index is None:
            index = BillTextIndex(parsed)
        else:
            parsed = index.parsed
        index_ms = round((time.perf_counter() - t_index) * 1000, 1)
    else:
        parsed = index.parsed
        index_ms = round(open_s * 1000, 1)
    timing = {
        "fetch_ms": round(fetch_s * 1000, 1),
        "parse_ms": parse_ms,
        "index_ms": index_ms,
    }
    return LoadedBillText(resolved=resolved, parsed=parsed, index=index, timing=timing, index_hit=index_hit)
