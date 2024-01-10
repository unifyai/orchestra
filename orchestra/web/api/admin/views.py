import datetime
from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.datapoint_dao import DatapointDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.license_dao import LicenseDAO
from orchestra.db.dao.metric_dao import MetricDAO
from orchestra.db.dao.modality_dao import ModalityDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.dao.provider_dao import ProviderDAO
from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.recharge_type_dao import RechargeTypeDAO
from orchestra.db.dao.task_dao import TaskDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.models.orchestra_models import Users
from orchestra.web.api.admin.schema import (  # noqa: WPS235
    DatapointModelRequest,
    EndpointModelRequest,
    LicenseModelRequest,
    MetricModelRequest,
    ModalityModelRequest,
    ModelRequest,
    ProviderModelRequest,
    RechargeModelRequest,
    RechargeTypeModelRequest,
    TaskModelRequest,
    UsersModelResponse,
)

router = APIRouter()


@router.get("/get_all_users", response_model=List[UsersModelResponse])
async def get_all_users_models(
    users_dao: UsersDAO = Depends(),
) -> List[Users]:
    """
    Retrieve all users objects from the database.

    :param users_dao: DAO for users models.
    :return: list of users objects from database.
    """
    return await users_dao.get_all_users()


@router.get("/get_user", response_model=List[UsersModelResponse])
async def get_user(
    id: str,  # noqa: WPS125
    users_dao: UsersDAO = Depends(),
) -> List[Users]:
    """
    Retrieve specific users object from the database.

    :param id: id of users instance.
    :param users_dao: DAO for users models.
    :return: list of users objects from database.
    """
    return await users_dao.filter(id=id)


@router.put("/create_datapoint")
async def create_datapoint_model(
    new_datapoint_object: DatapointModelRequest,
    datapoint_dao: DatapointDAO = Depends(),
) -> None:
    """
    Creates datapoint model in the database.

    :param new_datapoint_object: new datapoint model item.
    :param datapoint_dao: DAO for datapoint models.
    """
    await datapoint_dao.create_datapoint(
        endpoint_id=new_datapoint_object.endpoint_id,
        measured_at=new_datapoint_object.measured_at,
        metric_name=new_datapoint_object.metric_name,
        value=new_datapoint_object.value,
    )


@router.put("/create_endpoint")
async def create_endpoint_model(
    new_endpoint_object: EndpointModelRequest,
    endpoint_dao: EndpointDAO = Depends(),
) -> None:
    """
    Creates endpoint model in the database.

    :param new_endpoint_object: new endpoint model item.
    :param endpoint_dao: DAO for endpoint models.
    """
    created_at = datetime.datetime.now()
    await endpoint_dao.create_endpoint(
        mdl_id=new_endpoint_object.mdl_id,
        provider_id=new_endpoint_object.provider_id,
        created_at=created_at,
    )


@router.put("/create_license")
async def create_license_model(
    new_license_object: LicenseModelRequest,
    license_dao: LicenseDAO = Depends(),
) -> None:
    """
    Creates license model in the database.

    :param new_license_object: new license model item.
    :param license_dao: DAO for license models.
    """
    await license_dao.create_license(
        name=new_license_object.name,
        image_url=new_license_object.image_url,
        description=new_license_object.description,
    )


@router.put("/create_metric")
async def create_metric_model(
    new_metric_object: MetricModelRequest,
    metric_dao: MetricDAO = Depends(),
) -> None:
    """
    Creates metric model in the database.

    :param new_metric_object: new metric model item.
    :param metric_dao: DAO for metric models.
    """
    await metric_dao.create_metric(
        name=new_metric_object.name,
        units=new_metric_object.units,
    )


@router.put("/create_modality")
async def create_modality_model(
    new_modality_object: ModalityModelRequest,
    modality_dao: ModalityDAO = Depends(),
) -> None:
    """
    Creates modality model in the database.

    :param new_modality_object: new modality model item.
    :param modality_dao: DAO for modality models.
    """
    await modality_dao.create_modality(
        name=new_modality_object.name,
    )


@router.put("/create_model")
async def create_model(
    new_model_object: ModelRequest,
    model_dao: ModelDAO = Depends(),
) -> None:
    """
    Creates model model in the database.

    :param new_model_object: new model model item.
    :param model_dao: DAO for model models.
    """
    uploaded_at = datetime.datetime.now()
    await model_dao.create_model(
        mdl_code=new_model_object.mdl_code,
        user_id=new_model_object.user_id,
        uploaded_at=uploaded_at,
        task=new_model_object.task,
        description=new_model_object.description,
        license=new_model_object.license,
        input_args_format=new_model_object.input_args_format,
        output_format=new_model_object.output_format,
        custom_fields=new_model_object.custom_fields,
    )


@router.put("/create_provider")
async def create_provider_model(
    new_provider_object: ProviderModelRequest,
    provider_dao: ProviderDAO = Depends(),
) -> None:
    """
    Creates provider model in the database.

    :param new_provider_object: new provider model item.
    :param provider_dao: DAO for provider models.
    """
    await provider_dao.create_provider(
        name=new_provider_object.name,
        image_url=new_provider_object.image_url,
        description=new_provider_object.description,
    )


@router.put("/create_recharge")
async def create_recharge_model(
    new_recharge_object: RechargeModelRequest,
    recharge_dao: RechargeDAO = Depends(),
    user_dao: UsersDAO = Depends(),
) -> None:
    """
    Creates recharge model in the database.

    :param new_recharge_object: new recharge model item.
    :param recharge_dao: DAO for recharge models.
    :param user_dao: DAO for user models.
    """
    at = datetime.datetime.now()
    await user_dao.recharge_credit(
        user_id=new_recharge_object.user_id,
        quantity=new_recharge_object.quantity,
    )

    await recharge_dao.create_recharge(
        at=at,
        user_id=new_recharge_object.user_id,
        quantity=new_recharge_object.quantity,
        type=new_recharge_object.type,
    )


@router.put("/create_recharge_type")
async def create_recharge_type_model(
    new_recharge_type_object: RechargeTypeModelRequest,
    recharge_type_dao: RechargeTypeDAO = Depends(),
) -> None:
    """
    Creates recharge_type model in the database.

    :param new_recharge_type_object: new recharge_type model item.
    :param recharge_type_dao: DAO for recharge_type models.
    """
    await recharge_type_dao.create_recharge_type(
        type=new_recharge_type_object.type,
    )


@router.put("/create_task")
async def create_task_model(
    new_task_object: TaskModelRequest,
    task_dao: TaskDAO = Depends(),
) -> None:
    """
    Creates task model in the database.

    :param new_task_object: new task model item.
    :param task_dao: DAO for task models.
    """
    await task_dao.create_task(
        name=new_task_object.name,
        modality=new_task_object.modality,
    )
