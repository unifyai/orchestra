import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.param_functions import Depends
from google.cloud.storage import Client

from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.db.dao.credit_card_fingerprint import CreditCardFingerprintDAO
from orchestra.db.dao.custom_router_dao import CustomRouterDAO
from orchestra.db.dao.datapoint_dao import DatapointDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.metric_dao import MetricDAO
from orchestra.db.dao.modality_dao import ModalityDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.provider_dao import ProviderDAO
from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.recharge_type_dao import RechargeTypeDAO
from orchestra.db.dao.task_dao import TaskDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (  # noqa: WPS235
    BenchmarkRun,
    CreditCardFingerprint,
    Datapoint,
    Endpoint,
    Metric,
    Modality,
    Recharge,
    RechargeType,
    Task,
    Users,
)
from orchestra.lib.billing import credits_to_usd
from orchestra.web.api.admin.schema import (  # noqa: WPS235
    BenchmarkRunModelResponse,
    CreditCardFingerprintModelResponse,
    CustomRouterRequest,
    DatapointModelRequest,
    DatapointModelResponse,
    DatasetEvaluationModelRequest,
    DemoModelRequest,
    EndpointModelRequest,
    EndpointModelResponse,
    FileWriteRequest,
    MetricModelRequest,
    MetricModelResponse,
    ModalityModelRequest,
    ModalityModelResponse,
    ModelRequest,
    ProviderModelRequest,
    RechargeModelRequest,
    RechargeModelResponse,
    RechargeTypeModelRequest,
    RechargeTypeModelResponse,
    TaskModelRequest,
    TaskModelResponse,
    UsersModelResponse,
)
from orchestra.web.api.assistant.schema import AssistantRead, InfoResponse
from orchestra.web.api.assistant.views import normalize_phone_parameter

router = APIRouter()


@router.get("/get_all_users", response_model=List[UsersModelResponse])
def get_all_users_models(
    session=Depends(get_db_session),
) -> List[Users]:
    """
    Retrieve all users objects from the database.

    :param users_dao: DAO for users models.
    :return: list of users objects from database.
    """
    users_dao = UsersDAO(session)
    return users_dao.get_all_users()


@router.get("/get_user", response_model=List[UsersModelResponse])
def get_user(
    id: str,  # noqa: WPS125
    session=Depends(get_db_session),
) -> List[Users]:
    """
    Retrieve specific users object from the database.

    :param id: id of users instance.
    :param users_dao: DAO for users models.
    :return: list of users objects from database.
    """
    users_dao = UsersDAO(session)
    return users_dao.filter(id=id)


@router.get("/get_all_recharge_types", response_model=List[RechargeTypeModelResponse])
def get_recharge_type_models(
    limit: int = 10,
    offset: int = 0,
    session=Depends(get_db_session),
) -> List[RechargeType]:
    """
    Retrieve all recharge_type objects from the database.

    :param limit: limit of recharge_type objects, defaults to 10.
    :param offset: offset of recharge_type objects, defaults to 0.
    :param recharge_type_dao: DAO for recharge_type models.
    :return: list of recharge_type objects from database.
    """
    recharge_type_dao = RechargeTypeDAO(session)
    return recharge_type_dao.get_all_recharge_types(limit=limit, offset=offset)


@router.get("/get_recharge_type", response_model=List[RechargeTypeModelResponse])
def get_recharge_type(
    type: str,  # noqa: WPS125
    session=Depends(get_db_session),
) -> List[RechargeType]:
    """
    Retrieve specific recharge_type object from the database.

    :param type: type of recharge_type object.
    :param recharge_type_dao: DAO for recharge_type models.
    :return: recharge_type object from database.
    """
    recharge_type_dao = RechargeTypeDAO(session)
    return recharge_type_dao.filter(type=type)


@router.get("/get_all_recharges", response_model=List[RechargeModelResponse])
def get_recharge_models(
    limit: int = 10,
    offset: int = 0,
    session=Depends(get_db_session),
) -> List[Recharge]:
    """
    Retrieve all recharge objects from the database.

    :param limit: limit of recharge objects, defaults to 10.
    :param offset: offset of recharge objects, defaults to 0.
    :param recharge_dao: DAO for recharge models.
    :return: list of recharge objects from database.
    """
    recharge_dao = RechargeDAO(session)
    return recharge_dao.get_all_recharges(limit=limit, offset=offset)


