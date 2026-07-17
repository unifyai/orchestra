"""Request/response models for sync leases."""

from pydantic import BaseModel, Field


class SyncLeaseAcquireRequest(BaseModel):
    """Acquire (or renew) an exclusive sync lease."""

    project: str = Field(..., description="Project that owns the lease namespace.")
    lease_key: str = Field(
        ...,
        description=(
            "Stable key for the protected resource, e.g. "
            "'Teams/11/Functions/Compositional:custom_sync'."
        ),
        min_length=1,
        max_length=512,
    )
    holder: str = Field(
        ...,
        description="Opaque id for the writer (pod/job/process).",
        min_length=1,
        max_length=256,
    )
    ttl_seconds: float = Field(
        300.0,
        gt=0,
        le=3600,
        description="Lease lifetime from now. Callers must finish or renew before expiry.",
    )


class SyncLeaseReleaseRequest(BaseModel):
    """Release a sync lease previously acquired by the same holder."""

    project: str = Field(..., description="Project that owns the lease namespace.")
    lease_key: str = Field(..., min_length=1, max_length=512)
    holder: str = Field(..., min_length=1, max_length=256)


class SyncLeaseResponse(BaseModel):
    """Outcome of a lease acquire/release."""

    acquired: bool = False
    released: bool = False
    lease_key: str
    holder: str | None = None
    expires_at: str | None = None
    held_by: str | None = None
