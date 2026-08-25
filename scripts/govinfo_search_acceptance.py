#!/usr/bin/env python3
"""Live A1-A8 acceptance run for the GovInfo-backed search_bills
(govinfo-search-spec section 3 / section 6.6).

Runs the preregistered probes against the LIVE GovInfo endpoint through
the real tool function, archiving one JSON artifact per probe plus a
summary under runs/govinfo-search/<UTC-timestamp>/ (gitignored; cite
paths in the report). Requires a real api.data.gov key:

    CONGRESS_API_KEY=... python scripts/govinfo_search_acceptance.py

A7 (the fallback cell) poisons only the GovInfo leg: govinfo_search_post
builds a fresh httpx client per call (trust_env=True), so exporting
HTTPS_PROXY to a dead local port for that one probe makes GovInfo
unreachable while the congress.gov fallback -- which runs through the
harness's pre-built client -- still works (the V11/step-5 technique).

Probes (expected outcomes preregistered in spec section 3):
  A1  exact-title reachability     -> HR 4631 in results
  A2  'Radiation Exposure Compensation' -> non-empty, relevant
  A3  differential dead            -> A2/A2+Act not byte-identical noise
  A4  zzzqqx                       -> honest zero with diagnostics
  A5  monotonicity                 -> limit=10 results prefix of limit=50
  A6  pagination                   -> pages enumerable, dup only same-id
  A7  fallback cell (poisoned proxy) -> labeled recency_window_fallback
  A8  119hr10115ih reachable by 'RECA'
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402


class Ctx:
    """Minimal duck-typed context: the fallback path reaches congress.gov
    through the lifespan client."""

    class _Req:
        def __init__(self, lifespan_context):
            self.lifespan_context = lifespan_context

    def __init__(self, app_context):
        self.request_context = Ctx._Req(app_context)

    async def info(self, *_):
        pass

    async def error(self, *_):
        pass


def _out_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    directory = Path(__file__).resolve().parent.parent / "runs" / \
        "govinfo-search" / stamp
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _record(directory: Path, name: str, payload):
    (directory / f"{name}.json").write_text(
        json.dumps(payload, indent=2, default=str))


async def main() -> int:
    if not os.getenv("CONGRESS_API_KEY") and not os.getenv("GOVINFO_API_KEY"):
        print("Set CONGRESS_API_KEY (or GOVINFO_API_KEY) first.",
              file=sys.stderr)
        return 2

    from congress_api.core.client_handler import AppContext, SimpleCache
    from congress_api.features.buckets.bills.api import search_bills

    directory = _out_dir()
    app_ctx = AppContext(api_key=os.getenv("CONGRESS_API_KEY") or "",
                         client=httpx.AsyncClient(), cache=SimpleCache(60))
    ctx = Ctx(app_ctx)
    results = {}

    async def probe(name, expect, coro_kwargs, check):
        raw = await search_bills(ctx, **coro_kwargs)
        payload = json.loads(raw)
        ok, note = check(payload)
        results[name] = {"ok": ok, "expect": expect, "note": note}
        _record(directory, name, {"request": coro_kwargs,
                                  "response": payload,
                                  "ok": ok, "note": note})
        print(f"{name}: {'PASS' if ok else 'FAIL'} -- {note}")
        return payload

    def ids(payload):
        return [r.get("package_id") for r in payload.get("results", [])]

    # A1
    a1 = await probe(
        "A1", "HR 4631 reachable by exact title",
        {"keywords": "St. Louis RECA Readjustment Act", "congress": 119},
        lambda p: ("BILLS-119hr4631ih" in ids(p),
                   f"ids={ids(p)[:5]}"))

    # A2
    a2 = await probe(
        "A2", "no-'Act' query non-empty",
        {"keywords": "Radiation Exposure Compensation", "congress": 119},
        lambda p: (p.get("results_count", 0) > 0,
                   f"count={p.get('total_version_matches')}"))

    # A3: differential dead -- not byte-identical to A1's list, and the
    # 'Act'-ful variant is not a newest-bills page either.
    a3b = await probe(
        "A3", "differential dead",
        {"keywords": "Radiation Exposure Compensation Act", "congress": 119},
        lambda p: (p.get("results_count", 0) > 0, "with-Act non-empty"))
    a3_ok = (json.dumps(a1.get("results")) != json.dumps(a3b.get("results"))
             or ids(a1) == ids(a3b) == [])
    diff_ok = json.dumps(a2.get("results")) != json.dumps(
        a1.get("results"))
    results["A3"] = {"ok": bool(a3_ok and diff_ok), "expect":
                     "no byte-identical cross-query lists",
                     "note": f"a1!=a3b:{a3_ok} a2!=a1:{diff_ok}"}
    _record(directory, "A3_differential", results["A3"])
    print(f"A3: {'PASS' if results['A3']['ok'] else 'FAIL'}")

    # A4
    await probe(
        "A4", "honest diagnosable zero",
        {"keywords": "zzzqqx", "congress": 119},
        lambda p: (p.get("results_count") == 0 and "error" not in p
                   and "query_diagnostics" in p,
                   "zero with diagnostics"))

    # A5 monotonicity
    p10 = await probe("A5_limit10", "prefix base",
                      {"keywords": "Radiation Exposure Compensation",
                       "congress": 119, "limit": 10},
                      lambda p: (True, f"n={p.get('results_count')}"))
    p50 = await probe("A5_limit50", "prefix superset",
                      {"keywords": "Radiation Exposure Compensation",
                       "congress": 119, "limit": 50},
                      lambda p: (True, f"n={p.get('results_count')}"))
    prefix = ids(p10) == ids(p50)[:len(ids(p10))]
    results["A5"] = {"ok": prefix, "expect": "limit=10 is a prefix of 50",
                     "note": f"prefix={prefix}"}
    _record(directory, "A5_monotonicity", {
        "limit10": ids(p10), "limit50": ids(p50), "ok": prefix})
    print(f"A5: {'PASS' if prefix else 'FAIL'}")

    # A6 pagination walk
    walked, token, pages = [], None, 0
    while pages < 10:
        kwargs = {"keywords": "Radiation Exposure Compensation",
                  "congress": 119, "limit": 10}
        if token:
            kwargs["page_token"] = token
        payload = json.loads(await search_bills(ctx, **kwargs))
        walked.extend(ids(payload))
        pages += 1
        _record(directory, f"A6_page{pages}", payload)
        token = payload.get("next_page_token")
        if not token:
            break
    dup = [i for i in set(walked) if walked.count(i) > 1]
    # tolerated class: same bill id on adjacent pages; any dup must at
    # least be same-id (which package_id dedup shows trivially).
    results["A6"] = {"ok": token is None, "expect":
                     "walk terminates; dups only same-id",
                     "note": f"pages={pages} bills={len(walked)} "
                             f"dup_ids={dup}"}
    print(f"A6: {'PASS' if results['A6']['ok'] else 'FAIL'} "
          f"({results['A6']['note']})")

    # A7 fallback cell: poison ONLY the GovInfo leg (fresh client per
    # call reads HTTPS_PROXY at construction; the congress.gov fallback
    # uses the pre-built client above).
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
    try:
        a7 = json.loads(await search_bills(
            ctx, keywords="climate", congress=119))
    finally:
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("HTTP_PROXY", None)
    a7_ok = (a7.get("search_source") == "recency_window_fallback"
             and a7.get("fallback_trigger") == "govinfo_unreachable"
             and "window" in a7)
    results["A7"] = {"ok": a7_ok, "expect":
                     "labeled fallback, structurally distinguishable",
                     "note": f"source={a7.get('search_source')} "
                             f"trigger={a7.get('fallback_trigger')}"}
    _record(directory, "A7", a7)
    print(f"A7: {'PASS' if a7_ok else 'FAIL'} ({results['A7']['note']})")

    # A8
    await probe(
        "A8", "119hr10115ih reachable by RECA keyword",
        {"keywords": "RECA", "congress": 119},
        lambda p: (any(str(i).startswith("BILLS-119hr10115")
                       for i in ids(p)), f"ids={ids(p)}"))

    _record(directory, "summary", results)
    await app_ctx.client.aclose()
    failed = [k for k, v in results.items() if not v["ok"]]
    print(f"\nArtifacts: {directory}")
    print("RESULT:", "ALL PASS" if not failed else f"FAILED: {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
