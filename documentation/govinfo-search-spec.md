# `search_bills` on GovInfo `/search` — spec

**Status: DRAFT — mandate settled 2026-08-24 (maintainer); design OPEN, measurements owed before any design question freezes.** Branch `feature/govinfo-search`. Defects this work closes: **D17 and D18** (`tool-defect-register.md` — read their joint entry first; the differential probe table there is the diagnostic record). The conventions in `fulltext/00-INDEX.md` ("Conventions — these bind") apply here unchanged, including one-line-per-paragraph formatting and measurement-over-assertion — **nothing about GovInfo `/search`'s behavior is settled in this file until a probe has measured it.**

## §1 Mandate — settled 2026-08-24, maintainer

- Replace `search_bills`' recency-window filter with **GovInfo `/search` full-text search restricted to the BILLS collection** as the primary path. #66's honest-window behavior (250-row page, window named in titles, honest miss message) only made the window honest; it is still blind to old bills and name-noise.
- **#66's honest-window behavior is retained as the fallback** when GovInfo is down or unreachable.
- **Reuse the existing keyed GovInfo client** — `X-Api-Key` header + backoff (never key-in-query; `fulltext/09-safety.md` §11 hygiene inherited).
- **Compose with the PR-2 cache**: a search hit can warm straight into `search_bill_text`. (Presumption, pending Q8: the hit *carries the identifiers* a follow-up call needs; no speculative prefetch.)
- Prior art pointer from the maintainer's fix-direction ruling (2026-08-20, D17/D18 entry): `search_bill_text`'s `query_diagnostics` discipline — a zero must be readable — applies at corpus level.

## §2 Constraints carried in from the record — binding

