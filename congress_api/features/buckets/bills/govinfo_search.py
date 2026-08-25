"""GovInfo /search engine for search_bills (govinfo-search-spec section 6).

This module owns the corpus-search side of search_bills: request validation
and query assembly (section 6.1). Response mapping, pagination, and the
failure flow land in later steps of the same work order.

Assembly rules (all measured, section 2b of the spec):

- The caller's ``keywords`` pass through verbatim (outer whitespace
  stripped); the JSON transport escape is the only server-side
  transformation. Blank keywords are rejected client-side -- an empty
  upstream query matches the entire ~8.76M-document corpus with HTTP 200.
- Scoping terms are appended, never merged into the caller's text:
  ``collection:bills`` always, ``congress:{n}`` / ``billtype:{t}`` when set.
  ``congress`` is validated as a positive integer HERE because a non-numeric
  value 500s upstream with the outage-shaped body; ``billtype`` is validated
  against the eight documented values.
- ``sorts: [{score, DESC}]`` is always explicit: the upstream default sort
  is NOT relevance (measured -- package-id-shaped ordering without it).
- ``pageSize = min(3 * limit, 300)``: overfetch absorbs bill-level dedup
  shrinkage (most bills carry 1-4 version records).
- ``resultLevel`` and ``historical`` are omitted: both are measured no-ops
  for the BILLS collection. Nothing is sent that does nothing.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ....core.exceptions import (
    APIErrorResponse,
    CommonErrors,
    CongressionalAPIError,
    ErrorType,
)
from ....core.validators import ParameterValidator

logger = logging.getLogger(__name__)

# The eight documented GovInfo billtype: values (govinfo.gov/help/bills).
BILL_TYPES = ("hr", "s", "hjres", "sjres", "hconres", "sconres", "hres", "sres")

# pageSize policy (spec section 6.3): one upstream fetch per tool call.
OVERFETCH_FACTOR = 3
MAX_PAGE_SIZE = 300

# Q9: every request carries this explicitly; there is no caller-facing sort.
SCORE_SORTS = ({"field": "score", "sortOrder": "DESC"},)


def _reject(error: APIErrorResponse) -> None:
    raise CongressionalAPIError(error)


def validate_keywords(keywords: Optional[str]) -> str:
    """Section 6.1: required; stripped; blank -> invalid_parameters, never
    sent upstream. Otherwise returned verbatim (outer whitespace only)."""
    stripped = str(keywords).strip() if keywords is not None else ""
    if not stripped:
        _reject(APIErrorResponse(
            error_type=ErrorType.VALIDATION,
            message=(
                "keywords is required and must contain at least one "
                "non-whitespace character."
            ),
            suggestions=[
                "Pass plain search words (they are ANDed by GovInfo), "
                "e.g. keywords='radiation exposure compensation'",
                "An empty query would match the entire GovInfo corpus "
                "(~8.76M documents), so it is rejected here and never sent",
            ],
            error_code="invalid_parameters",
            details={"parameter": "keywords",
                     "provided_value": repr(keywords)},
        ))
    return stripped


def validate_congress(congress: Any) -> Optional[int]:
    """Positive-integer guard, client-side: a non-numeric congress: term
    500s upstream wearing the outage body (spec section 2b item 3)."""
    if congress is None:
        return None
    if isinstance(congress, bool):
        _reject(CommonErrors.invalid_congress_number(congress))
    if isinstance(congress, float) and not congress.is_integer():
        # The shared validator would int()-truncate 12.5 to 12 and scope the
        # query to the wrong congress; a fractional value is not an integer.
        _reject(CommonErrors.invalid_congress_number(congress))
    result = ParameterValidator.validate_congress_number(congress)
    if not result.is_valid:
        _reject(CommonErrors.invalid_congress_number(congress))
    return result.sanitized_value


def validate_bill_type(bill_type: Optional[str]) -> Optional[str]:
    """Validated against the eight documented billtype: values."""
    if bill_type is None:
        return None
    result = ParameterValidator.validate_bill_type(bill_type)
    if not result.is_valid or result.sanitized_value not in BILL_TYPES:
        _reject(CommonErrors.invalid_bill_type(bill_type))
    return result.sanitized_value


def clamp_limit(limit: Any) -> "tuple[int, Optional[str]]":
    """Existing clamp discipline: out-of-range clamps with the shared
    validator's advisory wording; non-integers are rejected."""
    result = ParameterValidator.validate_limit_range(limit)
    if not result.is_valid:
        _reject(CommonErrors.invalid_parameter(
            "limit", limit, result.error_message))
    # On a clamp the validator reports valid + sanitized + advisory text.
    return result.sanitized_value, result.error_message


