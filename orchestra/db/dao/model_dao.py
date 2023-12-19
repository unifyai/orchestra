import datetime
from typing import List, Optional

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Model


class ModelDAO:
    """Class for accessing model table."""

    def __init__(self, session: AsyncSession = Depends(get_db_session)):
        self.session = session

    async def create_model(  # noqa: WPS211
        self,
        model_code: str,
        user_id: str,
        uploaded_at: datetime.datetime,
        task: str,
        description: str,
        license: str,
        input_args_format: str,
        output_format: str,
        custom_fields: str,
    ) -> None:
        """
        Add single model to session.

        :param model_code: model_code of a model.
        :param user_id: user_id of a model.
        :param uploaded_at: uploaded_at of a model.
        :param task: task of a model.
        :param description: description of a model.
        :param license: license of a model.
        :param input_args_format: input_args_format of a model.
        :param output_format: output_format of a model.
        :param custom_fields: custom_fields of a model.
        """
        self.session.add(
            Model(
                model_code=model_code,
                user_id=user_id,
                uploaded_at=uploaded_at,
                task=task,
                description=description,
                license=license,
                input_args_format=input_args_format,
                output_format=output_format,
                custom_fields=custom_fields,
            ),
        )

    async def get_all_models(self, limit: int, offset: int) -> List[Model]:
        """
        Get all model models with limit/offset pagination.

        :param limit: limit of models.
        :param offset: offset of models.
        :return: stream of models.
        """
        raw_models = await self.session.execute(
            select(Model).limit(limit).offset(offset),
        )

        return list(raw_models.scalars().fetchall())

    async def filter(  # noqa: WPS211, C901
        self,
        model_code: Optional[str] = None,
        user_id: Optional[str] = None,
        uploaded_at: Optional[datetime.datetime] = None,
        task: Optional[str] = None,
        description: Optional[str] = None,
        license: Optional[str] = None,
        input_args_format: Optional[str] = None,
        output_format: Optional[str] = None,
        custom_fields: Optional[str] = None,
    ) -> List[Model]:
        """
        Get specific model model.

        :param model_code: model_code of model instance.
        :param user_id: user_id of model instance.
        :param uploaded_at: uploaded_at of model instance.
        :param task: task of model instance.
        :param description: description of model instance.
        :param license: license of model instance.
        :param input_args_format: input_args_format of model instance.
        :param output_format: output_format of model instance.
        :param custom_fields: custom_fields of model instance.
        :return: model models.
        """
        query = select(Model)
        if model_code:
            query = query.where(Model.model_code == model_code)
        if user_id:
            query = query.where(Model.user_id == user_id)
        if uploaded_at:
            query = query.where(Model.uploaded_at == uploaded_at)
        if task:
            query = query.where(Model.task == task)
        if description:
            query = query.where(Model.description == description)
        if license:
            query = query.where(Model.license == license)
        if input_args_format:
            query = query.where(Model.input_args_format == input_args_format)
        if output_format:
            query = query.where(Model.output_format == output_format)
        if custom_fields:
            query = query.where(Model.custom_fields == custom_fields)
        rows = await self.session.execute(query)
        return list(rows.scalars().fetchall())
