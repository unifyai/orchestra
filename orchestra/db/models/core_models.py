"""Kernel ORM models for orchestra.

These tables form the persistence kernel: projects own contexts and logs;
log events live inside contexts; field types describe schema; embeddings
back vector search. The hosted product layers user
accounts, organizations, billing, and Console UI tables on top of this
schema, declared on the same SQLAlchemy `Base`.

Core models do not declare ForeignKey constraints to platform-only tables
(`user`, `organization`). The `user_id` / `organization_id` columns on
`Project` are kept as nullable scoping columns so the kernel schema is
standalone-valid; the platform's initial migration adds the FK constraints
once the platform tables exist.
"""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from orchestra.db.base import Base


class Project(Base):
    __tablename__ = "project"

    id = Column(Integer, primary_key=True)
    # Nullable scoping columns. The kernel does not enforce FKs here; the
    # platform layer adds FK constraints to its `user` and `organization`
    # tables in a follow-up migration.
    user_id = Column(String, index=True, nullable=True)
    organization_id = Column(Integer, index=True, nullable=True)
    name = Column(String, nullable=False)
    description = Column(String(256), nullable=True)
    icon = Column(String, nullable=False, server_default="folder")
    order = Column(Integer, nullable=False, server_default="0")
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())
    is_versioned = Column(Boolean, nullable=False, server_default="f")
    # Public-read projects are readable (data-plane reads only) by any
    # authenticated account. Used for platform-wide read-only datasets such as
    # the builtin function primitives catalogue.
    is_public_read = Column(Boolean, nullable=False, server_default="f")
    current_commit_hash = Column(String, nullable=True)
    contexts = relationship("Context", back_populates="project", passive_deletes=True)

    __table_args__ = (
        UniqueConstraint("user_id", "name"),
        UniqueConstraint("organization_id", "name"),
        sa.CheckConstraint(
            "char_length(description) <= 256",
            name="ck_project_description_len",
        ),
    )


class ProjectVersion(Base):
    """Historical versions of projects."""

    __tablename__ = "project_version"

    id = Column(Integer, primary_key=True)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    commit_hash = Column(String, nullable=False, unique=True)
    prev_commit_hash = Column(String, nullable=True)
    next_commit_hash = Column(JSONB, nullable=False, server_default="[]")
    commit_message = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    context_versions = relationship("ContextVersion", back_populates="project_version")


class LogEventContext(Base):
    """Association table for the many-to-many relationship between LogEvent and Context.

    Partitioned by LIST (project_id) so a project's associations are dropped with
    the project's log_event partition. ``project_id`` is denormalized from
    ``log_event`` and is part of the composite primary key (required by the
    partition key). The FK to ``log_event`` is intentionally dropped: ``log_event``
    is partitioned with a composite PK ``(project_id, id)``, so a single-column FK
    can no longer reference it, and partition drops must not be blocked by FK
    references. Integrity of (log_event_id -> log_event) is maintained by the
    application and by coordinated partition drops.
    """

    __tablename__ = "log_event_context"

    project_id = Column(Integer, nullable=False, primary_key=True)
    log_event_id = Column(Integer, primary_key=True)
    context_id = Column(
        Integer,
        ForeignKey("context.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Owning scope of the referenced log_event (see orchestra.db.scope). Carried
    # on every association (including those into aggregation views) so all of an
    # assistant/team's associations live in its sub-partition and drop together.
    owner_key = Column(
        String,
        nullable=False,
        primary_key=True,
        server_default="sys",
    )

    __table_args__ = (
        Index("idx_log_event_context_context_id", "context_id"),
        Index("idx_log_event_context_log_event_id", "log_event_id"),
        # Owner-scoped deletion: makes ``DELETE ... WHERE project_id AND
        # owner_key`` (per-assistant/team purge) O(the owner's rows).
        Index("idx_log_event_context_project_owner", "project_id", "owner_key"),
        {"postgresql_partition_by": "LIST (project_id)"},
    )


class Context(Base):
    """Model class for organizing logs and artifacts within projects."""

    __tablename__ = "context"

    id = Column(Integer, primary_key=True)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String, nullable=False)
    description = Column(String(256), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())
    is_versioned = Column(Boolean, nullable=False, server_default="f")
    allow_duplicates = Column(Boolean, nullable=False, server_default="t")
    unique_key_names = Column(JSONB, nullable=False, server_default="[]")
    unique_key_types = Column(JSONB, nullable=False, server_default="[]")
    auto_counting = Column(JSONB, nullable=False, server_default="{}")
    foreign_keys = Column(JSONB, nullable=False, server_default="[]")
    current_commit_hash = Column(String, nullable=True)
    # Ownership scope: the entity whose data this context holds and the unit of
    # bulk deletion (see orchestra.db.scope). ``owner_scope`` is one of
    # assistant/team/aggregation/system; ``owner_id`` is the agent_id (assistant)
    # or team_id (team), else NULL. Logs created here denormalize this owner onto
    # the heavy tables so an assistant/team can be dropped as a partition.
    owner_scope = Column(String, nullable=True)
    owner_id = Column(Integer, nullable=True)

    project = relationship("Project", back_populates="contexts")
    log_events = relationship(
        "LogEvent",
        secondary="log_event_context",
        primaryjoin="Context.id == foreign(LogEventContext.context_id)",
        secondaryjoin="LogEvent.id == foreign(LogEventContext.log_event_id)",
        back_populates="contexts",
        viewonly=True,
    )

    @property
    def unique_keys(self):
        """Reconstruct unique_keys dict from the separate arrays."""
        if not self.unique_key_names or not self.unique_key_types:
            return {}
        return dict(zip(self.unique_key_names, self.unique_key_types))

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_project_context_name"),
        sa.CheckConstraint(
            "char_length(description) <= 256",
            name="ck_context_description_len",
        ),
        # Find all contexts owned by a given assistant/team within a project.
        Index(
            "idx_context_owner",
            "project_id",
            "owner_scope",
            "owner_id",
            postgresql_where=text("owner_id IS NOT NULL"),
        ),
    )