@router.get("/get_recharge", response_model=List[RechargeModelResponse])
def get_recharge(  # noqa: WPS211
    id: Optional[int] = None,  # noqa: WPS125
    at: Optional[datetime] = None,
    user_id: Optional[str] = None,
    quantity: Optional[int] = None,
    type: Optional[str] = None,  # noqa: WPS125
    session=Depends(get_db_session),
) -> List[Recharge]:
    """
    Retrieve specific recharge object from the database.

    :param id: id of recharge instance.
    :param at: at of recharge instance.
    :param user_id: user_id of recharge instance.
    :param quantity: quantity of recharge instance.
    :param type: type of recharge instance.
    :param recharge_dao: DAO for recharge models.
    :return: list of recharge objects from database.
    """
    recharge_dao = RechargeDAO(session)
    return recharge_dao.filter(
        id=id,
        at=at,
        user_id=user_id,
        quantity=quantity,
        type=type,
    )


@router.get("/get_all_benchmark_runs", response_model=List[BenchmarkRunModelResponse])
def get_benchmark_run_models(
    limit: int = 10,
    offset: int = 0,
    session=Depends(get_db_session),
) -> List[BenchmarkRun]:
    """
    Retrieve all benchmark_run objects from the database.

    :param limit: limit of benchmark_run objects, defaults to 10.
    :param offset: offset of benchmark_run objects, defaults to 0.
    :param benchmark_run_dao: DAO for benchmark_run models.
    :return: list of benchmark_run objects from database.
    """
    benchmark_run_dao = BenchmarkRunDAO(session)
    return benchmark_run_dao.get_all_benchmark_runs(limit=limit, offset=offset)


@router.get("/get_benchmark_run", response_model=List[BenchmarkRunModelResponse])
def get_benchmark_run(  # noqa: WPS211
    id: Optional[int] = None,  # noqa: WPS125
    endpoint_id: Optional[int] = None,
    regime: Optional[str] = None,
    region: Optional[str] = None,
    seq_len: Optional[str] = None,
    measured_at: Optional[datetime] = None,
    session=Depends(get_db_session),
) -> List[BenchmarkRun]:
    """
    Retrieve specific benchmark_run object from the database.

    :param id: id of benchmark_run object.
    :param endpoint_id: endpoint_id of benchmark_run object.
    :param regime: regime of benchmark_run object.
    :param region: region of benchmark_run object.
    :param seq_len: seq_len of benchmark_run object.
    :param measured_at: measured_at of benchmark_run object.
    :param benchmark_run_dao: DAO for benchmark_run models.
    :return: benchmark_run object from database.
    """
    benchmark_run_dao = BenchmarkRunDAO(session)
    return benchmark_run_dao.filter(
        id=id,
        endpoint_id=endpoint_id,
        regime=regime,
        region=region,
        seq_len=seq_len,
        measured_at=measured_at,
    )


@router.get("/get_all_datapoints", response_model=List[DatapointModelResponse])
def get_datapoint_models(
    limit: int = 10,
    offset: int = 0,
    session=Depends(get_db_session),
) -> List[Datapoint]:
    """
    Retrieve all datapoint objects from the database.

    :param limit: limit of datapoint objects, defaults to 10.
    :param offset: offset of datapoint objects, defaults to 0.
    :param datapoint_dao: DAO for datapoint models.
    :return: list of datapoint objects from database.
    """
    datapoint_dao = DatapointDAO(session)
    return datapoint_dao.get_all_datapoints(limit=limit, offset=offset)


@router.get("/get_datapoint", response_model=List[DatapointModelResponse])
def get_datapoint(  # noqa: WPS211
    id: Optional[int] = None,  # noqa: WPS125
    benchmark_run_id: Optional[int] = None,
    metric_name: Optional[str] = None,
    value: Optional[float] = None,
    measured_at: Optional[datetime] = None,
    session=Depends(get_db_session),
) -> List[Datapoint]:
    """
    Retrieve specific datapoint object from the database.

    :param id: id of datapoint object.
    :param benchmark_run_id: benchmark_run_id of datapoint object.
    :param metric_name: metric_name of datapoint object.
    :param value: value of datapoint object.
    :param measured_at: measured_at of datapoint object.
    :param datapoint_dao: DAO for datapoint models.
    :return: datapoint object from database.
    """
    datapoint_dao = DatapointDAO(session)
    return datapoint_dao.filter(
        id=id,
        benchmark_run_id=benchmark_run_id,
        metric_name=metric_name,
        value=value,
        measured_at=measured_at,
    )


@router.get("/get_all_endpoints_raw", response_model=List[EndpointModelResponse])
def get_endpoint_models(
    limit: int = 10,
    offset: int = 0,
    session=Depends(get_db_session),
) -> List[Endpoint]:
    """
    Retrieve all endpoint objects from the database.

    :param limit: limit of endpoint objects, defaults to 10.
    :param offset: offset of endpoint objects, defaults to 0.
    :param endpoint_dao: DAO for endpoint models.
    :return: list of endpoint objects from database.
    """
    endpoint_dao = EndpointDAO(session)
    return endpoint_dao.get_all_endpoints_raw(limit=limit, offset=offset)


