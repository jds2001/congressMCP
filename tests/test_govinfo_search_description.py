"""Step 5 of the govinfo-search work order (spec section 6.5): the tool
description is load-bearing (D17's root harm was matching semantics the
caller was never told). Every mandatory item is pinned INDIVIDUALLY --
an enumeration whose members are not individually pinned is the
assumption the section-6.5 list exists to reject.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CONGRESS_API_KEY", "test-key-govinfo-search")


def _doc() -> str:
    """The docstring, whitespace-flattened: inspect.getdoc dedents and the
    source wraps at ~80 cols, so phrase pins match on the flattened text."""
    from congress_api.features.bills_tool import bills
    doc = inspect.getdoc(inspect.unwrap(bills))
    assert doc, "bills tool has no docstring"
    return " ".join(doc.split())


def test_item_1_names_the_corpus():
    doc = _doc()
    assert "full text of congressional bills" in doc
    assert "every version of every bill" in doc
    assert "GovInfo BILLS collection" in doc


def test_item_2_matching_semantics():
    doc = _doc()
    assert "words are ANDed" in doc
    assert "Do NOT quote bill names" in doc
    assert 'title:"..."' in doc and 'shorttitle:"..."' in doc
    assert "OR and NOT" in doc
    assert "pass through" in doc          # field operators
    assert "billversion:" in doc


def test_item_3_version_discovery():
    doc = _doc()
    assert "matched_versions" in doc
    # match-scoped by construction, not the complete set
    assert "not the bill's complete version set" in doc
    # the affirmative discovery path: fielded no-text query
    assert 'keywords="congress:119 billtype:s docnumber:1071"' in doc
    # the pinned-version error disclosure stays
    assert "version_not_available" in doc


def test_item_4_count_semantics():
    doc = _doc()
    assert "total_version_matches" in doc
    assert "version packages" in doc.lower()
    assert "can exceed the number of distinct bills" in doc
    assert "results_count counts the bills actually returned" in doc


def test_item_5_pagination():
    doc = _doc()
    assert "next_page_token back verbatim" in doc
    assert "run short" in doc
    assert "reappear" in doc and "same identity" in doc


def test_item_6_fallback():
    doc = _doc()
    assert "recency_window_fallback" in doc
    assert "fallback_trigger" in doc
    assert "not the corpus" in doc.lower()


def test_item_7_blank_keywords_rejected():
    doc = _doc()
    assert "blank or whitespace-only keywords are rejected" in doc


def test_removed_parameters_are_disclaimed():
    # The signature break is part of what shapes input: the description
    # says search_bills does not take the old window parameters.
    assert "does NOT take offset" in _doc()
