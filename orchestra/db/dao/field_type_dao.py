from typing import Dict, List

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
    ) -> None:
        """Upsert approach: insert or do nothing if it exists."""
        inferred_type = LogDAO.infer_type(field_name, value)

        stmt = pg_insert(FieldType).values(
            project_id=project_id,
            field_name=field_name,
            field_type=inferred_type,
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
    ) -> Dict[str, str]:
        """Retrieve field types for a specific project ordered by creation time (id)."""
        query = (
            select(FieldType)
            .where(FieldType.project_id == project_id)
            .order_by(FieldType.id)
        )
        field_types = self.session.execute(query).scalars().all()
        if return_mutable:
            return {
                field_type.field_name: {
                    "field_type": field_type.field_type,
                    "mutable": field_type.mutable,
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
    ) -> None:
        """Upsert approach: insert or overwrite the existing field_type."""
        inferred_type = LogDAO.infer_type(field_name, value)

        stmt = pg_insert(FieldType).values(
            project_id=project_id,
            field_name=field_name,
            field_type=inferred_type,
            mutable=mutable,
        )
        # "on_conflict_do_update" to update existing row if it already exists
        stmt = stmt.on_conflict_do_update(
            index_elements=["project_id", "field_name"],
            set_={
                "field_type": inferred_type,
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

    def get_ordered_field_names(self, project_id: int) -> List[str]:
        """Retrieve field names for a project ordered by creation time (id)."""
        query = (
            select(FieldType.field_name)
            .where(
                FieldType.project_id == project_id,
            )
            .order_by(FieldType.id)
        )

        result = self.session.execute(query).scalars().all()
        return {field: i for i, field in enumerate(result)}
