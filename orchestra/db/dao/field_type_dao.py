from typing import Dict

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dao.log_dao import LogDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import FieldType


class FieldTypeDAO:
    def __init__(self, session: Session = Depends(get_db_session)):
        self.session = session

    def create_field_type(self, project_id: int, field_name: str, value) -> None:
        """Create a new field type for a project."""
        field_type = LogDAO.infer_type(value)
        new_field_type = FieldType(
            project_id=project_id,
            field_name=field_name,
            field_type=field_type,
        )
        self.session.add(new_field_type)
        self.session.commit()

    def get_field_types(self, project_id: int) -> Dict[str, str]:
        """Retrieve field types for a specific project."""
        query = select(FieldType).where(FieldType.project_id == project_id)
        field_types = self.session.execute(query).scalars().all()
        return {
            field_type.field_name: field_type.field_type for field_type in field_types
        }

    def update_field_type(self, project_id: int, field_name: str, value) -> None:
        """Update the type for a specific field in a project."""
        field_type = self._serialize_type(value)
        query = select(FieldType).where(
            FieldType.project_id == project_id,
            FieldType.field_name == field_name,
        )
        existing_field_type = self.session.execute(query).scalars().first()

        if existing_field_type:
            existing_field_type.field_type = field_type
            self.session.commit()
        else:
            raise ValueError("Field type does not exist.")

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
