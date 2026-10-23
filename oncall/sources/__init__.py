"""Pick the data source for this process: live, fake (fixtures), bundle replay, or live+record."""
from oncall import config
from oncall.sources.bundle import BundleSource, RecordingSource
from oncall.sources.fake import FakeSource
from oncall.sources.guard import ForbiddenAccess
from oncall.sources.live import LiveSource, SourceError

_source = None


def get_source():
    global _source
    if _source is None:
        set_source(config.SOURCE)
    return _source


def set_source(kind="live", bundle_dir=None, record_to=None, fixture_dir=None):
    """kind: "live" | "fake" | "bundle". record_to: also save live answers into this bundle dir."""
    global _source
    if kind == "fake":
        _source = FakeSource(fixture_dir or config.FIXTURE_DIR)
    elif kind == "bundle":
        _source = BundleSource(bundle_dir or config.BUNDLE_DIR)
    else:
        _source = LiveSource()
        if record_to:
            _source = RecordingSource(_source, record_to)
    return _source


__all__ = ["get_source", "set_source", "SourceError", "ForbiddenAccess"]