@router.get("/get_endpoint", response_model=List[EndpointModelResponse])
def get_endpoint(
    id: Optional[int] = None,  # noqa: WPS125
    mdl_id: Optional[int] = None,
    provider_id: Optional[int] = None,
    created_at: Optional[datetime] = None,
    session=Depends(get_db_session),
) -> List[Endpoint]:
    """
    Retrieve specific endpoint object from the database.

    :param id: id of endpoint object.
    :param mdl_id: mdl_id of endpoint object.
    :param provider_id: provider_id of endpoint object.
    :param created_at: created_at of endpoint object.
    :param endpoint_dao: DAO for endpoint models.
    :return: endpoint object from database.
    """
    endpoint_dao = EndpointDAO(session)
    return endpoint_dao.filter(
        id=id,
        mdl_id=mdl_id,
        provider_id=provider_id,
        created_at=created_at,
    )


@router.get("/get_all_metrics", response_model=List[MetricModelResponse])
def get_metric_models(
    limit: int = 10,
    offset: int = 0,
    session=Depends(get_db_session),
) -> List[Metric]:
    """
    Retrieve all metric objects from the database.

    :param limit: limit of metric objects, defaults to 10.
    :param offset: offset of metric objects, defaults to 0.
    :param metric_dao: DAO for metric models.
    :return: list of metric objects from database.
    """
    metric_dao = MetricDAO(session)
    return metric_dao.get_all_metrics(limit=limit, offset=offset)


@router.get("/get_metric", response_model=List[MetricModelResponse])
def get_metric(  # noqa: WPS211
    name: str,
    units: str,
    display_name: str,
    tooltip: str,
    priority: int,
    plottable: bool,
    session=Depends(get_db_session),
) -> List[Metric]:
    """
    Retrieve specific metric object from the database.

    :param name: name of metric instance.
    :param units: units of metric instance.
    :param display_name: display_name of metric instance.
    :param tooltip: tooltip of metric instance.
    :param priority: priority of metric instance.
    :param plottable: plottable of metric instance.
    :param metric_dao: DAO for metric models.
    :return: list of metric objects from database.
    """
    metric_dao = MetricDAO(session)
    return metric_dao.filter(
        name=name,
        units=units,
        display_name=display_name,
        tooltip=tooltip,
        priority=priority,
        plottable=plottable,
    )


@router.get("/get_all_modalities", response_model=List[ModalityModelResponse])
def get_modality_models(
    limit: int = 10,
    offset: int = 0,
    session=Depends(get_db_session),
) -> List[Modality]:
    """
    Retrieve all modality objects from the database.

    :param limit: limit of modality objects, defaults to 10.
    :param offset: offset of modality objects, defaults to 0.
    :param modality_dao: DAO for modality models.
    :return: list of modality objects from database.
    """
    modality_dao = ModalityDAO(session)
    return modality_dao.get_all_modalities(limit=limit, offset=offset)


@router.get("/get_modality", response_model=List[ModalityModelResponse])
def get_modality(
    name: str,
    session=Depends(get_db_session),
) -> List[Modality]:
    """
    Retrieve specific modality object from the database.

    :param name: name of modality object.
    :param modality_dao: DAO for modality models.
    :return: modality object from database.
    """
    modality_dao = ModalityDAO(session)
    return modality_dao.filter(name=name)


@router.get("/get_all_tasks", response_model=List[TaskModelResponse])
def get_task_models(
    limit: int = 10,
    offset: int = 0,
    session=Depends(get_db_session),
) -> List[Task]:
    """
    Retrieve all task objects from the database.

    :param limit: limit of task objects, defaults to 10.
    :param offset: offset of task objects, defaults to 0.
    :param task_dao: DAO for task models.
    :return: list of task objects from database.
    """
    task_dao = TaskDAO(session)
    return task_dao.get_all_tasks(limit=limit, offset=offset)


@router.get("/get_task", response_model=List[TaskModelResponse])
def get_task(
    name: str,
    session=Depends(get_db_session),
) -> List[Task]:
    """
    Retrieve specific task object from the database.

    :param name: name of task object.
    :param task_dao: DAO for task models.
    :return: task object from database.
    """
    task_dao = TaskDAO(session)
    return task_dao.filter(name=name)


