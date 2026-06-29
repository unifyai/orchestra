"""Make Builtins a system-owned platform project.

Revision ID: system_builtins_project
Revises: whatsapp_call_permission_state
Create Date: 2026-07-03 00:00:00.000000
"""

from alembic import op

revision = "system_builtins_project"
down_revision = "whatsapp_call_permission_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.project
            ADD COLUMN IF NOT EXISTS is_system BOOLEAN DEFAULT false NOT NULL;
        """,
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_project_is_system
            ON public.project (is_system)
            WHERE is_system;
        """,
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_project_system_name
            ON public.project (name)
            WHERE is_system;
        """,
    )
    op.execute(
        """
        WITH canonical AS (
            SELECT id
            FROM public.project
            WHERE name = 'Builtins'
            ORDER BY is_system DESC, is_public_read DESC, id ASC
            LIMIT 1
        )
        UPDATE public.project
        SET
            user_id = NULL,
            organization_id = NULL,
            is_public_read = true,
            is_system = true,
            is_versioned = true,
            description = COALESCE(description, 'System Builtins catalogue')
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
            'Builtins',
            'System Builtins catalogue',
            'folder',
            0,
            true,
            true,
            true
        WHERE NOT EXISTS (
            SELECT 1 FROM public.project WHERE name = 'Builtins'
        );
        """,
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE public.project
        SET is_system = false
        WHERE name = 'Builtins';
        """,
    )
    op.execute("DROP INDEX IF EXISTS uq_project_system_name;")
    op.execute("DROP INDEX IF EXISTS ix_project_is_system;")
    op.execute("ALTER TABLE public.project DROP COLUMN IF EXISTS is_system;")
