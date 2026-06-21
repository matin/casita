"""GCS-canonical Casita DB.

Private deployments can mirror the SQLite DB to a GCS object. Every write
verb pulls a fresh copy, mutates locally, and pushes back with an
if-generation-match precondition. If the precondition fails, the caller can
pull once more and retry the verb.

Read-only verbs can use `with_db(read_only=True)` to skip the push.

The publisher daemon watches a second GCS object for a "something changed"
flag set after every successful push.
"""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from google.api_core import exceptions as gcs_exc
from google.cloud import storage as gcs

def _bucket_name() -> str | None:
    return os.environ.get("CASITA_GCS_BUCKET")


def _db_object() -> str:
    return os.environ.get("CASITA_GCS_DB_OBJECT", "casita/db.sqlite")


def _publish_pending_object() -> str:
    return os.environ.get(
        "CASITA_GCS_PUBLISH_PENDING_OBJECT",
        "casita/publish_pending.json",
    )


class ConflictError(RuntimeError):
    """The DB on GCS was rewritten between pull and push."""


def _client() -> gcs.Client:
    return gcs.Client()


def _bucket() -> gcs.Bucket:
    bucket_name = _bucket_name()
    if not bucket_name:
        raise RuntimeError("Set CASITA_GCS_BUCKET to use cloud-synced commands.")
    return _client().bucket(bucket_name)


def pull_db(dest: Path | None = None) -> tuple[Path, int]:
    """Download the canonical DB; return (local_path, generation).

    `generation` must be passed to push_db() to detect interleaved writes.
    """
    blob = _bucket().blob(_db_object())
    blob.reload()  # ensures .generation is populated
    if dest is None:
        fd, name = tempfile.mkstemp(prefix="casita-", suffix=".sqlite")
        os.close(fd)
        dest = Path(name)
    blob.download_to_filename(str(dest))
    return dest, blob.generation


def push_db(local_path: Path, generation: int) -> int:
    """Upload local DB iff GCS generation still matches; return new generation.

    Raises ConflictError if the precondition fails.
    """
    blob = _bucket().blob(_db_object())
    try:
        blob.upload_from_filename(
            str(local_path),
            content_type="application/octet-stream",
            if_generation_match=generation,
        )
    except gcs_exc.PreconditionFailed as e:
        # 412 from if_generation_match — another writer changed the DB between
        # our pull and push. Convert to ConflictError so _retry_canonical can
        # re-pull + re-apply. NB: the library raises google.api_core's
        # PreconditionFailed; google.cloud.storage.exceptions has no such name.
        raise ConflictError(
            f"DB generation changed between pull and push (had {generation})"
        ) from e
    return blob.generation


def set_publish_pending() -> None:
    """Mark the site as dirty so the publisher daemon redeploys."""
    payload = (
        '{"ts": "'
        + datetime.now(timezone.utc).isoformat()
        + '"}'
    )
    _bucket().blob(_publish_pending_object()).upload_from_string(
        payload, content_type="application/json"
    )


def clear_publish_pending(generation: int) -> None:
    """Clear the dirty flag iff it hasn't been re-set since we observed it."""
    blob = _bucket().blob(_publish_pending_object())
    try:
        blob.delete(if_generation_match=generation)
    except gcs_exc.PreconditionFailed:
        # Someone re-flagged between our read and delete — leave it set so
        # the next tick picks it up.
        pass


def read_publish_pending() -> tuple[str, int] | None:
    """Return (body, generation) if the flag is set; None otherwise."""
    blob = _bucket().blob(_publish_pending_object())
    if not blob.exists():
        return None
    blob.reload()
    body = blob.download_as_text()
    return body, blob.generation


@contextmanager
def with_db(*, read_only: bool = False, mark_publish: bool = True):
    """Pull the canonical DB, point storage at it, push back on success.

    Sets CASITA_DB_PATH to a temp file for the duration so storage.connect()
    operates on the pulled DB. On clean exit (no exception), pushes back
    with if-generation-match and sets the publish-pending flag.

    With read_only=True, the push step is skipped — useful for automation
    or read-only CLI paths that answer questions from the canonical DB.
    """
    local_path, gen = pull_db()
    prev_env = os.environ.get("CASITA_DB_PATH")
    os.environ["CASITA_DB_PATH"] = str(local_path)
    try:
        yield local_path
    except Exception:
        # Drop the temp file; never push on error.
        try:
            local_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    else:
        if not read_only:
            try:
                push_db(local_path, gen)
            except ConflictError:
                # One-shot retry: caller has to redo their mutation. We
                # don't auto-retry the verb itself because we don't know
                # how to replay it from here — the caller decides.
                raise
            if mark_publish:
                set_publish_pending()
    finally:
        # Restore env even if we raised.
        if prev_env is None:
            os.environ.pop("CASITA_DB_PATH", None)
        else:
            os.environ["CASITA_DB_PATH"] = prev_env
        try:
            local_path.unlink(missing_ok=True)
        except Exception:
            pass


def upload_db_initial(local_path: Path) -> int:
    """One-shot: push a local canonical DB to GCS for the first time.

    Used during migration. Doesn't enforce a generation match — overwrites
    whatever's there.
    """
    blob = _bucket().blob(_db_object())
    blob.upload_from_filename(
        str(local_path), content_type="application/octet-stream"
    )
    return blob.generation
