# `search_bills` on GovInfo `/search` — spec

**Status: DRAFT — mandate settled 2026-08-24 (maintainer); upstream interface documented 2026-08-24 (§2a); design questions partly settled on documentation, probe confirmation owed before freeze.** Branch `feature/govinfo-search`. Defects this work closes: **D17 and D18** (`tool-defect-register.md` — read their joint entry first; the differential probe table there is the diagnostic record). The conventions in `fulltext/00-INDEX.md` ("Conventions — these bind") apply here unchanged, including one-line-per-paragraph formatting and measurement-over-assertion — **vendor documentation is a claim with better provenance than an assumption, not a measurement; anything marked "per docs" below still gets a confirming probe before acceptance runs.**

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

## §2a Upstream interface — per published documentation, 2026-08-24 (probe confirmation owed)

Sources, maintainer-supplied 2026-08-24: `govinfo.gov/features/search-service-overview` (service), `govinfo.gov/help/search-operators` (query language), `govinfo.gov/help/bills` (BILLS fields). The service is labeled **public preview** (launched 2023-08-15) — a stability caveat worth one line in the completion report when this ships.

- **Endpoint:** POST `https://api.govinfo.gov/search`. Requires an API key (`DEMO_KEY` exists for low-volume probing).
- **Request:** `query` (string, the full query language below), `pageSize` (max 1000), `offsetMark` (**cursor**: `*` on the first call, then the value the previous response returned), `sorts` (array of `{field, sortOrder}`; fields `score`/`publishdate`/`title`/`lastModified`; "sorting by score ascending is not supported"), `resultLevel` (`"package"` or `"default"` — default is a mix of granules and packages), `historical` (bool, default false, "include non-current documents").
- **Response:** `count` (total matches) plus records carrying `title`, `packageId`, `granuleId`, `lastModified`, `governmentAuthor`, `dateIssued`, `collectionCode`, `resultLink`, `dateIngested`, and a `download` link object.
- **Query language:** multiple words are an **implied AND** (documented verbatim: "spaces between words are treated as an implied 'and'"); `"quoted phrases"` match adjacent-and-in-order; `AND`/`OR`/`NOT`/`-` booleans; `?`/`*` wildcards; `adj`, `before/#`, `near/#` proximity; `field:value` fielded terms (no spaces around the colon); `field:range(a,b)` ranges. Quotes inside the JSON `query` are backslash-escaped.
- **BILLS fields:** `collection:bills`, `congress:`, `billtype:` (`hr s hjres sjres hconres sconres hres sres`), `docnumber:`, `billversion:`, `title:`, `shorttitle:`, `chamber:`, `member:`, `memberparty:`, `memberstate:`, `committee:`, `actiondate:`, `publishdate:`, `isprivate:`, `isappropriation:`, `uscodecitation:`, `statutecitation:`, `plawcitation:`. Package ids follow `BILLS-{congress}{billtype}{docnumber}{billversion}`.
- **Noted, out of scope here:** `uscodecitation:`/`plawcitation:`/`statutecitation:` are an upstream affordance adjacent to the `amends` work (F36's citation classes, queryable corpus-wide). Recorded so it is not rediscovered; not part of this fix.

**What the documentation changes:** implied-AND semantics are the *opposite* of D17's OR-substring pathology, so the upstream default is already the correct matcher; the design work is mostly honesty plumbing and pagination. The load-bearing unknowns left for probes: tokenization of punctuated terms (`St.` in A1's query), what `historical` means for bill *versions* (it may do Q3's dedup upstream), granule-vs-package behavior for BILLS under each `resultLevel`, and cursor stability.

## §3 Preregistered acceptance floor — from the D17/D18 record

Each probe archives artifacts (`runs/` convention) and asserts a non-zero denominator. A1–A4 are the register's regression probes; A5–A8 kill D18's structural consequences and the real-use failure.

- **A1:** `search_bills(congress=119, keywords="St. Louis RECA Readjustment Act")` returns HR 4631 — the bill unreachable by its own exact title today.
- **A2:** `Radiation Exposure Compensation` (no "Act") returns relevant bills, non-empty — the query that returns 0 today.
- **A3:** the D17 differential is dead: the two RECA queries must not return byte-identical newest-bill lists, and dropping `Act` must not zero a query whose other terms match.
- **A4:** `zzzqqx` → honest zero with corpus-level diagnostics, not an error and not noise.
- **A5:** monotonicity — the result set at `limit=10` is a prefix of the ranking at `limit=50` for the same query (D18's non-monotonicity dead).
- **A6:** pagination pages the *result set*: matches enumerable across pages without duplication or loss (within upstream index stability across the run). *Restated from "offset pages the result set" — the upstream is cursor-based (§2a), and the surface question is Q4.*
- **A7:** fallback cell — GovInfo unreachable (poisoned proxy, per the V11/step-5 technique) → labeled honest-window response, distinguishable from a corpus zero by structured metadata, not prose.
- **A8:** the 2026-08-22 real-use failure: `119hr10115ih` reachable by title keywords — the case where a live research session had to route around `search_bills` entirely.

Under §2a's documented semantics, A1–A4 all have *predicted* outcomes (implied AND over title words → HR 4631 reachable; AND over three real terms → non-empty; the differential structurally impossible; `zzzqqx` → `count: 0`). The predictions are preregistered by this paragraph: if a probe falsifies one, that is a finding about the documentation, and it gets recorded here either way.

## §4 Open design questions

Routing per `documentation/CLAUDE.md`: **RULE HERE** = IR/technical judgment, decided in this file with rationale; **MAINTAINER** = requirements/product. Gating measurements marked.

- **Q1 — surface.** Same `search_bills` operation, signature-preserved, is the presumption (the mandate names the tool). MAINTAINER only if a parameter must break — **Q4 now invokes exactly this carve-out.**
- **Q2 — query semantics. PROVISIONALLY SETTLED per §2a docs; confirmation is M1.** Default is implied AND; quoted phrases, booleans, wildcards, proximity, and fielded terms exist. Ruling contingent on confirmation: **pass the caller's `keywords` through to `query` unmodified** (so a caller can use quotes, booleans, and fields deliberately), with the server appending the scoping terms (`collection:bills`, `congress:{n}` when the param is set). The tool description states: words are ANDed; `"quotes"` make phrases; `OR`/`NOT` available — and warns that field syntax is passed through. One escape rule binds: the server must backslash-escape nothing on behalf of the caller except what JSON transport requires — no silent query rewriting (D17's lesson: semantics the caller is never told).
- **Q3 — version multiplicity** *(gated M1)*. `/search` returns per-version packages, so one bill can appear once per version. Two upstream affordances may do the work: `historical: false` (default) may already collapse to current versions, and `billversion:` can pin. Probe S. 1071 (four versions) under `historical` both ways, then RULE HERE: presumption — one hit per bill, fronted by the most authoritative version per `fulltext/03-data-sources.md` §3 precedence. **Note:** this seam may partially discharge the open version-discovery surface requirement (`fulltext` §3); flag to MAINTAINER when the shape is concrete.
- **Q4 — pagination surface. RESHAPED by §2a: upstream is a cursor (`offsetMark`), not an offset — random access does not exist.** D18 already established the shipped `offset` is incoherent (it pages the candidate window), so no consumer can be depending on correct offset behavior. Options: (a) replace `offset` with an opaque page-token parameter mirroring the cursor — honest, cheap, **breaking**; (b) keep `offset` and emulate by walking cursors server-side — non-breaking, cost linear in offset, and it re-manufactures random-access semantics upstream doesn't offer; (c) drop pagination and cap results. **Recommendation: (a)** — the parameter is broken today, so the break is nominal and the honesty is real. **MAINTAINER** (signature change, Q1's carve-out).
- **Q5 — filter composition** *(gated M1)*. `congress` and bill type map to fielded terms (`congress:`, `billtype:`) — upstream, settled. **Policy area has no GovInfo field** (§2a's list is exhaustive per the docs): options are dropping the filter for the corpus path, post-filtering the corpus result set (unlike D18's shape this filters *matches*, not a recency page — but it breaks `count` honesty and pagination), or keeping it fallback-only. Presumption: **drop it from the corpus path and say so in the description**; MAINTAINER confirms since it removes an advertised filter.
- **Q6 — response shape.** Keep the existing `search_bills` response model; add source disclosure — a machine-readable marker distinguishing corpus results from fallback-window results, plus the D17/D18 honesty metadata (`bills_scanned`, oldest `updateDate`, `window_truncated`) whenever the fallback fires. Corpus responses carry upstream `count` (total matches) beside the returned page so a truncated list is legible. #65 coherence binds: `results_count` counts the returned list, not upstream `count`. RULE HERE.
- **Q7 — fallback trigger.** Which failures demote to the honest window (5xx/network: yes, that is the mandate's "GovInfo-down"; rate-limited: probably yes with the note; keyless: **no** — keyless is an `api_key_missing` error per F31, not a silent downgrade, because the operator must act). RULE HERE after M4 enumerates the real failure shapes.
- **Q8 — cache composition.** Presumption recorded in §1: the hit carries package/version identifiers (upstream `packageId` decomposes per §2a's id structure); no prefetch (quota). Anything more speculative is a MAINTAINER feature call, unrequested so far.
- **Q9 — ranking. PROVISIONALLY RULED: default `sorts: [{score, DESC}]`** — relevance is the only default that serves a search tool, and it is documented as the score sort's only direction. Recency stays available to callers only if a sort parameter is surfaced (not planned; the fallback window is already recency-shaped). Confirmation rides M1.

## §5 Measurements owed before design freeze

Keyed probes against the live `/search` endpoint (`DEMO_KEY` acceptable for low-volume probing; the real key for anything rate-sensitive); artifacts archived beside this file (`runs/govinfo-search/`); every scan asserts its denominator; any detector built for these gets planted positives and negatives before its figure is trusted (`fulltext/00-INDEX.md`). Probes may run from either session — what lands here is the artifact, not the report. §2a converts these from exploration to **confirmation with preregistered expectations**; a divergence from the documentation is a finding, recorded either way.

- **M1 — query-language confirmation.** The §3 predictions (A1–A4 shapes) run raw against `/search`; plus punctuation tokenization (`St.` / `U.S.` in queries), fielded-term behavior for `congress:`/`billtype:`/`title:`/`shorttitle:`, `resultLevel` package-vs-default for BILLS, and the S. 1071 version-multiplicity probe under `historical: true` and `false` (Q3's gate).
- **M2 — pagination mechanics.** Cursor walk on a many-hit query: `offsetMark` progression, page-size cap honored, no duplication/loss across pages, cursor lifetime (does a cursor survive minutes-later reuse — bears on Q4(a)'s token semantics).
- **M3 — quota cost.** Requests consumed per search call; confirm `/search` draws from the same 36,000/hr GovInfo bucket as content fetches (V12 measured content vs congress.gov; search vs content is the open pair).
- **M4 — failure shapes.** What a GovInfo `/search` outage/5xx/timeout/oversize-query actually returns, so Q7's trigger list is enumerated from observation, not imagination.
