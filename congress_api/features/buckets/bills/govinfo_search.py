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