def build_query(keywords: str, congress: Optional[int],
                bill_type: Optional[str]) -> str:
    """``"{keywords} collection:bills[ congress:{n}][ billtype:{t}]"``.

    ``keywords`` must already be validated; scoping values must already be
    sanitized (this function assembles, it does not judge).
    """
    parts = [keywords, "collection:bills"]
    if congress is not None:
        parts.append(f"congress:{congress}")
    if bill_type is not None:
        parts.append(f"billtype:{bill_type}")
    return " ".join(parts)


def build_search_body(query: str, limit: int,
                      offset_mark: str = "*") -> "dict[str, Any]":
    """The exact upstream request body. ``resultLevel`` and ``historical``
    are deliberately absent (measured no-ops for BILLS, section 2b)."""
    return {
        "query": query,
        "pageSize": min(OVERFETCH_FACTOR * limit, MAX_PAGE_SIZE),
        "offsetMark": offset_mark,
        "sorts": [dict(sort) for sort in SCORE_SORTS],
    }


# ---------------------------------------------------------------------------
# Response mapping (spec section 6.2)
# ---------------------------------------------------------------------------

# Package ids follow BILLS-{congress}{billtype}{docnumber}{billversion}
# (section 2a). The billtype alternation is the eight documented values,
# longest-first so 'hres' can never be read as 'hr' + 'es...', and the
# version code must start with a letter (the shipped bill_text rule), so a
# longer docnumber can never bleed digits into the code.
_PACKAGE_ID_RE = None  # built lazily below to keep the alternation in one place


def _package_id_re():
    global _PACKAGE_ID_RE
    if _PACKAGE_ID_RE is None:
        import re
        from ...bill_text.client import VERSION_CODE_PATTERN
        types = "|".join(sorted(BILL_TYPES, key=len, reverse=True))
        _PACKAGE_ID_RE = re.compile(
            rf"^BILLS-(\d{{1,3}})({types})(\d+)({VERSION_CODE_PATTERN})$",
            re.IGNORECASE,
        )
    return _PACKAGE_ID_RE


def parse_package_id(package_id: Any) -> "Optional[tuple[int, str, int, str]]":
    """(congress, bill_type, number, version) parsed from a BILLS package
    id, or None when the id is not a well-formed BILLS id. The id is the
    authoritative value (carry, don't reconstruct): grouping and the hit's
    identity fields come from here, never re-derived from other fields."""
    match = _package_id_re().match(str(package_id or ""))
    if not match:
        return None
    congress, bill_type, number, version = match.groups()
    return int(congress), bill_type.lower(), int(number), version.lower()


def _version_sort_key(code: str, date: "Optional[str]") -> "tuple[int, str, str]":
    """Precedence-primary ordering, mirroring the shipped bill_text rule:
    the 53-code table ranks first, date (nullable, weaker) breaks ties
    within a tier, the code string makes the order total. Unknown codes get
    UNKNOWN_PRECEDENCE and lose to any known text stage; a missing date
    simply sorts last within its own tier instead of being special-cased."""
    from ...bill_text.client import UNKNOWN_PRECEDENCE, VERSION_PRECEDENCE
    return (VERSION_PRECEDENCE.get(code, UNKNOWN_PRECEDENCE), date or "", code)


