"""Shared resumable upload boundaries for staged and legacy entire-body paths."""

from sqlalchemy import select

from coinpup_api.files.resume_models import UploadResume
from coinpup_api.ledger.service import LedgerError, _not_found
from coinpup_api.sync.device_models import DeviceRevision


def require_active_device(session, owner, device):
    state = session.scalar(
        select(DeviceRevision.state)
        .where(DeviceRevision.owner_id == owner, DeviceRevision.device_id == device)
        .order_by(DeviceRevision.version.desc())
        .limit(1)
    )
    if state is None:
        raise _not_found()
    if state != "registered":
        raise LedgerError("sync_device_retired", 409, "The uploading device has been retired.")


def check_resume(session, upload, device, *, digest=None, existing=False):
    manifest = session.get(UploadResume, upload.id)
    if manifest is None:
        if existing:
            raise _not_found()
        return None
    if device is None:
        raise LedgerError("upload_device_required", 409, "Use the device for this upload.")
    if manifest.ledger_id != upload.ledger_id or manifest.device_id != device:
        raise _not_found()
    if digest is not None and manifest.sha256 != digest:
        raise LedgerError("upload_content_conflict", 409, "The upload content does not match.")
    return manifest


def require_not_abandoned(manifest):
    if manifest is not None and manifest.abandoned_at is not None:
        raise LedgerError("upload_abandoned", 409, "The resumable upload was abandoned.")
