"""
tests/test_package_imports.py

Covers blocker #1 (round 3): rename signal → sigeng to avoid Python stdlib
signal module collision.

Verifies:
  - sigeng package imports cleanly (no stdlib collision)
  - All expected names are exported
  - The stdlib signal module is still importable (no shadowing)
"""


class TestSigengImports:
    def test_sigeng_importable(self):
        """sigeng package must import without error."""
        import sigeng  # noqa: F401

    def test_sigeng_exports_signal_engine(self):
        from sigeng import SignalEngine
        assert callable(SignalEngine)

    def test_sigeng_exports_window_signal(self):
        from sigeng import WindowSignal
        assert WindowSignal is not None

    def test_sigeng_exports_signal_direction(self):
        from sigeng import SignalDirection
        assert SignalDirection.YES.value == "YES"
        assert SignalDirection.NO.value == "NO"
        assert SignalDirection.NONE.value == "NONE"

    def test_sigeng_exports_feed_window(self):
        from sigeng import FeedWindow
        assert FeedWindow is not None

    def test_stdlib_signal_still_importable(self):
        """stdlib signal module must not be shadowed by local sigeng package."""
        import signal as stdlib_signal
        # SIGINT is a standard signal — must be present in stdlib signal module
        assert hasattr(stdlib_signal, "SIGINT"), (
            "stdlib signal module is broken — local package may be shadowing it"
        )

    def test_sigeng_engine_importable_directly(self):
        from sigeng.engine import SignalEngine, FeedWindow, SignalDirection, WindowSignal
        assert SignalEngine is not None
        assert FeedWindow is not None

    def test_no_signal_package_conflict(self):
        """
        Regression: importing sigeng must NOT accidentally expose stdlib signal
        attributes through the sigeng namespace.
        """
        import sigeng
        # sigeng must not have SIGINT (that's stdlib signal)
        assert not hasattr(sigeng, "SIGINT"), (
            "sigeng namespace has SIGINT — stdlib signal module is being imported "
            "under the sigeng name, which means the rename did not take effect."
        )