def group_records(records: "list[dict[str, Any]]") \
        -> "tuple[list[dict[str, Any]], list[Any]]":
    """Bill-level dedup of upstream version-package records, in rank order.

    Returns (bills, skipped_package_ids). Bills are ordered by the rank of
    each bill's FIRST record (first occurrence wins -- the property A5's
    monotonicity rests on). Each bill dict fronts the most authoritative
    version among ITS fetched records and carries every fetched version in
    ``matched_versions``, precedence-ordered. Records whose packageId does
    not parse as a BILLS id are skipped from grouping (and reported) --
    they cannot be attributed to a bill without reconstructing identity
    from weaker fields, which the carry-don't-reconstruct rule forbids.
    """
    order: "list[tuple[int, str, int]]" = []
    by_bill: "dict[tuple[int, str, int], list[tuple[str, dict[str, Any]]]]" = {}
    skipped: "list[Any]" = []
    for record in records:
        if not isinstance(record, dict):
            skipped.append(record)
            continue
        parsed = parse_package_id(record.get("packageId"))
        if parsed is None:
            skipped.append(record.get("packageId"))
            continue
        congress, bill_type, number, version = parsed
        key = (congress, bill_type, number)
        if key not in by_bill:
            by_bill[key] = []
            order.append(key)
        by_bill[key].append((version, record))
    if skipped:
        logger.warning(
            "govinfo /search returned %d record(s) without a parseable "
            "BILLS packageId; they were skipped from bill grouping: %r",
            len(skipped), skipped[:5])

    bills = []
    for key in order:
        congress, bill_type, number = key
        versioned = sorted(
            by_bill[key],
            key=lambda pair: _version_sort_key(
                pair[0], str(pair[1].get("dateIssued") or "") or None),
            reverse=True,
        )
        front_version, front_record = versioned[0]
        seen = set()
        matched_versions = []
        for version, _ in versioned:
            if version not in seen:
                seen.add(version)
                matched_versions.append(version)
        bills.append({
            "bill": f"{bill_type.upper()} {number}",
            "congress": congress,
            "bill_type": bill_type,
            "bill_number": number,
            "title": front_record.get("title"),
            "package_id": front_record.get("packageId"),
            "version": front_version,
            "date_issued": front_record.get("dateIssued"),
            "matched_versions": matched_versions,
        })
    return bills, skipped


def zero_diagnostics(upstream_query: str, congress: Optional[int],
                     bill_type: Optional[str]) -> "dict[str, Any]":
    """Corpus-level readable zero (the search_bill_text query_diagnostics
    discipline applied at corpus level): says what corpus was searched,
    with which upstream query, and what to try -- so 'no match in the
    corpus' cannot be misread as noise or as an error."""
    return {
        "corpus": ("full text of congressional bills, all versions "
                   "(GovInfo BILLS collection)"),
        "upstream_query": upstream_query,
        "scope": {"congress": congress, "bill_type": bill_type},
        "note": (
            "0 version packages matched this query in the corpus. Words "
            "are ANDed. Quoted phrases are unreliable against bill titles "
            "-- retry with plain unquoted words, or use "
            'title:"..." / shorttitle:"..." for an exact title.'
        ),
    }


def build_corpus_response(
    bills: "list[dict[str, Any]]",
    total_version_matches: int,
    upstream_query: str,
    congress: Optional[int] = None,
    bill_type: Optional[str] = None,
    next_page_token: Optional[str] = None,
    request_note: Optional[str] = None,
) -> "dict[str, Any]":
    """The corpus-path response shape (section 6.2).

    results_count counts the returned list (#65), never the upstream
    total; total_version_matches is the upstream count under its honest
    name -- it counts version packages and can exceed the number of
    distinct bills. search_source is the machine-readable origin marker.
    """
    response: "dict[str, Any]" = {
        "search_source": "govinfo_fulltext",
        "results_count": len(bills),
        "total_version_matches": total_version_matches,
        "results": bills,
        "next_page_token": next_page_token,
    }
    if request_note:
        response["request_note"] = request_note
    if not bills:
        response["query_diagnostics"] = zero_diagnostics(
            upstream_query, congress, bill_type)
    return response