@router.get(
    "/assistant",
    response_model=InfoResponse[List[AssistantRead]],
    summary="Admin: list all assistants",
    description="Retrieve every assistant in the system, optionally filtered by phone or email.",
)
def admin_list_assistants(
    phone: Optional[str] = Query(
        None,
        description="Only return assistants whose phone number matches this E.164-style value (leading '+' is URL-encoded).",
    ),
    email: Optional[str] = Query(
        None,
        description="Only return assistants whose email address matches this value.",
    ),
    session=Depends(get_db_session),
) -> InfoResponse[List[AssistantRead]]:
    """
    List all assistants in the system with optional filtering by phone or email.

    This admin endpoint returns all assistants across all users, with optional
    filtering capabilities for phone numbers and email addresses.
    """
    # Normalize phone parameter to handle URL-decoded '+' characters
    phone = normalize_phone_parameter(phone)

    dao = AssistantDAO(session)
    try:
        assistants = dao.list_all_assistants(phone=phone, email=email)
        return InfoResponse(
            info=[
                AssistantRead(
                    agent_id=str(a.agent_id),
                    first_name=a.first_name,
                    surname=a.surname,
                    age=a.age,
                    region=a.region,
                    profile_photo=a.profile_photo,
                    about=a.about,
                    weekly_limit=float(a.weekly_limit),
                    max_parallel=a.max_parallel,
                    created_at=a.created_at,
                    updated_at=a.updated_at,
                    phone=a.phone,
                    email=a.email,
                    whatsapp_sid=a.whatsapp_sid,
                    voice_id=a.voice_id,
                )
                for a in assistants
            ],
        )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error fetching assistants: {str(e)}",
        )


@router.put("/create_datapoint")
def create_datapoint_model(
    new_datapoint_object: DatapointModelRequest,
    session=Depends(get_db_session),
) -> None:
    """
    Creates datapoint model in the database.

    :param new_datapoint_object: new datapoint model item.
    :param datapoint_dao: DAO for datapoint models.
    """
    datapoint_dao = DatapointDAO(session)
    datapoint_dao.create_datapoint(
        benchmark_run_id=new_datapoint_object.benchmark_run_id,
        measured_at=new_datapoint_object.measured_at,
        metric_name=new_datapoint_object.metric_name,
        value=new_datapoint_object.value,
        tooltip=new_datapoint_object.tooltip,
    )


@router.put("/create_endpoint")
def create_endpoint_model(
    new_endpoint_object: EndpointModelRequest,
    session=Depends(get_db_session),
) -> None:
    """
    Creates endpoint model in the database.

    :param new_endpoint_object: new endpoint model item.
    :param endpoint_dao: DAO for endpoint models.
    """
    created_at = datetime.now(timezone.utc)
    endpoint_dao = EndpointDAO(session)
    endpoint_dao.create_endpoint(
        mdl_id=new_endpoint_object.mdl_id,
        provider_id=new_endpoint_object.provider_id,
        created_at=created_at,
    )


@router.put("/create_metric")
def create_metric_model(
    new_metric_object: MetricModelRequest,
    session=Depends(get_db_session),
) -> None:
    """
    Creates metric model in the database.

    :param new_metric_object: new metric model item.
    :param metric_dao: DAO for metric models.
    """
    metric_dao = MetricDAO(session)
    metric_dao.create_metric(
        name=new_metric_object.name,
        units=new_metric_object.units,
        display_name=new_metric_object.display_name,
        tooltip=new_metric_object.tooltip,
        priority=new_metric_object.priority,
        plottable=new_metric_object.plottable,
    )


@router.put("/create_modality")
def create_modality_model(
    new_modality_object: ModalityModelRequest,
    session=Depends(get_db_session),
) -> None:
    """
    Creates modality model in the database.

    :param new_modality_object: new modality model item.
    :param modality_dao: DAO for modality models.
    """
    modality_dao = ModalityDAO(session)
    modality_dao.create_modality(
        name=new_modality_object.name,
    )


@router.put("/create_model")
def create_model(
    new_model_object: ModelRequest,
    session=Depends(get_db_session),
) -> None:
    """
    Creates model model in the database.

    :param new_model_object: new model model item.
    :param model_dao: DAO for model models.
    """
    uploaded_at = datetime.now(timezone.utc)
    model_dao = ModelDAO(session)
    model_dao.create_model(
        mdl_code=new_model_object.mdl_code,
        uploaded_at=uploaded_at,
        task=new_model_object.task,
        active=new_model_object.active,
    )


@router.put("/update_model")
def update_model(  # noqa: WPS211
    id: int,  # noqa: WPS125
    mdl_code: Optional[str] = None,
    uploaded_at: Optional[datetime] = None,
    task: Optional[str] = None,
    active: Optional[bool] = None,
    session=Depends(get_db_session),
) -> None:
    """
    Update specific model model.

    :param id: id of model instance.
    :param mdl_code: mdl_code of model instance.
    :param uploaded_at: uploaded_at of model instance.
    :param task: task of model instance.
    :param active: is model instance active.
    :param model_dao: DAO for model models.
    """
    model_dao = ModelDAO(session)
    model_dao.update_model(
        id=id,
        mdl_code=mdl_code,
        uploaded_at=uploaded_at,
        task=task,
        active=active,
    )


