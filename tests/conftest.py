"""Shared test configuration.

One fixture, and it is here purely because the suite was paying for the same CA
bundle over and over.
"""

from __future__ import annotations

import ssl

import pytest


@pytest.fixture(autouse=True, scope="session")
def _reuse_default_ssl_contexts():
    """Build each distinct default SSL context once per session instead of per client.

    ``ssl.create_default_context()`` re-parses the entire bundle it is handed on
    every call -- measured here at **0.31s per call** with certifi's bundle, and
    there is no memoisation in the stdlib. ``httpx`` calls it eagerly for every
    client it constructs, ``http://`` clients included, so a test that builds
    three clients (the app under test plus a couple of stubs) paid ~0.95s before
    doing any work of its own. That was the bulk of the suite's wall clock: with
    ~220 tests above 5ms, the fixed cost dominated everything.

    Safe for this suite: nothing in ``tests/`` or ``src/`` mutates an SSL
    context (checked before adding this), and ``httpx`` only reads from the one
    it is given. The cache is keyed on the call arguments, so a caller asking for
    a different bundle still gets a context of its own. Verification semantics are
    unchanged -- the same certifi bundle is loaded, just not hundreds of times.
    """

    original = ssl.create_default_context
    cache: dict[tuple, ssl.SSLContext] = {}

    def cached(*args, **kwargs):
        try:
            key = (args, tuple(sorted(kwargs.items())))
        except TypeError:  # an unhashable argument (a list for ``cadata``)
            return original(*args, **kwargs)
        if key not in cache:
            cache[key] = original(*args, **kwargs)
        return cache[key]

    ssl.create_default_context = cached
    try:
        yield
    finally:
        ssl.create_default_context = original
