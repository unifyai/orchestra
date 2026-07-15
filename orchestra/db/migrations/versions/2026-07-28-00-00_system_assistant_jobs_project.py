"""Make AssistantJobs a system-owned platform project.

Revision ID: system_assistant_jobs_project
Revises: log_unique_constraint_project_id
Create Date: 2026-07-15 00:00:00.000000
"""

from alembic import op

revision = "system_assistant_jobs_project"
down_revision = "log_unique_constraint_project_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Promote the canonical AssistantJobs row (prefer existing system / oldest id)
    # and clear ownership so fleet audit no longer depends on a User API key.
    op.execute(
        """
        WITH canonical AS (
            SELECT id
            FROM public.project
            WHERE name = 'AssistantJobs'
            ORDER BY is_system DESC, id ASC
            LIMIT 1
        )
        UPDATE public.project
        SET
            user_id = NULL,
            organization_id = NULL,
            is_public_read = false,
            is_system = true,
            is_versioned = false,
            description = COALESCE(
                description,
                'Platform fleet audit and Console liveview discovery'
            )
        WHERE id = (SELECT id FROM canonical);
        """,
    )
    op.execute(
        """
        INSERT INTO public.project (
            name,
            description,
            icon,
            "order",
            is_versioned,
            is_public_read,
            is_system
        )
        SELECT
            'AssistantJobs',
            'Platform fleet audit and Console liveview discovery',
            'folder',
            0,
            false,
            false,
            true
        WHERE NOT EXISTS (
            SELECT 1 FROM public.project WHERE name = 'AssistantJobs'
        );
        """,
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE public.project
        SET is_system = false
        WHERE name = 'AssistantJobs';
        """,
    )