@router.put("/create_provider")
def create_provider_model(
    new_provider_object: ProviderModelRequest,
    session=Depends(get_db_session),
) -> None:
    """
    Creates provider model in the database.

    :param new_provider_object: new provider model item.
    :param provider_dao: DAO for provider models.
    """
    provider_dao = ProviderDAO(session)
    provider_dao.create_provider(
        name=new_provider_object.name,
        image_url=new_provider_object.image_url,
        description=new_provider_object.description,
    )


@router.post("/create_recharge")
def create_recharge_model(
    new_recharge_object: RechargeModelRequest,
    session=Depends(get_db_session),
) -> None:
    """
    Creates recharge model in the database.

    :param new_recharge_object: new recharge model item.
    :param recharge_dao: DAO for recharge models.
    :param user_dao: DAO for user models.
    """
    recharge_dao = RechargeDAO(session)
    user_dao = UsersDAO(session)
    if (
        new_recharge_object.type == "payment"
        and new_recharge_object.transaction_id is None
    ):
        raise HTTPException(
            status_code=400,
            detail="Transaction id must be specified when adding a payment.",
        )

    at = datetime.now(timezone.utc)
    user_dao.recharge_credit(
        user_id=new_recharge_object.user_id,
        quantity=new_recharge_object.quantity,
    )

    # Calculate amount_usd and invoice_group for the new billing system
    amount_usd = credits_to_usd(int(new_recharge_object.quantity))
    # Use month-end date for invoice grouping
    first_next_month = (at.replace(day=1) + timedelta(days=32)).replace(day=1)
    invoice_group = (first_next_month - timedelta(microseconds=1)).date()

    recharge_dao.create_recharge(
        user_id=new_recharge_object.user_id,
        quantity=int(new_recharge_object.quantity),
        amount_usd=amount_usd,
        invoice_group=invoice_group,
        type_=new_recharge_object.type,
        transaction_id=new_recharge_object.transaction_id,
    )


@router.put("/create_recharge_type")
def create_recharge_type_model(
    new_recharge_type_object: RechargeTypeModelRequest,
    session=Depends(get_db_session),
) -> None:
    """
    Creates recharge_type model in the database.

    :param new_recharge_type_object: new recharge_type model item.
    :param recharge_type_dao: DAO for recharge_type models.
    """
    recharge_type_dao = RechargeTypeDAO(session)
    recharge_type_dao.create_recharge_type(
        type=new_recharge_type_object.type,
    )


@router.put("/create_task")
def create_task_model(
    new_task_object: TaskModelRequest,
    session=Depends(get_db_session),
) -> None:
    """
    Create new task model in the database.

    :param new_task_object: new task model item.
    :param task_dao: DAO for task models.
    """
    task_dao = TaskDAO(session)
    task_dao.create_task_model(
        name=new_task_object.name,
    )


@router.put("/update_benchmark_run")
def update_benchmark_run(  # noqa: WPS211
    id: int,  # noqa: WPS125
    endpoint_id: Optional[int] = None,
    regime: Optional[str] = None,
    region: Optional[str] = None,
    seq_len: Optional[str] = None,
    measured_at: Optional[datetime] = None,
    session=Depends(get_db_session),
) -> None:
    """
    Update specific benchmark_run model.

    :param id: id of benchmark_run instance.
    :param endpoint_id: endpoint_id of benchmark_run instance.
    :param regime: regime of benchmark_run instance.
    :param region: region of benchmark_run instance.
    :param seq_len: seq_len of benchmark_run instance.
    :param measured_at: measured_at of benchmark_run instance.
    :param benchmark_run_dao: DAO for benchmark_run models.
    """
    benchmark_run_dao = BenchmarkRunDAO(session)
    benchmark_run_dao.update_benchmark_run(
        id=id,
        endpoint_id=endpoint_id,
        regime=regime,
        region=region,
        seq_len=seq_len,
        measured_at=measured_at,
    )


@router.put("/update_datapoint")
def update_datapoint(  # noqa: WPS211
    id: int,  # noqa: WPS125
    benchmark_run_id: Optional[int] = None,
    metric_name: Optional[str] = None,
    value: Optional[float] = None,
    tooltip: Optional[str] = None,
    measured_at: Optional[datetime] = None,
    session=Depends(get_db_session),
) -> None:
    """
    Update specific datapoint model.

    :param id: id of datapoint instance.
    :param benchmark_run_id: benchmark_run_id of datapoint instance.
    :param metric_name: metric_name of datapoint instance.
    :param value: value of datapoint instance.
    :param tooltip: tooltip of datapoint instance.
    :param measured_at: measured_at of datapoint instance.
    :param datapoint_dao: DAO for datapoint models.
    """
    datapoint_dao = DatapointDAO(session)
    datapoint_dao.update_datapoint(
        id=id,
        benchmark_run_id=benchmark_run_id,
        metric_name=metric_name,
        value=value,
        tooltip=tooltip,
        measured_at=measured_at,
    )


