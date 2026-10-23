"""Pick the data source for this process.

    live    the running demo (Prometheus, Jaeger, docker CLI) - the default
    bundle  replay of a recorded live run (--record DIR while live)
    test    JSON fixtures under tests/fixtures/ - ONLY with an explicit --test flag

Every report records source_info() so a fixture run can never pass for a live one.
"""
from oncall import config
from oncall.sources.bundle import BundleSource, RecordingSource
from oncall.sources.fixtures import FixtureSource
from oncall.sources.guard import ForbiddenAccess
from oncall.sources.live import LiveSource, SourceError

_source = None


class TestModeRefused(RuntimeError):
    __test__ = False  # not a pytest test class


def get_source():
    global _source
    if _source is None:
        set_source(config.SOURCE)
    return _source


def set_source(kind="live", bundle_dir=None, record_to=None, fixture_dir=None, allow_test=False):
    """kind: "live" | "bundle" | "test". "test" also needs allow_test=True (the --test flag)."""
    global _source
    if kind == "test":
        if not allow_test:
            raise TestModeRefused("test mode reads fixtures, not the demo; pass --test explicitly")
        _source = FixtureSource(fixture_dir or config.FIXTURE_DIR)
    elif kind == "bundle":
        _source = BundleSource(bundle_dir or config.BUNDLE_DIR)
    elif kind == "live":
        _source = LiveSource()
        if record_to:
            _source = RecordingSource(_source, record_to)
    else:
        raise ValueError(f"unknown source {kind!r}; use live or bundle (or --test)")
    return _source


def source_info():
    src = get_source()
    return {"source": src.name, "source_detail": src.detail}


def add_source_args(parser):
    """Shared CLI flags. Test mode is only reachable through --test."""
    parser.add_argument("--source", choices=["live", "bundle"], default="live",
                        help="data source (default live)")
    parser.add_argument("--test", action="store_true",
                        help="use JSON fixtures under tests/fixtures/ instead of the demo")
    parser.add_argument("--fixtures", help="fixture directory under tests/fixtures/ (needs --test)")
    parser.add_argument("--bundle", help="bundle directory for --source bundle")
    parser.add_argument("--record", help="while live, also save answers into this bundle dir")


def apply_source_args(parser, args):
    if args.test:
        if args.source != "live" or args.record or args.bundle:
            parser.error("--test cannot be combined with --source bundle, --bundle or --record")
        return set_source("test", fixture_dir=args.fixtures, allow_test=True)
    if args.fixtures:
        parser.error("--fixtures needs --test")
    try:
        return set_source(args.source, bundle_dir=args.bundle, record_to=args.record)
    except TestModeRefused as e:
        parser.error(str(e))


__all__ = ["get_source", "set_source", "source_info", "add_source_args", "apply_source_args",
           "SourceError", "ForbiddenAccess", "TestModeRefused"]
