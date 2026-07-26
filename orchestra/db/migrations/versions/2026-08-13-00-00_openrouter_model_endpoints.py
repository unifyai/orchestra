"""Migrate assistant LLM endpoints from OpenAI-aliased OpenRouter routes.

Previously ``gpt-*@openai`` was transported via OpenRouter inside UniLLM.
Those public aliases now mean native OpenAI. Rewrite stored assistant
``default_model`` / ``slow_brain_model`` values to the canonical
``openai/<id>@openrouter`` form so production assistants keep working
without OpenAI API keys.

Revision ID: openrouter_model_endpoints
Revises: ms_teams_bot_welcomes
Create Date: 2026-07-26 21:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "openrouter_model_endpoints"
down_revision = "ms_teams_bot_welcomes"
branch_labels = None
depends_on = None

# Exact endpoint rewrites for previously OpenRouter-aliased OpenAI names.
_REWRITES: tuple[tuple[str, str], ...] = (
    ("gpt-5.6-sol@openai", "openai/gpt-5.6-sol@openrouter"),
    ("gpt-5.6-terra@openai", "openai/gpt-5.6-terra@openrouter"),
    ("gpt-5.6-luna@openai", "openai/gpt-5.6-luna@openrouter"),
    ("gpt-5.5@openai", "openai/gpt-5.5@openrouter"),
    ("gpt-5.4@openai", "openai/gpt-5.4@openrouter"),
    ("gpt-5.4-mini@openai", "openai/gpt-5.4-mini@openrouter"),
    ("gpt-5.4-nano@openai", "openai/gpt-5.4-nano@openrouter"),
    ("gpt-5.2@openai", "openai/gpt-5.2@openrouter"),
    ("gpt-5.1@openai", "openai/gpt-5.1@openrouter"),
    ("gpt-5@openai", "openai/gpt-5@openrouter"),
    ("gpt-5-mini@openai", "openai/gpt-5-mini@openrouter"),
    ("gpt-5-nano@openai", "openai/gpt-5-nano@openrouter"),
    ("gpt-4.1@openai", "openai/gpt-4.1@openrouter"),
    ("gpt-4.1-mini@openai", "openai/gpt-4.1-mini@openrouter"),
    ("gpt-4.1-nano@openai", "openai/gpt-4.1-nano@openrouter"),
    ("gpt-4o@openai", "openai/gpt-4o@openrouter"),
    ("gpt-4o-mini@openai", "openai/gpt-4o-mini@openrouter"),
)


def upgrade() -> None:
    conn = op.get_bind()
    for old, new in _REWRITES:
        conn.execute(
            sa.text(
                "UPDATE assistants SET default_model = :new "
                "WHERE default_model = :old",
            ),
            {"old": old, "new": new},
        )
        conn.execute(
            sa.text(
                "UPDATE assistants SET slow_brain_model = :new "
                "WHERE slow_brain_model = :old",
            ),
            {"old": old, "new": new},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for old, new in _REWRITES:
        conn.execute(
            sa.text(
                "UPDATE assistants SET default_model = :old "
                "WHERE default_model = :new",
            ),
            {"old": old, "new": new},
        )
        conn.execute(
            sa.text(
                "UPDATE assistants SET slow_brain_model = :old "
                "WHERE slow_brain_model = :new",
            ),
            {"old": old, "new": new},
        )