@router.put("/stripe_customer_id")
def update_user_stripe_customer_id(  # noqa: WPS211
    id: str,  # noqa: WPS125
    stripe_customer_id: str,
    session=Depends(get_db_session),
) -> None:
    """
    Update the stripe customer id of a user.

    :param id: id of the user to be updated.
    :param stripe_customer_id: stripe customer id.
    :param users_dao: DAO for users models.
    """
    users_dao = UsersDAO(session)
    users_dao.set_stripe_customer_id(user_id=id, stripe_id=stripe_customer_id)
    users_dao.session.commit()


@router.put("/enable_autorecharge")
def update_user_autorecharge(  # noqa: WPS211
    id: str,  # noqa: WPS125
    enable: bool,
    session=Depends(get_db_session),
) -> None:
    """
    Update the autorecharge status of a user.

    :param id: id of the user to be updated.
    :param enable: whether to enable or disable autorecharge.
    :param users_dao: DAO for users models.
    """
    users_dao = UsersDAO(session)
    try:
        users_dao.enable_autorecharge(user_id=id, enable=enable)
        users_dao.session.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException as e:
        # Re-raise HTTPExceptions (like 404 for user not found)
        raise e


@router.put("/autorecharge_threshold")
def update_user_autorecharge_threshold(  # noqa: WPS211
    id: str,  # noqa: WPS125
    threshold: float,
    session=Depends(get_db_session),
) -> None:
    """
    Update the autorecharge threshold of a user.

    :param id: id of the user to be updated.
    :param threshold: new autorecharge threshold.
    :param users_dao: DAO for users models.
    """
    users_dao = UsersDAO(session)
    users_dao.set_autorecharge_threshold(user_id=id, threshold=threshold)
    users_dao.session.commit()


@router.put("/autorecharge_qty")
def update_user_autorecharge_qty(  # noqa: WPS211
    id: str,  # noqa: WPS125
    qty: float,
    session=Depends(get_db_session),
) -> None:
    """
    Update the autorecharge quantity of a user.

    :param id: id of the user to be updated.
    :param qty: new autorecharge quantity.
    :param users_dao: DAO for users models.
    """
    users_dao = UsersDAO(session)
    try:
        users_dao.set_autorecharge_qty(user_id=id, qty=qty)
        users_dao.session.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException as e:
        # Re-raise HTTPExceptions (like 404 for user not found)
        raise e


@router.put("/create_custom_router")
def create_custom_router(
    custom_router_object: CustomRouterRequest,
    session=Depends(get_db_session),
) -> None:
    """
    Creates a custom router in the database.
    """
    custom_router_dao = CustomRouterDAO(session)
    custom_router_dao.create_custom_router(
        user_id=custom_router_object.user_id,
        router_name=custom_router_object.router_name,
        router_id=custom_router_object.router_id,
    )


@router.put("/update_user_prompt_telemetry")
def update_user_prompt_telemetry(
    user_id: str,
    activated: bool,
    session=Depends(get_db_session),
) -> None:
    """
    Updates database evaluation model in the database.
    """
    users_dao = UsersDAO(session)
    users_dao.set_prompt_telemetry(user_id, activated)


@router.get("/user_prompt_telemetry")
def get_user_prompt_telemetry(
    user_id: str,
    session=Depends(get_db_session),
) -> bool:
    """
    Returns state of the store prompts attr for a given user.
    """
    users_dao = UsersDAO(session)
    return users_dao.is_telemetry_activated(user_id)


@router.post("/credit_card_fingerprint")
def create_credit_card_fingerprint(
    user_id: str,
    fingerprint: str,
    session=Depends(get_db_session),
) -> None:
    """
    Creates a credit card fingerprint entry in the database.
    """
    credit_card_fingerprint_dao = CreditCardFingerprintDAO(session)
    credit_card_fingerprint_dao.create(user_id, fingerprint)


@router.get("/duplicated_credit_card_fingerprint")
def duplicated_credit_card_fingerprint(
    user_id: str,
    fingerprint: str,
    session=Depends(get_db_session),
) -> bool:
    """
    Creates a credit card fingerprint entry in the database.
    """
    credit_card_fingerprint_dao = CreditCardFingerprintDAO(session)
    results = credit_card_fingerprint_dao.filter(fingerprint=fingerprint)
    results = [r for r in results if r.user_id != user_id]
    if len(results) > 0:
        return True
    return False


@router.get(
    "/credit_card_fingerprint",
    response_model=List[CreditCardFingerprintModelResponse],
)
def get_credit_card_fingerprint(
    user_id: str,
    session=Depends(get_db_session),
) -> List[CreditCardFingerprint]:
    """
    Returns the credit card fingerprints entry in the database matching a user id.
    """
    credit_card_fingerprint_dao = CreditCardFingerprintDAO(session)
    return credit_card_fingerprint_dao.filter(user_id=user_id)


