import datetime as dt
import json

from google.api_core import exceptions as gcs_exc
import pytest

import casita
from casita import cloud_sync


class FakeBlob:
    def __init__(self, name):
        self.name = name
        self.deleted = False
        self.uploads = []

    def delete(self, if_generation_match=None):
        self.deleted = if_generation_match

    def upload_from_string(self, payload, content_type=None):
        self.uploads.append((payload, content_type))


class ConflictBlob(FakeBlob):
    def delete(self, if_generation_match=None):
        raise gcs_exc.PreconditionFailed("generation changed")


class FakeBucket:
    def __init__(self, blob_cls=FakeBlob):
        self.blob_cls = blob_cls
        self.blobs = {}

    def blob(self, name):
        return self.blobs.setdefault(name, self.blob_cls(name))


class FakeClient:
    def __init__(self, bucket):
        self.bucket_obj = bucket
        self.bucket_names = []

    def bucket(self, name):
        self.bucket_names.append(name)
        return self.bucket_obj


def test_bucket_uses_current_environment(monkeypatch):
    bucket = FakeBucket()
    client = FakeClient(bucket)
    monkeypatch.setenv("CASITA_GCS_BUCKET", "casita-public-test")
    monkeypatch.setattr(cloud_sync, "_client", lambda: client)

    assert cloud_sync._bucket() is bucket
    assert client.bucket_names == ["casita-public-test"]


def test_bucket_requires_bucket_name(monkeypatch):
    monkeypatch.delenv("CASITA_GCS_BUCKET", raising=False)

    with pytest.raises(RuntimeError, match="CASITA_GCS_BUCKET"):
        cloud_sync._bucket()


def test_publish_pending_object_is_configurable(monkeypatch):
    bucket = FakeBucket()
    client = FakeClient(bucket)
    monkeypatch.setenv("CASITA_GCS_BUCKET", "casita-public-test")
    monkeypatch.setenv("CASITA_GCS_PUBLISH_PENDING_OBJECT", "custom/pending.json")
    monkeypatch.setattr(cloud_sync, "_client", lambda: client)

    cloud_sync.set_publish_pending()

    blob = bucket.blobs["custom/pending.json"]
    assert blob.uploads
    assert blob.uploads[0][1] == "application/json"


def test_clear_publish_pending_ignores_generation_conflict(monkeypatch):
    bucket = FakeBucket(ConflictBlob)
    client = FakeClient(bucket)
    monkeypatch.setenv("CASITA_GCS_BUCKET", "casita-public-test")
    monkeypatch.setattr(cloud_sync, "_client", lambda: client)

    cloud_sync.clear_publish_pending(123)


def test_publisher_tick_fails_fast_without_project(monkeypatch):
    monkeypatch.delenv("CASITA_FIREBASE_PROJECT", raising=False)
    body = json.dumps({
        "ts": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5))
        .isoformat()
        .replace("+00:00", "Z")
    })
    monkeypatch.setattr(cloud_sync, "read_publish_pending", lambda: (body, 123))

    with pytest.raises(SystemExit) as exc:
        casita.publisher.commands["tick"].callback(debounce=0)

    assert exc.value.code == 1
