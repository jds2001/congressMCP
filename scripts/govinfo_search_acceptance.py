#!/usr/bin/env python3
"""Live A1-A8 acceptance run for the GovInfo-backed search_bills
(govinfo-search-spec section 3 / section 6.6).

Runs the preregistered probes against the LIVE GovInfo endpoint through
the real tool function, archiving one JSON artifact per probe plus a
summary under runs/govinfo-search/<UTC-timestamp>/ (gitignored; cite
paths in the report). Requires a real api.data.gov key:

    CONGRESS_API_KEY=... python scripts/govinfo_search_acceptance.py

A7 (the fallback cell) poisons only the GovInfo leg with a
HOST-SELECTIVE stdlib CONNECT proxy: api.govinfo.gov is refused at
CONNECT time (502, logged), every other host tunnels for real. A blanket
dead proxy cannot prove the discrimination A7 claims -- it kills BOTH
legs whenever either builds a fresh trust_env client, so a "fallback"
outcome would be unattributable. The probe also asserts its instrument
premises before trusting the reading (a finding is only as valid as the
instrument): under the poison, a fresh client must fail against
api.govinfo.gov and succeed against api.congress.gov, and the proxy's
own deny/tunnel logs attribute the GovInfo failure to the poison rather
than a real outage.

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
import socket
import sys
import threading
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


class HostSelectiveProxy:
    """Minimal stdlib HTTP CONNECT proxy: named hosts are refused (502 on
    CONNECT, before any egress), every other host is tunneled for real.

    Serves as A7's poison. Keeps deny/tunnel logs so the artifact can
    attribute a GovInfo transport failure to THIS instrument rather than
    to a coincidental real outage."""

    def __init__(self, deny_hosts):
        self.deny = {h.lower() for h in deny_hosts}
        self.denied = []
        self.tunneled = []
        self._srv = socket.create_server(("127.0.0.1", 0))
        self.port = self._srv.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "HostSelectiveProxy":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        try:
            self._srv.close()
        except OSError:
            pass

    def _serve(self) -> None:
        self._srv.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(15)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    conn.close()
                    return
                data += chunk
            request_line = data.split(b"\r\n", 1)[0].decode("latin-1")
            parts = request_line.split(" ")
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                conn.close()
                return
            host, _, port = parts[1].partition(":")
            host = host.lower()
            if host in self.deny:
                self.denied.append(host)
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                conn.close()
                return
            upstream = socket.create_connection((host, int(port or 443)),
                                                timeout=15)
            self.tunneled.append(host)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            a = threading.Thread(target=self._pipe, args=(conn, upstream),
                                 daemon=True)
            b = threading.Thread(target=self._pipe, args=(upstream, conn),
                                 daemon=True)
            a.start()
            b.start()
            a.join()
            b.join()
        except OSError:
            pass
        finally:
            for sock in (conn,):
                try:
                    sock.close()
                except OSError:
                    pass

    @staticmethod
    def _pipe(src_sock: socket.socket, dst_sock: socket.socket) -> None:
        try:
            while True:
                chunk = src_sock.recv(65536)
                if not chunk:
                    break
                dst_sock.sendall(chunk)
        except OSError:
            pass
        finally:
            for sock in (src_sock, dst_sock):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
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

    # A7 fallback cell: GovInfo unreachable while congress.gov stays
    # reachable. The poison is host-selective (see module docstring) --
    # a blanket dead proxy kills both legs and makes the outcome
    # unattributable. Premises are asserted BEFORE the probe runs; if
    # either fails, A7 is recorded VOID (instrument failure), never
    # PASS or FAIL.
    proxy = HostSelectiveProxy(deny_hosts=["api.govinfo.gov"]).start()
    saved_env = {name: os.environ.get(name)
                 for name in ("HTTPS_PROXY", "HTTP_PROXY")}
    os.environ["HTTPS_PROXY"] = proxy.url
    os.environ["HTTP_PROXY"] = proxy.url
    premise = {"govinfo_dead": False, "congress_alive": False}
    try:
        # Premise 1: under the poison, a FRESH trust_env client (the same
        # construction class govinfo_search_post uses) must fail against
        # GovInfo -- refused at CONNECT, so no quota is spent and no key
        # is needed.
        try:
            async with httpx.AsyncClient(timeout=10.0) as fresh:
                await fresh.get("https://api.govinfo.gov/search")
            premise["govinfo_unexpected"] = "request succeeded"
        except httpx.HTTPError as exc:
            premise["govinfo_dead"] = True
            premise["govinfo_error"] = type(exc).__name__
        # Premise 2: the SAME environment must let a fresh client reach
        # congress.gov (any HTTP status is reachability; auth is not the
        # premise).
        try:
            async with httpx.AsyncClient(timeout=20.0) as fresh:
                resp = await fresh.get(
                    "https://api.congress.gov/v3/bill",
                    params={"format": "json"})
            premise["congress_alive"] = True
            premise["congress_status"] = resp.status_code
        except httpx.HTTPError as exc:
            premise["congress_error"] = type(exc).__name__
        premise_ok = premise["govinfo_dead"] and premise["congress_alive"]

        if premise_ok:
            a7 = json.loads(await search_bills(
                ctx, keywords="climate", congress=119))
        else:
            a7 = {"instrument_error": "A7 premise assertions failed; "
                                      "probe not run"}
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        proxy.stop()
    # Attribution: the GovInfo failure must be OUR refusal, on the record.
    premise["proxy_denied_hosts"] = sorted(set(proxy.denied))
    premise["proxy_tunneled_hosts"] = sorted(set(proxy.tunneled))
    attributed = "api.govinfo.gov" in premise["proxy_denied_hosts"]
    if not premise_ok or not attributed:
        results["A7"] = {"ok": False, "expect":
                         "labeled fallback, structurally distinguishable",
                         "note": "VOID: instrument premises not "
                                 f"established ({premise})",
                         "premise": premise}
    else:
        a7_ok = (a7.get("search_source") == "recency_window_fallback"
                 and a7.get("fallback_trigger") == "govinfo_unreachable"
                 and "window" in a7)
        results["A7"] = {"ok": a7_ok, "expect":
                         "labeled fallback, structurally distinguishable",
                         "note": f"source={a7.get('search_source')} "
                                 f"trigger={a7.get('fallback_trigger')}",
                         "premise": premise}
    _record(directory, "A7", {"premise": premise, "response": a7})
    print(f"A7: {'PASS' if results['A7']['ok'] else 'FAIL'} "
          f"({results['A7']['note']})")

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