@router.post("/run_demo")
def run_demo(
    demo_object: DemoModelRequest,
    session=Depends(get_db_session),
) -> None:
    """
    Run a given demo for the user in an isolated process.
    """
    api_key_dao = ApiKeyDAO(session)
    api_key = api_key_dao.filter(user_id=demo_object.user_id)
    if not api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        # Create a temporary file with the demo code
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py") as tf:
            tf.write(demo_object.code)
            tf.flush()

            # Run the code in a separate process with its own environment
            env = dict(os.environ)
            env["UNIFY_KEY"] = api_key[0][0].key

            if demo_object.staging:
                env[
                    "UNIFY_BASE_URL"
                ] = "https://orchestra-staging-lz5fmz6i7q-ew.a.run.app/v0"

            # This will block until the subprocess completes
            result = subprocess.run(
                [sys.executable, tf.name],
                env=env,
                capture_output=True,
                text=True,
                check=True,  # Raises CalledProcessError if return code != 0
            )

            return {
                "info": "Demo run successfully",
                "stdout": result.stdout,
                "stderr": result.stderr,
            }

    except subprocess.TimeoutExpired as e:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Timeout",
                "timeout": e.timeout,
                "stdout": e.stdout.decode() if e.stdout else None,
                "stderr": e.stderr.decode() if e.stderr else None,
            },
        )
    except subprocess.CalledProcessError as e:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Process Error",
                "return_code": e.returncode,
                "stdout": e.stdout,
                "stderr": e.stderr,
                "cmd": e.cmd,
            },
        )
    except Exception as e:
        # Catch any other exceptions and include full traceback
        import traceback

        raise HTTPException(
            status_code=500,
            detail={
                "error": str(e),
                "error_type": type(e).__name__,
                "traceback": traceback.format_exc(),
            },
        )


@router.post(
    "/file",
    responses={
        200: {
            "description": "File uploaded successfully",
            "content": {
                "application/json": {
                    "example": {
                        "message": "File uploaded successfully",
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
    },
)
def write_files(
    request: FileWriteRequest,
    session=Depends(get_db_session),
):
    """
    Write/Update files to the Google Cloud Storage bucket.
    The files will be stored at <user-id>/<project>/<path>
    """
    project_dao = ProjectDAO(session)
    project = project_dao.get_by_user_and_name(
        user_id=request.user_id,
        name=request.project,
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {request.project} not found.",
        )

    try:
        # Initialize the Google Cloud Storage client
        client = Client()
        bucket = client.bucket(
            (
                "interface-file-system-staging"
                if request.staging
                else "interface-file-system"
            ),
        )

        # Construct the full path in the bucket
        for file_path, file_content in request.files.items():
            full_path = f"{request.user_id}/{project.name}/{file_path}"

            # Create a new blob and upload the file contents
            blob = bucket.blob(full_path)
            blob.upload_from_string(file_content)

        return {
            "message": "Files uploaded successfully",
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to upload file: {str(e)}",
        )


@router.get(
    "/file",
    responses={
        200: {
            "description": "List of files retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "123/my-project/file1.txt": "Hello, world!",
                        "123/my-project/folder/file2.txt": "Hello, world!",
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
    },
)
def get_files(
    user_id: str,
    project: str,
    staging: bool = False,
    session=Depends(get_db_session),
):
    """
    Get all files in a user's project folder in the bucket.
    Returns a flat list of file paths and contents.
    """
    project_dao = ProjectDAO(session)
    project_obj = project_dao.get_by_user_and_name(
        user_id=user_id,
        name=project,
    )
    if not project_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project} not found.",
        )

    try:
        # Initialize the Google Cloud Storage client
        client = Client()
        bucket = client.bucket(
            "interface-file-system-staging" if staging else "interface-file-system",
        )

        # Construct the prefix to list files under
        prefix = f"{user_id}/{project_obj.name}/"

        # List all blobs under the prefix
        blobs = bucket.list_blobs(prefix=prefix)

        # Extract the full paths and contents
        files = dict()
        for blob in blobs:
            # Download the content of each file
            content = blob.download_as_text() if not blob.name.endswith("/") else ""
            files[blob.name.replace(prefix, "")] = content

        return files
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get files: {str(e)}",
        )


@router.get(
    "/file/contents",
    responses={
        200: {
            "description": "File contents retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "contents": "Hello, world!",
                        "path": "my-app/folder/file.txt",
                    },
                },
            },
        },
        404_1: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
        404_2: {
            "description": "File Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "File not found at path: <path>",
                    },
                },
            },
        },
    },
)
def get_file_contents(
    user_id: str,
    project: str,
    path: str,
    staging: bool = False,
    session=Depends(get_db_session),
):
    """
    Get the contents of a specific file in the bucket.
    """
    project_dao = ProjectDAO(session)
    project_obj = project_dao.get_by_user_and_name(
        user_id=user_id,
        name=project,
    )
    if not project_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project} not found.",
        )

    try:
        # Initialize the Google Cloud Storage client
        client = Client()
        bucket = client.bucket(
            "interface-file-system-staging" if staging else "interface-file-system",
        )

        # Construct the full path in the bucket
        full_path = f"{user_id}/{project_obj.name}/{path}"

        # Get the blob
        blob = bucket.blob(full_path)

        # Check if the file exists
        if not blob.exists():
            raise HTTPException(
                status_code=404,
                detail=f"File not found at path: {full_path}",
            )

        # Download the contents
        contents = blob.download_as_text()

        return {
            "contents": contents,
            "path": full_path,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get file contents: {str(e)}",
        )


