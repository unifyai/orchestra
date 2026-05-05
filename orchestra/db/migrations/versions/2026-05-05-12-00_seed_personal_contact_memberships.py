"""Seed personal contact memberships.

Revision ID: seed_personal_cm
Revises: tighten_space_description
Create Date: 2026-05-05 12:00:00.000000
"""

from alembic import op

revision = "seed_personal_cm"
down_revision = "tighten_space_description"
branch_labels = None
depends_on = None

_BOSS_CONTACT_RESPONSE_POLICY = (
    "Your immediate manager, please do whatever they ask you to do within reason, "
    "and do *not* withhold any "
    "information from them."
)


def upgrade() -> None:
    op.execute(
        """
        UPDATE contact_memberships cm
        SET
            relationship = 'self',
            should_respond = TRUE,
            response_policy = '',
            can_edit = TRUE
        WHERE cm.target_scope = 'personal'
          AND cm.contact_id = 0
          AND NOT EXISTS (
              SELECT 1
              FROM contact_memberships existing
              WHERE existing.assistant_id = cm.assistant_id
                AND existing.target_scope = 'personal'
                AND existing.relationship = 'self'
          )
        """,
    )
    op.execute(
        f"""
        UPDATE contact_memberships cm
        SET
            relationship = 'boss',
            should_respond = TRUE,
            response_policy = {_BOSS_CONTACT_RESPONSE_POLICY!r},
            can_edit = TRUE
        WHERE cm.target_scope = 'personal'
          AND cm.contact_id = 1
          AND NOT EXISTS (
              SELECT 1
              FROM contact_memberships existing
              WHERE existing.assistant_id = cm.assistant_id
                AND existing.target_scope = 'personal'
                AND existing.relationship = 'boss'
          )
        """,
    )
    op.execute(
        f"""
        WITH desired_relationships AS (
            SELECT *
            FROM (
                VALUES
                    ('self', 0, TRUE, '', TRUE),
                    ('boss', 1, TRUE, {_BOSS_CONTACT_RESPONSE_POLICY!r}, TRUE)
            ) AS desired(
                relationship,
                contact_id,
                should_respond,
                response_policy,
                can_edit
            )
        )
        INSERT INTO contact_memberships (
            assistant_id,
            contact_id,
            target_scope,
            target_space_id,
            relationship,
            should_respond,
            response_policy,
            can_edit
        )
        SELECT
            a.agent_id,
            desired.contact_id,
            'personal',
            NULL,
            desired.relationship,
            desired.should_respond,
            desired.response_policy,
            desired.can_edit
        FROM assistants a
        CROSS JOIN desired_relationships desired
            WHERE NOT EXISTS (
                SELECT 1
                FROM contact_memberships cm
                WHERE cm.assistant_id = a.agent_id
                  AND cm.target_scope = 'personal'
                  AND cm.relationship = desired.relationship
            )
        ON CONFLICT (assistant_id, contact_id) WHERE target_scope = 'personal'
        DO NOTHING
        """,
    )


def downgrade() -> None:
    pass
