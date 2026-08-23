"""Rendering-version tripwire (spec §10 tail, F26 pattern).

The cache key is SCHEMA_VERSION. §10's rule: bump it for ANY change that alters
what a cached index serves -- not only the table schema but segment joining,
delimiter rendering, the header separator, and the byte-split boundary rules
(F12 reordered results on a whitespace-only change). A comment is not a guard;
this test is. index.rendering_fingerprint() hashes the AST of those symbols and
cache.RENDERING_FINGERPRINT pins it beside SCHEMA_VERSION, so changing any of
them without deliberately updating the pair fails CI.

Run with: pytest tests/test_bill_text_rendering_tripwire.py
"""

from __future__ import annotations

import ast
import textwrap

import congress_api.features.bill_text.parser as parser_mod
from congress_api.features.bill_text import cache, index


def test_rendering_fingerprint_is_pinned_beside_schema_version():
    actual = index.rendering_fingerprint()
    assert actual == cache.RENDERING_FINGERPRINT, (
        "A rendering-determining symbol changed (see index.RENDERING_SYMBOLS). "
        "A cached index built before this change would serve different chunks/"
        "rendering under the same key. If the change is deliberate: bump "
        f"cache.SCHEMA_VERSION (currently {cache.SCHEMA_VERSION}) AND set "
        f"cache.RENDERING_FINGERPRINT = {actual!r} in the same commit."
    )


def test_fingerprint_is_deterministic():
    assert index.rendering_fingerprint() == index.rendering_fingerprint()


def test_fingerprint_is_sensitive_to_a_rendering_constant(monkeypatch):
    # Non-vacuity: the pin must actually move when a covered symbol changes.
    before = index.rendering_fingerprint()
    monkeypatch.setattr(parser_mod, "MAX_UNIT_BYTES", parser_mod.MAX_UNIT_BYTES + 1)
    assert index.rendering_fingerprint() != before
    monkeypatch.undo()
    monkeypatch.setattr(parser_mod, "_QUOTE_OPEN", "<<")
    assert index.rendering_fingerprint() != before
    monkeypatch.undo()
    monkeypatch.setattr(index, "FTS_TOKENIZER", "unicode61")
    assert index.rendering_fingerprint() != before


def test_every_covered_symbol_resolves():
    # A renamed symbol must fail loudly here, not silently drop out of the hash.
    for module_name, dotted in index.RENDERING_SYMBOLS:
        assert index._symbol_digest_input(module_name, dotted)


def test_comments_and_docstrings_do_not_trip_it():
    # The digest is over the AST with docstrings stripped: formatting, comments
    # and prose edits are free; a logic change is not.
    a = textwrap.dedent('''
        def f(x):
            """Doc A."""
            # comment A
            return x + 1
    ''')
    b = textwrap.dedent('''
        def f(x):
            """Doc B, rewritten."""

            # a different comment, and blank lines
            return x+1
    ''')
    c = textwrap.dedent('''
        def f(x):
            """Doc A."""
            return x + 2
    ''')
    dump = lambda src: ast.dump(index._strip_docstrings(ast.parse(src)), include_attributes=False)  # noqa: E731
    assert dump(a) == dump(b)
    assert dump(a) != dump(c)


def test_named_rendering_areas_are_all_covered():
    # The four §10 names, by the symbols that implement them, plus the storage
    # definitions. Dropping one from RENDERING_SYMBOLS is a silent narrowing.
    covered = {dotted for _, dotted in index.RENDERING_SYMBOLS}
    assert {"join_segments", "render_segments", "_QUOTE_OPEN", "_QUOTE_CLOSE", "_ATTACH_PUNCT"} <= covered  # joining + delimiters + header separator
    assert {"collapse_ws", "normalize_text", "_tighten_punct"} <= covered  # stored-text normalization
    assert {"byte_split_unit", "_segment_atoms", "_bound_atom", "_chunk_unit", "MAX_UNIT_BYTES"} <= covered  # byte-split boundaries
    assert {"FTS_TOKENIZER", "_SCHEMA_SQL", "BillTextIndex._build", "load_parsed"} <= covered  # storage
