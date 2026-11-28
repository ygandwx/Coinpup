import hashlib
import io
import urllib.error
from types import SimpleNamespace

import pytest

from scripts import prepare_ocr_ubuntu_sources as prepare
from scripts.ocr_benchmark import engine_assets as assets

_PAYLOAD = b"fictional source descriptor"


class Response(io.BytesIO):
    def __init__(self, url, data):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


@pytest.fixture
def network(monkeypatch):
    def configure(original, *, status=404, error_url=None, data=_PAYLOAD, final=None):
        calls = []

        def archive(url, *, timeout):
            calls.append(url)
            if status == 200:
                return Response(url, data)
            raise urllib.error.HTTPError(error_url or url, status, "fixture", {}, None)

        def snapshot(url, *, timeout):
            calls.append(url)
            return Response(final or url, data)

        item = dict(
            url=original,
            name="fixture.dsc",
            byte_size=len(_PAYLOAD),
            sha256=hashlib.sha256(_PAYLOAD).hexdigest(),
        )
        monkeypatch.setattr(assets, "_manifest", lambda: {"files": [item]})
        monkeypatch.setattr(assets, "_opener", lambda: SimpleNamespace(open=archive))
        monkeypatch.setattr(
            prepare.urllib.request, "build_opener", lambda handler: SimpleNamespace(open=snapshot)
        )
        return calls

    return configure


@pytest.mark.parametrize("original,snapshot", prepare._SNAPSHOTS.items())
def test_only_exact_original_404_prefills_then_frozen_fetch_verifies(
    tmp_path, network, original, snapshot
):
    calls = network(original)
    prepare.prepare(tmp_path)
    prepare.prepare(tmp_path)
    assets.fetch_assets(tmp_path)
    assert calls == [original, snapshot]
    assert (tmp_path / "fixture.dsc").read_bytes() == _PAYLOAD


def test_other_sources_redirected_404_and_other_responses_have_no_fallback(tmp_path, network):
    original = next(iter(prepare._SNAPSHOTS))
    calls = network(original + "?different")
    prepare.prepare(tmp_path)
    assert calls == []
    calls = network(original, status=200)
    prepare.prepare(tmp_path)
    assert calls == [original]
    for status, url in [(403, original), (503, original), (404, original + "?redirected")]:
        calls = network(original, status=status, error_url=url)
        with pytest.raises(urllib.error.HTTPError):
            prepare.prepare(tmp_path)
        assert calls == [original]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "data,final",
    [
        (_PAYLOAD[:-1], None),
        (b"x" * len(_PAYLOAD), None),
        (_PAYLOAD, "https://snapshot.ubuntu.com/other"),
    ],
)
def test_bad_snapshot_never_publishes(tmp_path, network, data, final):
    original = next(iter(prepare._SNAPSHOTS))
    network(original, data=data, final=final)
    with pytest.raises(assets.EngineAssetError):
        prepare.prepare(tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert prepare._NoSnapshotRedirect().redirect_request(None, None, 302, "", {}, original) is None
