from typing import Dict, Union

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from orchestra.db.dao.log_dao import LogDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import FieldType


class FieldTypeDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_field_type_if_absent(
        self,
        project_id: int,
        field_name: str,
        value,
        mutable: bool = False,
        field_category: str = "entry",
    ) -> None:
        """Upsert approach: insert or do nothing if it exists."""
        # First check if a field with this name exists but with a different category
        existing = (
            self.session.query(FieldType)
            .filter(
                FieldType.project_id == project_id,
                FieldType.field_name == field_name,
            )
            .first()
        )
        if existing:
            if existing.field_category != field_category:
                new_article = "an" if field_category == "entry" else "a"
                existing_article = "an" if existing.field_category == "entry" else "a"
                raise ValueError(
                    f"Field '{field_name}' already exists as {existing_article} {existing.field_category}. "
                    f"Cannot create it as {new_article} {field_category}.",
                )
            return

        inferred_type = LogDAO.infer_type(field_name, value)

        stmt = pg_insert(FieldType).values(
            project_id=project_id,
            field_name=field_name,
            field_type=inferred_type,
            field_category=field_category,
            mutable=mutable,
        )
        # "on_conflict_do_nothing" will skip insertion if (project_id, field_name) already exists:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["project_id", "field_name"],
        )
        self.session.execute(stmt)
        self.session.commit()

    def get_field_types(
        self,
        project_id: int,
        return_mutable: bool = False,
    ) -> Dict[str, Union[str, Dict[str, Union[str, bool]]]]:
        """Retrieve field types for a specific project ordered by creation time."""
        query = (
            select(FieldType)
            .where(FieldType.project_id == project_id)
            .order_by(FieldType.created_at)
        )
        field_types = self.session.execute(query).scalars().all()
        if return_mutable:
            return {
                field_type.field_name: {
                    "field_type": field_type.field_type,
                    "field_category": field_type.field_category,
                    "mutable": field_type.mutable,
                    "created_at": field_type.created_at.isoformat()
                    if field_type.created_at
                    else None,
                }
                for field_type in field_types
            }
        else:
            return {
                field_type.field_name: field_type.field_type
                for field_type in field_types
            }

    def upsert_field_type(
        self,
        project_id: int,
        field_name: str,
        value,
        mutable: bool = False,
        field_category: str = "entry",
    ) -> None:
        """Upsert approach: insert or overwrite the existing field_type."""
        # First check if a field with this name exists but with a different category
        existing = (
            self.session.query(FieldType)
            .filter(
                FieldType.project_id == project_id,
                FieldType.field_name == field_name,
            )
            .first()
        )
        if existing and existing.field_category != field_category:
            raise ValueError(
                f"Field '{field_name}' already exists as a {existing.field_category}. "
                f"Cannot update it to a {field_category}.",
            )

        inferred_type = LogDAO.infer_type(field_name, value)

        stmt = pg_insert(FieldType).values(
            project_id=project_id,
            field_name=field_name,
            field_type=inferred_type,
            field_category=field_category,
            mutable=mutable,
        )
        # "on_conflict_do_update" to update existing row if it already exists
        stmt = stmt.on_conflict_do_update(
            index_elements=["project_id", "field_name"],
            set_={
                "field_type": inferred_type,
                "field_category": field_category,
                "mutable": mutable,
            },
        )
        self.session.execute(stmt)
        self.session.commit()

    def update_field_mutability(
        self,
        project_id: int,
        field_name: str,
        mutable: bool,
    ) -> None:
        """Update only the mutability attribute of a field type using an upsert approach."""
        # First get the existing field type if it exists
        existing = (
            self.session.query(FieldType)
            .filter(
                FieldType.project_id == project_id,
                FieldType.field_name == field_name,
            )
            .first()
        )

        if not existing:
            raise ValueError(f"Field type {field_name} does not exist")

        existing.mutable = mutable
        self.session.commit()

    def delete_field_type(self, project_id: int, field_name: str) -> None:
        """Delete a specific field type for a project."""
        query = select(FieldType).where(
            FieldType.project_id == project_id,
            FieldType.field_name == field_name,
        )
        field_type = self.session.execute(query).scalars().first()

        if field_type:
            self.session.delete(field_type)
            self.session.commit()
        else:
            raise ValueError("Field type does not exist.")

    def get_ordered_field_names(self, project_id: int) -> Dict[str, int]:
        """Retrieve field names for a project ordered by creation time."""
        query = (
            select(FieldType.field_name)
            .where(
                FieldType.project_id == project_id,
            )
            .order_by(FieldType.created_at)
        )

        result = self.session.execute(query).scalars().all()
        return {field: i for i, field in enumerate(result)}

    def rename_field(
        self,
        project_id: int,
        old_field_name: str,
        new_field_name: str,
    ) -> None:
        """Rename a field type for a given project.

        Args:
            project_id: The ID of the project containing the field
            old_field_name: The current name of the field to rename
            new_field_name: The new name to assign to the field

        Raises:
            ValueError: If the field doesn't exist or if the new name conflicts with an existing field
        """
        # First check if the old field exists
        field_to_rename = (
            self.session.query(FieldType)
            .filter(
                FieldType.project_id == project_id,
                FieldType.field_name == old_field_name,
            )
            .first()
        )

        if not field_to_rename:
            raise ValueError(
                f"Field '{old_field_name}' does not exist in project {project_id}",
            )

        # Check if the new name would conflict with an existing field
        existing_field = (
            self.session.query(FieldType)
            .filter(
                FieldType.project_id == project_id,
                FieldType.field_name == new_field_name,
            )
            .first()
        )

        if existing_field:
            raise ValueError(
                f"Cannot rename field to '{new_field_name}' as it already exists in project {project_id}",
            )

        # Perform the rename
        field_to_rename.field_name = new_field_name
        self.session.commit()