- **The D17/D18 masking constraint.** Satisfied by construction here (the corpus search replaces the window and the matcher in one move), but binding if scope ever shrinks: **no matcher-only intermediate ships.**
- **Matching semantics go in the tool description** — the rule measured into `fulltext/07-search.md`: descriptions shape input; D17's root harm was matching semantics the caller was never told.
- **Three zeros, never one string.** "No match in the corpus," "no match in the fallback window," and "the search errored" are three different responses. A `/search` failure must surface as the §9 envelope or the labeled fallback — never as an empty result set. This is the scan-that-errors rule bound onto the tool itself.
- **§9 error envelope, server-wide** (#68): new code paths emit `error.code`/`message`/`detail`/`remediation`; F22 URL-stripping applies to any GovInfo URL placed in `detail`; keyless is `api_key_missing`-shaped per the F31 contract, never `govinfo_key_rejected`.
- **#65 coherence:** `results_count` equals the length of the typed list it counts, in every response shape including the fallback.
- **Quota:** GovInfo's 36,000/hr bucket is **shared with bill-text content fetches** — searching must not starve retrieval. M3 measures per-call cost; a budget note lands in §4 of this file when it reports.
- **No new dependencies.**

## §3 Preregistered acceptance floor — from the D17/D18 record

Each probe archives artifacts (`runs/` convention) and asserts a non-zero denominator. A1–A4 are the register's regression probes; A5–A8 kill D18's structural consequences and the real-use failure.

- **A1:** `search_bills(congress=119, keywords="St. Louis RECA Readjustment Act")` returns HR 4631 — the bill unreachable by its own exact title today.
- **A2:** `Radiation Exposure Compensation` (no "Act") returns relevant bills, non-empty — the query that returns 0 today.
- **A3:** the D17 differential is dead: the two RECA queries must not return byte-identical newest-bill lists, and dropping `Act` must not zero a query whose other terms match.
- **A4:** `zzzqqx` → honest zero with corpus-level diagnostics, not an error and not noise.
- **A5:** monotonicity — the result set at `limit=10` is a prefix of the ranking at `limit=50` for the same query (D18's non-monotonicity dead).
- **A6:** `offset` pages the *result set*: matches enumerable across pages without duplication or loss (within upstream index stability across the run).
- **A7:** fallback cell — GovInfo unreachable (poisoned proxy, per the V11/step-5 technique) → labeled honest-window response, distinguishable from a corpus zero by structured metadata, not prose.
- **A8:** the 2026-08-22 real-use failure: `119hr10115ih` reachable by title keywords — the case where a live research session had to route around `search_bills` entirely.

## §4 Open design questions

Routing per `documentation/CLAUDE.md`: **RULE HERE** = IR/technical judgment, decided in this file with rationale; **MAINTAINER** = requirements/product. Most of these are gated on §5's measurements — marked with the gating M.

- **Q1 — surface.** Same `search_bills` operation, signature-preserved, is the presumption (the mandate names the tool). MAINTAINER only if a parameter must break.
- **Q2 — query semantics** *(gated M1)*. What does GovInfo `/search` do with a multi-word query — phrase, AND, OR? Which syntax survives (quoted phrases, fielded terms, boolean)? RULE HERE after M1; whatever ships is stated in the description verbatim (§2).
- **Q3 — version multiplicity** *(gated M1)*. `/search` returns per-version packages (`fulltext/03-data-sources.md` §3: "returns all versions as separate packages"), so one bill can appear once per version. Dedup to bill level? Which version fronts the hit (presumption: `fulltext` §3 precedence — most authoritative)? RULE HERE. **Note:** this seam may partially discharge the open version-discovery surface requirement (`fulltext/03-data-sources.md`) — the same response could carry the version list affirmatively instead of via error. Flag to MAINTAINER when the shape is concrete.
- **Q4 — pagination mapping** *(gated M2)*. Map `offset`/`limit` onto GovInfo's pagination so A5/A6 hold. RULE HERE.
- **Q5 — filter composition** *(gated M1)*. How `search_bills`' existing filters (congress, bill type, policy area) compose with full-text: fielded query upstream vs client-side post-filter. **Presumption against client-side post-filtering** — filtering a fetched page is the D18 shape reborn. RULE HERE.
- **Q6 — response shape.** Keep the existing `search_bills` response model; add source disclosure — a machine-readable marker distinguishing corpus results from fallback-window results, plus the D17/D18 honesty metadata (`bills_scanned`, oldest `updateDate`, `window_truncated`) whenever the fallback fires. #65 coherence binds. RULE HERE.
- **Q7 — fallback trigger.** Which failures demote to the honest window (5xx/network: yes, that is the mandate's "GovInfo-down"; rate-limited: probably yes with the note; keyless: **no** — keyless is an `api_key_missing` error per F31, not a silent downgrade, because the operator must act). RULE HERE; the keyless call is already constrained by the F31 contract.
- **Q8 — cache composition.** Presumption recorded in §1: the hit carries package/version identifiers; no prefetch (quota). Anything more speculative is a MAINTAINER feature call, unrequested so far.
- **Q9 — ranking** *(gated M1/M2)*. Relevance vs recency default, and what GovInfo's sort options actually are. RULE HERE.

## §5 Measurements owed before design freeze

Keyed probes against the live `/search` endpoint; artifacts archived beside this file (`runs/govinfo-search/`); every scan asserts its denominator; any detector built for these gets planted positives and negatives before its figure is trusted (`fulltext/00-INDEX.md`). Probes may run from either session — what lands here is the artifact, not the report.

- **M1 — query semantics.** Multi-word behavior (the RECA differential set is the natural probe corpus — known-correct answers exist), quoted-phrase support, fielded terms (`collection:`, `congress:`, `billtype:`, `docnumber:` — the §3 fallback line documents this form for resolution; measure whether it holds for general search), result-record fields, per-version multiplicity on a bill with many versions (S. 1071 has four).
- **M2 — pagination.** Page/offset mechanics, page-size caps, stability of ordering across pages.
- **M3 — quota cost.** Requests consumed per search call; confirm the bucket is the shared 36,000/hr GovInfo bucket (measured independent of congress.gov in V12 — reconfirm search draws from the same bucket rather than a separate service).
- **M4 — failure shapes.** What a GovInfo `/search` outage/5xx/timeout actually returns, so Q7's trigger list is enumerated from observation, not imagination.