class ContextCounter(Base):
    """Materialized auto-counter state for context-scoped log IDs."""

    __tablename__ = "context_counter"

    context_id = Column(
        Integer,
        ForeignKey("context.id", ondelete="CASCADE"),
        primary_key=True,
    )
    column_name = Column(Text, primary_key=True)
    parent_values_hash = Column(Text, primary_key=True)
    parent_values = Column(JSONB, nullable=False)
    next_value = Column(BigInteger, nullable=False)
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ContextVersion(Base):
    """Historical versions of contexts."""

    __tablename__ = "context_version"

    id = Column(Integer, primary_key=True)
    context_id = Column(
        Integer,
        ForeignKey("context.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_version_id = Column(
        Integer,
        ForeignKey("project_version.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name = Column(String, nullable=True)
    description = Column(String, nullable=True)
    archived_at = Column(TIMESTAMP, server_default=func.now())
    commit_hash = Column(String, nullable=False)
    prev_commit_hash = Column(String, nullable=True)
    next_commit_hash = Column(JSONB, nullable=False, server_default="[]")
    commit_message = Column(String, nullable=True)

    project_version = relationship("ProjectVersion", back_populates="context_versions")
    log_event_versions = relationship(
        "LogEventVersion",
        back_populates="context_version",
        cascade="all, delete-orphan",
    )


class LogEvent(Base):
    __tablename__ = "log_event"

    # Partitioned by LIST (project_id): the partition key must be part of the
    # primary key, so the PK is composite (project_id, id). ``id`` keeps its own
    # sequence (globally unique) so existing single-column id lookups still work;
    # tenant deletion drops a whole partition instead of per-row index churn.
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    data = Column(JSONB, nullable=False, server_default=text("'{}'"))
    # Stores original insertion order of nested dictionary keys.
    key_order = Column(JSONB, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())
    # Owning scope (see orchestra.db.scope): the assistant/team whose context
    # created this log. The LIST sub-partition key the shared Assistants project
    # is divided by, enabling O(1) per-assistant / per-team deletion.
    owner_key = Column(
        String,
        nullable=False,
        primary_key=True,
        server_default="sys",
    )
    contexts = relationship(
        "Context",
        secondary="log_event_context",
        primaryjoin="LogEvent.id == foreign(LogEventContext.log_event_id)",
        secondaryjoin="Context.id == foreign(LogEventContext.context_id)",
        back_populates="log_events",
        viewonly=True,
    )

    __table_args__ = (
        Index("idx_log_event_project_id_id", "project_id", "id"),
        Index("idx_log_event_data", "data", postgresql_using="gin"),
        # Owner-scoped deletion: makes ``DELETE ... WHERE project_id AND
        # owner_key`` (per-assistant/team purge) O(the owner's rows) instead of a
        # scan of the whole shared Assistants project.
        Index("idx_log_event_project_owner", "project_id", "owner_key"),
        {"postgresql_partition_by": "LIST (project_id)"},
    )


class LogEventVersion(Base):
    """JSONB snapshots of log events for versioning."""

    __tablename__ = "log_event_version"

    id = Column(Integer, primary_key=True)
    context_version_id = Column(
        Integer,
        ForeignKey("context_version.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    log_event_id = Column(Integer, nullable=False, index=True)
    data = Column(JSONB, nullable=False)
    key_order = Column(JSONB, nullable=True)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)

    context_version = relationship(
        "ContextVersion",
        back_populates="log_event_versions",
    )


class ActiveDerivedLog(Base):
    """Filter-based derived logs that are applied to future base logs."""

    __tablename__ = "active_derived_log_template"

    id = Column(Integer, primary_key=True)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    context_id = Column(
        Integer,
        ForeignKey("context.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key = Column(String, nullable=False, index=True)
    equation = Column(String, nullable=False)
    referenced_logs = Column(JSONB, nullable=False)
    filter_expression = Column(JSONB, nullable=False)
    inferred_type = Column(String)
    referenced_keys = Column(JSONB, nullable=True)
    is_active = Column(Boolean, nullable=False, server_default="t")
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, onupdate=func.now())

    __table_args__ = (UniqueConstraint("project_id", "context_id", "key"),)


class LogUniqueConstraint(Base):
    """
    Lookup table for efficient unique field validation.

    Replaces O(N x M) JSONB containment scans with O(M x log N) B-tree lookups
    for checking unique field constraints during log creation/update.

    Supports:
    - Single unique fields: field_name = 'row_id', value_hash = md5(value)
    - Composite keys: field_name = '__composite__', value_hash = md5(json(combo))
    """

    __tablename__ = "log_unique_constraint"

    context_id = Column(Integer, nullable=False)
    field_name = Column(String, nullable=False)
    value_hash = Column(String(32), nullable=False)
    # FK to log_event dropped: log_event now has a composite PK (project_id, id)
    # and is partitioned, so a single-column FK can no longer reference it.
    # Orphan rows are cleaned by the application / coordinated partition drops.
    log_event_id = Column(BigInteger, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        sa.PrimaryKeyConstraint("context_id", "field_name", "value_hash"),
        Index("idx_log_unique_constraint_log_event", "log_event_id"),
    )


class FieldType(Base):
    __tablename__ = "field_type"

    id = Column(Integer, primary_key=True)
    project_id = Column(
        Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    )
    context_id = Column(
        Integer,
        ForeignKey("context.id", ondelete="CASCADE"),
        nullable=True,
    )
    field_name = Column(String, nullable=False)
    field_type = Column(String, nullable=False)
    field_category = Column(
        String,
        nullable=False,
        server_default="entry",
    )
    mutable = Column(Boolean(), nullable=False, server_default="t")
    unique = Column(Boolean(), nullable=False, server_default="f")
    enum_values = Column(JSONB, nullable=False, server_default=text("'[]'"))
    enum_restrict = Column(Boolean(), nullable=False, server_default="false")
    description = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    # NULL = field has never been null-merged into every existing log_event row of its context.
    # Non-NULL = a POST /v0/logs/fields call with backfill_logs=True has already null-merged
    # this field across the whole context, so idempotent re-POSTs may skip the expensive
    # log_event UPDATE.
    backfilled_at = Column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "field_name",
            "context_id",
            name="uq_project_field_name_context_id",
        ),
        Index("idx_field_type_context_id", "context_id"),
        Index(
            "idx_field_type_needs_backfill",
            "project_id",
            "context_id",
            postgresql_where=text("backfilled_at IS NULL"),
        ),
        sa.CheckConstraint(
            "char_length(description) <= 256",
            name="ck_field_type_description_len",
        ),
    )


class Embedding(Base):
    """Embeddings table.

    Supports soft-delete via the `is_deleted` column to avoid expensive HNSW
    index surgery during deletions. When embeddings are "deleted", they are
    marked with is_deleted=True rather than being physically removed.

    The HNSW indexes include `AND is_deleted = false` to exclude soft-deleted
    rows so they don't participate in vector similarity searches.
    """

    __tablename__ = "embedding"

    # Partitioned by LIST (project_id): project_id is denormalized from the
    # referenced log_event so embeddings (and their per-partition HNSW indexes)
    # are dropped instantly with the project's partition. project_id is part of
    # the composite PK (required by the partition key). The FK to log_event is
    # dropped (log_event now has a composite PK and partition drops must not be
    # blocked); ref_id remains a plain column. Soft-delete + index_maintenance
    # still reclaim rows for non-partition-drop deletions.
    project_id = Column(Integer, nullable=False, primary_key=True)
    id = Column(Integer, primary_key=True, autoincrement=True)
    ref_id = Column(Integer, nullable=True)
    model = Column(String, nullable=False)
    key = Column(String, nullable=False)
    vector = Column(Vector(), nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())
    is_deleted = Column(Boolean, nullable=False, server_default=sa.text("false"))
    # Owning scope of the referenced log_event (see orchestra.db.scope); the
    # LIST sub-partition key so an assistant/team's vectors (and their HNSW
    # index segment) drop with its partition.
    owner_key = Column(
        String,
        nullable=False,
        primary_key=True,
        server_default="sys",
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "owner_key",
            "ref_id",
            "model",
            "key",
            name="uq_embedding",
        ),
        Index(
            "idx_embedding_ref",
            "ref_id",
            "model",
            "key",
        ),
        Index("idx_embedding_is_deleted", "is_deleted"),
        Index("idx_embedding_ref_id_is_deleted", "ref_id", "is_deleted"),
        sa.CheckConstraint(
            "model <> 'text-embedding-3-small' OR vector_dims(vector) = 1536",
            name="embedding_dims_text_openai_chk",
        ),
        sa.CheckConstraint(
            "model <> 'multimodalembedding@001' OR vector_dims(vector) = 1408",
            name="embedding_dims_vertexai_chk",
        ),
        Index(
            "embedding_hnsw_cosine_openai_1536_idx",
            sa.text("(vector::vector(1536)) vector_cosine_ops"),
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_where=sa.text(
                "model = 'text-embedding-3-small' AND is_deleted = false",
            ),
        ),
        Index(
            "embedding_hnsw_cosine_vertexai_1408_idx",
            sa.text("(vector::vector(1408)) vector_cosine_ops"),
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_where=sa.text(
                "model = 'multimodalembedding@001' AND is_deleted = false",
            ),
        ),
        {"postgresql_partition_by": "LIST (project_id)"},
    )


class EmbeddingQueue(Base):
    """Queue for async embedding generation with two-stage processing pipeline.

    Stage 1 (parallel-safe): Generate embedding vectors. Multiple workers can
    run concurrently using FOR UPDATE SKIP LOCKED. pending -> generating ->
    vector_ready.

    Stage 2 (serial): Bulk insert into indexed Embedding table. Single worker
    for optimal HNSW index performance. vector_ready -> inserting -> deleted.

    Status values:
    - pending: Waiting for Stage 1 processing
    - generating: Being processed by Stage 1 worker (vector generation)
    - vector_ready: Vector generated, awaiting Stage 2 (index insertion)
    - inserting: Being processed by Stage 2 worker (bulk insert)
    - completed: Successfully processed (will be deleted from queue)
    - failed: Failed after max retries (kept for debugging)
    - cancelled: Deliberately stopped (e.g. parent project deleted)
    """

    __tablename__ = "embedding_queue"

    # Partitioned by LIST (project_id) (denormalized from log_event); project_id
    # is part of the composite PK. FK to log_event dropped (composite-PK parent /
    # partition-drop friendliness); ref_id remains a plain column.
    project_id = Column(Integer, nullable=False, primary_key=True)
    id = Column(Integer, primary_key=True, autoincrement=True)
    ref_id = Column(Integer, nullable=False)
    key = Column(String, nullable=False)
    text = Column(String, nullable=False)
    model = Column(String, nullable=False)
    dimensions = Column(Integer, nullable=True)
    status = Column(String, nullable=False, server_default="pending")
    retry_count = Column(Integer, nullable=False, server_default=sa.text("0"))
    error_message = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    processing_started_at = Column(TIMESTAMP, nullable=True)

    generated_vector = Column(Vector(), nullable=True)
    vector_generated_at = Column(TIMESTAMP, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "ref_id",
            "key",
            "model",
            name="uq_embedding_queue",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'generating', 'vector_ready', 'inserting', "
            "'completed', 'failed', 'cancelled')",
            name="chk_embedding_queue_status",
        ),
        Index("idx_embedding_queue_status_created", "status", "created_at"),
        Index("idx_embedding_queue_ref_id", "ref_id"),
        Index(
            "idx_embedding_queue_processing_started",
            "status",
            "processing_started_at",
        ),
        Index(
            "idx_embedding_queue_vector_ready",
            "created_at",
            postgresql_where=sa.text("status = 'vector_ready'"),
        ),
        {"postgresql_partition_by": "LIST (project_id)"},
    )