@router.delete(
    "/file",
    responses={
        200: {
            "description": "File or folder deleted successfully",
            "content": {
                "application/json": {
                    "example": {
                        "message": "File or folder deleted successfully",
                        "path": "my-app/folder/file.txt",
                    },
                },
            },
        },
        404: {
            "description": "Project or File Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found or file not found at path: <path>",
                    },
                },
            },
        },
    },
)
def delete_file_or_folder(
    user_id: str,
    project: str,
    path: str,
    staging: bool = False,
    session=Depends(get_db_session),
):
    """
    Delete a file or folder from the user's project directory.
    If the path points to a folder, all contents will be deleted recursively.
    """
    project_dao = ProjectDAO(session)
    project_obj = project_dao.get_by_user_and_name(
        user_id=user_id,
        name=project,
    )
    if not project_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project} not found.",
        )

    try:
        # Initialize the Google Cloud Storage client
        client = Client()
        bucket = client.bucket(
            "interface-file-system-staging" if staging else "interface-file-system",
        )

        # Construct the full path in the bucket
        full_path = f"{user_id}/{project_obj.name}/{path}"

        # Check if the path exists
        blobs = list(bucket.list_blobs(prefix=full_path))
        if not blobs:
            raise HTTPException(
                status_code=404,
                detail=f"File or folder not found at path: {full_path}",
            )

        # Delete all blobs under the path (handles both files and folders)
        for blob in blobs:
            blob.delete()

        return {
            "message": "File or folder deleted successfully",
            "path": full_path,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete file or folder: {str(e)}",
        )


@router.post("/billing/invoice-month")
def trigger_monthly_invoicing(
    year: Optional[int] = None,
    month: Optional[int] = None,
    session=Depends(get_db_session),
) -> dict:
    """
    Trigger monthly invoicing for the specified period.
    Defaults to previous month if not specified.

    This endpoint is designed to be called by Cloud Scheduler.
    """
    try:
        # Import here to avoid circular imports
        from orchestra.routines.monthly_invoicer import invoice_month

        # Pass the session to avoid creating a new one
        invoice_month(year, month, session=session)

        period = f"{year}-{month:02d}" if year and month else "previous month"
        return {
            "status": "success",
            "message": f"Monthly invoicing completed for {period}",
            "year": year,
            "month": month,
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Monthly invoicing failed: {str(e)}",
        )


@router.post("/billing/suspend-past-due")
def trigger_billing_guard(
    session=Depends(get_db_session),
) -> dict:
    """
    Trigger billing guard to suspend past-due users with zero credits.

    This endpoint is designed to be called by Cloud Scheduler.
    """
    try:
        # Import here to avoid circular imports
        from orchestra.routines.billing_guard import suspend_past_due_users

        # Pass the session directly instead of letting the function manage its own
        suspend_past_due_users(session=session)

        return {
            "status": "success",
            "message": "Billing guard completed - past due users with zero credits suspended",
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Billing guard failed: {str(e)}")


@router.get("/user_billing_eligibility")
def get_user_billing_eligibility(
    user_id: str,
    session=Depends(get_db_session),
) -> dict:
    """
    Check if a user is eligible for monthly billing based on spending.

    :param user_id: id of the user to check.
    :return: dict with eligibility status and spending information.
    """
    users_dao = UsersDAO(session)
    try:
        # First check if user exists - this will raise HTTPException if not found
        user = users_dao.get_user_with_id(user_id)

        total_spending = users_dao.get_total_spending(user_id)
        can_enable_monthly = users_dao.can_enable_monthly_billing(user_id)

        return {
            "user_id": user_id,
            "total_spending": total_spending,
            "can_enable_monthly_billing": can_enable_monthly,
            "minimum_spend_required": 100.0,
            "remaining_spend_needed": max(0, 100.0 - total_spending),
        }
    except HTTPException as e:
        # Re-raise HTTPExceptions (like 404 for user not found)
        raise e
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"User not found: {str(e)}")
