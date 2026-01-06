"""Async version of field_type_dao for use with AsyncSession."""

from typing import Any, Dict, List, Optional, Union

from sqlalchemy import case, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.models.orchestra_models import FieldType


class AsyncFieldTypeDAO:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _validate_description(self, description: Optional[str]) -> None:
        """Validate description length does not exceed 256 characters.

        Args:
            description: The description to validate

        Raises:
            ValueError: If description exceeds 256 characters
        """
        if description is not None and len(description) > 256:
            raise ValueError("Description cannot exceed 256 characters")

    async def get_by_name_and_context(
        self,
        project_id: int,
        field_name: str,
        context_id: int,
    ) -> Optional[FieldType]:
        """Retrieve a single field type by its name and context."""
        query = select(FieldType).where(
            FieldType.project_id == project_id,
            FieldType.field_name == field_name,
            FieldType.context_id == context_id,
        )
        return await self.session.execute(query).scalars().first()

    async def create_field_type_if_absent(
        self,
        project_id: int,
        field_name: str,
        value,
        context_id: int,
        mutable: bool = False,
        field_category: str = "entry",
        enum_values: Optional[List[str]] = None,
        enum_restrict: bool = False,
        unique: bool = False,
        description: Optional[str] = None,
        field_type: Optional[Union[str, dict]] = None,  # str or JSON schema
        infer_type: bool = True,  # Whether to infer type from value if field_type not provided
    ) -> None:
        """
        Create a field type if it doesn't exist.

        Args:
            field_type: If provided, use this as the field type.
            infer_type: If True and field_type is None, infer type from value.
                       If False and field_type is None, default to "Any".

        Type determination logic:
            1. If field_type is provided → use it (explicit type)
            2. If field_type is None and infer_type=True → infer from value
            3. If field_type is None and infer_type=False → default to "Any"
        """
        self._validate_description(description)

        # First check if a field with this name exists but with a different category
        existing = (
            self.session.query(FieldType)
            .filter(
                FieldType.project_id == project_id,
                FieldType.field_name == field_name,
                FieldType.context_id == context_id,
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

        # Determine the field type based on priority
        from orchestra.db.dao.log_dao import LogDAO
        from orchestra.web.api.log.utils.type_utils import (
            DEFAULT_FIELD_TYPE,
            is_pydantic_schema,
            normalize_pydantic_schema,
            normalize_type_string,
            pydantic_schema_to_string,
        )

        if field_type is not None:
            # Priority 1: Explicit type provided - support str or JSON schema
            # This takes precedence regardless of infer_type value
            if is_pydantic_schema(field_type):
                schema = normalize_pydantic_schema(field_type)
                # Store full schema JSON string
                normalized_type = pydantic_schema_to_string(schema)
            else:
                normalized_type = normalize_type_string(str(field_type))
        elif infer_type and value is not None:
            # Priority 2: No explicit type, but infer_type=True → infer from value
            inferred = LogDAO.infer_type(field_name, value, explicit_type=None)
            normalized_type = normalize_type_string(inferred)
        else:
            # Priority 3: No explicit type and infer_type=False → default to "Any"
            normalized_type = DEFAULT_FIELD_TYPE

        stmt = pg_insert(FieldType).values(
            project_id=project_id,
            field_name=field_name,
            field_type=normalized_type,
            field_category=field_category,
            mutable=mutable,
            context_id=context_id,
            enum_values=enum_values,
            enum_restrict=enum_restrict,
            unique=unique,
            description=description,
        )
        # "on_conflict_do_nothing" will skip insertion if (project_id, field_name, context_id) already exists:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["project_id", "field_name", "context_id"],
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def get_field_types(
        self,
        project_id: Optional[int] = None,
        context_id: Optional[int] = None,
        return_mutable: bool = False,
    ) -> Dict[str, Union[str, Dict[str, Union[str, bool]]]]:
        """Retrieve field types for a specific project ordered by creation time.

        Args:
            project_id: Optional project ID filter
            context_id: Optional context ID filter
            return_mutable: Whether to return additional field metadata

        Returns:
            Dictionary mapping field names to their types or metadata
        """
        query = select(FieldType).order_by(FieldType.id).order_by(FieldType.created_at)

        # Build filters progressively
        if project_id is not None:
            query = query.where(FieldType.project_id == project_id)
        if context_id is not None:
            query = query.where(FieldType.context_id == context_id)

        field_types = await self.session.execute(query).scalars().all()
        from orchestra.web.api.log.utils.type_utils import get_display_type

        if return_mutable:
            return {
                field_type.field_name: {
                    # Present user-facing simple display type
                    "field_type": get_display_type(None, field_type.field_type),
                    "field_category": field_type.field_category,
                    "mutable": field_type.mutable,
                    "unique": field_type.unique,
                    "enum_values": field_type.enum_values,
                    "restrict": field_type.enum_restrict,
                    "description": field_type.description,
                    "created_at": (
                        field_type.created_at.isoformat()
                        if field_type.created_at
                        else None
                    ),
                }
                for field_type in field_types
            }
        else:
            return {
                field_type.field_name: get_display_type(None, field_type.field_type)
                for field_type in field_types
            }

    async def upsert_field_type(
        self,
        project_id: int,
        field_name: str,
        value,
        context_id: int,
        mutable: bool = False,
        field_category: str = "entry",
        enum_values: Optional[List[str]] = None,
        enum_restrict: bool = False,
        unique: bool = False,
        description: Optional[str] = None,
        field_type: Optional[Union[str, dict]] = None,  # str or JSON schema
    ) -> None:
        """
        Upsert approach: insert or overwrite the existing field_type.

        Args:
            field_type: If provided, use this as the field type. If None, defaults to DEFAULT_FIELD_TYPE ("Any").
        """
        self._validate_description(description)

        # First check if a field with this name exists but with a different category
        existing = (
            self.session.query(FieldType)
            .filter(
                FieldType.project_id == project_id,
                FieldType.field_name == field_name,
                FieldType.context_id == context_id,
            )
            .first()
        )
        if existing and existing.field_category != field_category:
            raise ValueError(
                f"Field '{field_name}' already exists as a {existing.field_category}. "
                f"Cannot update it to a {field_category}.",
            )

        # Determine the field type
        if field_type is not None:
            # User provided explicit type - normalize and use it
            from orchestra.web.api.log.utils.type_utils import (
                is_pydantic_schema,
                normalize_pydantic_schema,
                normalize_type_string,
                pydantic_schema_to_string,
            )

            if is_pydantic_schema(field_type):
                schema = normalize_pydantic_schema(field_type)
                normalized_type = pydantic_schema_to_string(schema)
            else:
                normalized_type = normalize_type_string(str(field_type))
        else:
            # No type provided - default to DEFAULT_FIELD_TYPE
            from orchestra.web.api.log.utils.type_utils import DEFAULT_FIELD_TYPE

            normalized_type = DEFAULT_FIELD_TYPE

        stmt = pg_insert(FieldType).values(
            project_id=project_id,
            field_name=field_name,
            field_type=normalized_type,
            field_category=field_category,
            mutable=mutable,
            context_id=context_id,
            enum_values=enum_values,
            enum_restrict=enum_restrict,
            unique=unique,
            description=description,
        )

        # "on_conflict_do_update" to update existing row if it already exists
        stmt = stmt.on_conflict_do_update(
            index_elements=["project_id", "field_name", "context_id"],
            set_={
                "field_type": normalized_type,
                "field_category": field_category,
                "mutable": mutable,
                "enum_values": enum_values,
                "enum_restrict": enum_restrict,
                "unique": unique,
                "description": description,
            },
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def update_field_mutability(
        self,
        project_id: int,
        field_name: str,
        mutable: bool,
        context_id: int,
    ) -> None:
        """Update only the mutability attribute of a field type using an upsert approach.

        Note: For batch operations, use `bulk_update_mutability` to avoid N+1 queries.
        This method executes one query per call.
        """
        # First get the existing field type if it exists
        existing = (
            self.session.query(FieldType)
            .filter(
                FieldType.project_id == project_id,
                FieldType.field_name == field_name,
                FieldType.context_id == context_id,
            )
            .first()
        )

        if not existing:
            raise ValueError(f"Field type {field_name} does not exist")

        existing.mutable = mutable
        await self.session.commit()

    async def bulk_update_mutability(
        self,
        project_id: int,
        context_id: int,
        field_mutability_map: Dict[str, bool],
    ) -> None:
        """
        Batch update mutability for multiple field types in a single operation.

        Args:
            project_id: The project ID
            context_id: The context ID
            field_mutability_map: Dictionary mapping field_name -> mutable (bool)

        Raises:
            ValueError: If any field type does not exist
        """
        if not field_mutability_map:
            return

        # Verify all fields exist first
        field_names = list(field_mutability_map.keys())
        existing_fields = (
            self.session.query(FieldType)
            .filter(
                FieldType.project_id == project_id,
                FieldType.context_id == context_id,
                FieldType.field_name.in_(field_names),
            )
            .all()
        )

        existing_field_names = {f.field_name for f in existing_fields}
        missing_fields = set(field_names) - existing_field_names
        if missing_fields:
            raise ValueError(f"Field types do not exist: {missing_fields}")

        # Build CASE statement for batch update
        # UPDATE field_type SET mutable = CASE
        #   WHEN field_name = 'a' THEN true
        #   WHEN field_name = 'b' THEN false
        #   ...
        # END WHERE project_id = X AND context_id = Y AND field_name IN (...)
        case_conditions = []
        for field_name, mutable in field_mutability_map.items():
            case_conditions.append((FieldType.field_name == field_name, mutable))

        stmt = (
            update(FieldType)
            .where(
                FieldType.project_id == project_id,
                FieldType.context_id == context_id,
                FieldType.field_name.in_(field_names),
            )
            .values(mutable=case(*case_conditions, else_=FieldType.mutable))
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def delete_field_type(
        self,
        project_id: int,
        field_name: str,
        context_id: int,
    ) -> bool:
        """Delete a specific field type for a project.

        Args:
            project_id: The ID of the project containing the field
            field_name: The name of the field to delete
            context_id: The context ID

        Returns:
            bool: True if a field was deleted, False if no field was found

        Raises:
            ValueError: If the field doesn't exist and raise_if_missing is True
        """
        query = select(FieldType).where(
            FieldType.project_id == project_id,
            FieldType.field_name == field_name,
            FieldType.context_id == context_id,
        )
        field_type = await self.session.execute(query).scalars().first()

        if field_type:
            await self.session.delete(field_type)
            await self.session.commit()
        else:
            raise ValueError("Field type does not exist.")

    async def get_ordered_field_names(
        self,
        project_id: Optional[int] = None,
        context_id: Optional[int] = None,
    ) -> Dict[str, int]:
        """Retrieve field names ordered by creation time.

        Args:
            project_id: Optional project ID filter
            context_id: Optional context ID filter

        Returns:
            Dictionary mapping field names to their order index
        """
        query = (
            select(FieldType.field_name)
            .order_by(FieldType.id)
            .order_by(FieldType.created_at)
        )

        # Build filters progressively
        if project_id is not None:
            query = query.where(FieldType.project_id == project_id)
        if context_id is not None:
            query = query.where(FieldType.context_id == context_id)

        result = await self.session.execute(query).scalars().all()
        return {field: i for i, field in enumerate(result)}

    async def rename_field(
        self,
        project_id: int,
        old_field_name: str,
        new_field_name: str,
        context_id: int,
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
                FieldType.context_id == context_id,
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
                FieldType.context_id == context_id,
            )
            .first()
        )

        if existing_field:
            raise ValueError(
                f"Cannot rename field to '{new_field_name}' as it already exists in project {project_id}",
            )

        # Perform the rename
        field_to_rename.field_name = new_field_name
        await self.session.commit()

    async def create_fields(
        self,
        project_id: int,
        context_id: int,
        fields: Dict[str, Union[Dict[str, Any], str, None]],
        description: Optional[str] = None,
    ) -> None:
        """Create field definitions for a context without creating logs.

        Args:
            project_id: The project ID
            context_id: The context ID
            fields: Dictionary mapping fields names to their definitions.
        """
        from orchestra.web.api.log.schema import StandardFieldDefinition

        if not fields:
            return

        # Validate the global description parameter
        self._validate_description(description)

        # Prepare values for bulk insertion
        # Import EnumType for isinstance check
        from orchestra.web.api.log.schema import EnumType
        from orchestra.web.api.log.utils.type_utils import (
            DEFAULT_FIELD_TYPE,
            is_pydantic_schema,
            normalize_pydantic_schema,
            normalize_type_string,
            pydantic_schema_to_string,
        )

        values_to_insert = []
        for field_name, field_info in fields.items():
            field_type = DEFAULT_FIELD_TYPE  # Default to DEFAULT_FIELD_TYPE ("Any")
            mutable = False
            unique = False
            enum_values = None
            enum_restrict = False
            field_description = None

            if isinstance(field_info, EnumType):
                # Handle EnumType separately
                field_type = "enum"
                mutable = True  # Enums need to be mutable to accept different values
                unique = False
                enum_values = field_info.values
                enum_restrict = (
                    field_info.restrict if field_info.restrict is not None else False
                )
                field_description = field_info.description
                # Validate individual field description
                self._validate_description(field_description)
            elif isinstance(field_info, StandardFieldDefinition):
                # field_info.type may be str or JSON schema (dict/JSON string)
                if is_pydantic_schema(field_info.type):
                    schema = normalize_pydantic_schema(field_info.type)
                    field_type = pydantic_schema_to_string(schema)
                else:
                    field_type = field_info.type
                mutable = field_info.mutable
                unique = field_info.unique
                field_description = getattr(field_info, "description", None)
                # Validate individual field description
                self._validate_description(field_description)
                if field_type.lower() == "enum":
                    enum_values = getattr(field_info, "values", None)
                    enum_restrict = getattr(field_info, "restrict", False)
            elif isinstance(field_info, str):
                field_type = field_info
            elif isinstance(field_info, dict) and is_pydantic_schema(field_info):
                schema = normalize_pydantic_schema(field_info)
                field_type = pydantic_schema_to_string(schema)
            elif field_info is None:
                # If None, use default DEFAULT_FIELD_TYPE
                field_type = DEFAULT_FIELD_TYPE

            # Normalize and validate the field type
            from orchestra.web.api.log.utils.type_utils import is_valid_field_type

            normalized_type = (
                pydantic_schema_to_string(normalize_pydantic_schema(field_type))
                if is_pydantic_schema(field_type)
                else normalize_type_string(str(field_type))
            )

            if not is_valid_field_type(normalized_type):
                raise ValueError(f"Invalid field type: {field_type}")

            values_to_insert.append(
                {
                    "project_id": project_id,
                    "field_name": field_name,
                    "field_type": normalized_type,
                    "field_category": "entry",
                    "mutable": mutable,
                    "unique": unique,
                    "context_id": context_id,
                    "enum_values": enum_values,
                    "enum_restrict": enum_restrict,
                    "description": field_description or description,
                },
            )

        # Execute bulk insert with on_conflict_do_update
        if values_to_insert:
            stmt = pg_insert(FieldType).values(values_to_insert)
            stmt = stmt.on_conflict_do_update(
                index_elements=["project_id", "field_name", "context_id"],
                set_={
                    "field_type": stmt.excluded.field_type,
                    "mutable": stmt.excluded.mutable,
                    "unique": stmt.excluded.unique,
                    "enum_values": stmt.excluded.enum_values,
                    "enum_restrict": stmt.excluded.enum_restrict,
                    "description": stmt.excluded.description,
                },
            )
            await self.session.execute(stmt)
            await self.session.commit()

    # Valid field categories for validation
    VALID_FIELD_CATEGORIES = {"entry", "param", "derived_entry"}

    async def bulk_create_field_types(
        self,
        field_types_data: list[dict],
        description: Optional[str] = None,
    ) -> None:
        """Efficiently insert multiple field types at once using a bulk operation.

        Args:
            field_types_data: List of dictionaries, each containing:
                - project_id: The project ID
                - field_name: The name of the field
                - value: The value (not used for type inference anymore)
                - context_id: The context ID
                - mutable: Optional, defaults to False
                - field_category: Optional, defaults to "entry". Valid values are:
                    - "entry": Regular entry fields
                    - "param": Parameter fields
                    - "derived_entry": Derived field values
                - unique: Optional, defaults to False
                - field_type: Optional, the explicit type for this field
                - enum_values: Optional, for enum types
                - enum_restrict: Optional, for enum types

        Note:
            This method is used for field creation from log operations.
            - If field_type is provided (from explicit_types), use it → Strict typing
            - If field_type is not provided → Use "Any" → Untyped field

            Uses PostgreSQL's insert with on_conflict_do_nothing to avoid inserting
            duplicate field types (based on project_id, field_name, and context_id).

        Raises:
            ValueError: If an invalid field_category is provided.
        """
        if not field_types_data:
            return

        # Validate the global description parameter
        self._validate_description(description)

        from orchestra.web.api.log.utils.type_utils import (
            DEFAULT_FIELD_TYPE,
            is_pydantic_schema,
            is_valid_field_type,
            normalize_pydantic_schema,
            normalize_type_string,
            pydantic_schema_to_string,
        )

        # Prepare values for bulk insertion
        values_to_insert = []
        for data in field_types_data:
            project_id = data["project_id"]
            field_name = data["field_name"]
            context_id = data["context_id"]
            mutable = data.get("mutable", False)
            field_category = data.get("field_category", "entry")
            unique = data.get("unique", False)
            field_description = data.get("description", description)

            # Validate field_category
            if field_category not in self.VALID_FIELD_CATEGORIES:
                raise ValueError(
                    f"Invalid field_category '{field_category}' for field '{field_name}'. "
                    f"Valid values are: {', '.join(sorted(self.VALID_FIELD_CATEGORIES))}",
                )

            # Validate individual field description
            self._validate_description(field_description)

            # Type precedence:
            # 1. Explicit type (from explicit_types) → Use it (strict typing)
            # 2. No explicit type → Infer from value using LogDAO.infer_type
            # 3. Inference fails or no value → Fall back to "Any"

            field_type_raw = data.get("field_type")
            value = data.get("value")
            enum_values = data.get("enum_values")
            enum_restrict = data.get("enum_restrict", False)

            if field_type_raw is not None:
                # Priority 1: Explicit type provided - support str or JSON schema
                if is_pydantic_schema(field_type_raw):
                    schema = normalize_pydantic_schema(field_type_raw)
                    field_type = pydantic_schema_to_string(schema)
                else:
                    field_type = normalize_type_string(str(field_type_raw))
                if not is_valid_field_type(field_type):
                    field_type = DEFAULT_FIELD_TYPE
            elif value is not None:
                # Priority 2: Infer type from value
                from orchestra.db.dao.log_dao import LogDAO

                try:
                    inferred = LogDAO.infer_type(field_name, value, explicit_type=None)
                    field_type = normalize_type_string(inferred)
                except Exception:
                    # Fall back to Any if inference fails
                    field_type = DEFAULT_FIELD_TYPE
            else:
                # Priority 3: No explicit type and no value - default to "Any"
                field_type = DEFAULT_FIELD_TYPE

            values_to_insert.append(
                {
                    "project_id": project_id,
                    "field_name": field_name,
                    "field_type": field_type,
                    "field_category": field_category,
                    "mutable": mutable,
                    "context_id": context_id,
                    "unique": unique,
                    "enum_values": enum_values if enum_values else [],
                    "enum_restrict": enum_restrict,
                    "description": field_description,
                },
            )

        # Execute bulk insert with on_conflict_do_nothing
        stmt = pg_insert(FieldType).values(values_to_insert)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["project_id", "field_name", "context_id"],
        )
        await self.session.execute(stmt)
        await self.session.commit()
