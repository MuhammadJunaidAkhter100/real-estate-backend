from __future__ import annotations

from django.conf import settings
from django.core.files.storage import Storage, default_storage

from api.storage import TimeoutS3Storage


class PrivateCallRecordingStorage(TimeoutS3Storage):
    """Private storage backend for call recordings."""

    default_acl = 'private'
    file_overwrite = False
    location = 'call-recordings'
    querystring_auth = True
    querystring_expire = 300


def get_call_recording_storage() -> Storage:
    if settings.USE_S3:
        return PrivateCallRecordingStorage()
    return default_storage
