import datetime
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, HTTPException
from fastapi.param_functions import Depends

from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.db.dao.datapoint_dao import DatapointDAO
from orchestra.db.dao.dataset_evaluation_dao import DatasetEvaluationDAO
from orchestra.db.dao.dataset_evaluation_task_dao import DatasetEvaluationTaskDAO
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
from orchestra.db.models.orchestra_models import (  # noqa: WPS235
    BenchmarkRun,
    Datapoint,
    DatasetEvaluation,
    DatasetEvaluationTask,
    Endpoint,
    License,
    Metric,
    Modality,
    Recharge,
    RechargeType,
    Task,
    Users,
)
from orchestra.web.api.admin.schema import (  # noqa: WPS235
    BenchmarkRunModelResponse,
    DatapointModelRequest,
    DatapointModelResponse,
    DatasetEvaluationModelRequest,
    DatasetEvaluationModelResponse,
    EndpointModelRequest,
    EndpointModelResponse,
    LicenseModelRequest,
    LicenseModelResponse,
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
from orchestra.web.api.utils.generate_points import generate_and_prune_points

router = APIRouter()


@router.get("/get_all_users", response_model=List[UsersModelResponse])
def get_all_users_models(
    users_dao: UsersDAO = Depends(),
) -> List[Users]:
    """
    Retrieve all users objects from the database.

    :param users_dao: DAO for users models.
    :return: list of users objects from database.
    """
    return users_dao.get_all_users()


@router.get("/get_user", response_model=List[UsersModelResponse])
def get_user(
    id: str,  # noqa: WPS125
    users_dao: UsersDAO = Depends(),
) -> List[Users]:
    """
    Retrieve specific users object from the database.

    :param id: id of users instance.
    :param users_dao: DAO for users models.
    :return: list of users objects from database.
    """
    return users_dao.filter(id=id)


@router.get("/get_all_recharge_types", response_model=List[RechargeTypeModelResponse])
def get_recharge_type_models(
    limit: int = 10,
    offset: int = 0,
    recharge_type_dao: RechargeTypeDAO = Depends(),
) -> List[RechargeType]:
    """
    Retrieve all recharge_type objects from the database.

    :param limit: limit of recharge_type objects, defaults to 10.
    :param offset: offset of recharge_type objects, defaults to 0.
    :param recharge_type_dao: DAO for recharge_type models.
    :return: list of recharge_type objects from database.
    """
    return recharge_type_dao.get_all_recharge_types(limit=limit, offset=offset)


@router.get("/get_recharge_type", response_model=List[RechargeTypeModelResponse])
def get_recharge_type(
    type: str,  # noqa: WPS125
    recharge_type_dao: RechargeTypeDAO = Depends(),
) -> List[RechargeType]:
    """
    Retrieve specific recharge_type object from the database.

    :param type: type of recharge_type object.
    :param recharge_type_dao: DAO for recharge_type models.
    :return: recharge_type object from database.
    """
    return recharge_type_dao.filter(type=type)


@router.get("/get_all_recharges", response_model=List[RechargeModelResponse])
def get_recharge_models(
    limit: int = 10,
    offset: int = 0,
    recharge_dao: RechargeDAO = Depends(),
) -> List[Recharge]:
    """
    Retrieve all recharge objects from the database.

    :param limit: limit of recharge objects, defaults to 10.
    :param offset: offset of recharge objects, defaults to 0.
    :param recharge_dao: DAO for recharge models.
    :return: list of recharge objects from database.
    """
    return recharge_dao.get_all_recharges(limit=limit, offset=offset)


@router.get("/get_recharge", response_model=List[RechargeModelResponse])
def get_recharge(  # noqa: WPS211
    id: Optional[int] = None,  # noqa: WPS125
    at: Optional[datetime.datetime] = None,
    user_id: Optional[str] = None,
    quantity: Optional[int] = None,
    type: Optional[str] = None,  # noqa: WPS125
    recharge_dao: RechargeDAO = Depends(),
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
    benchmark_run_dao: BenchmarkRunDAO = Depends(),
) -> List[BenchmarkRun]:
    """
    Retrieve all benchmark_run objects from the database.

    :param limit: limit of benchmark_run objects, defaults to 10.
    :param offset: offset of benchmark_run objects, defaults to 0.
    :param benchmark_run_dao: DAO for benchmark_run models.
    :return: list of benchmark_run objects from database.
    """
    return benchmark_run_dao.get_all_benchmark_runs(limit=limit, offset=offset)


@router.get("/get_benchmark_run", response_model=List[BenchmarkRunModelResponse])
def get_benchmark_run(  # noqa: WPS211
    id: Optional[int] = None,  # noqa: WPS125
    endpoint_id: Optional[int] = None,
    regime: Optional[str] = None,
    region: Optional[str] = None,
    seq_len: Optional[str] = None,
    measured_at: Optional[datetime.datetime] = None,
    benchmark_run_dao: BenchmarkRunDAO = Depends(),
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
    datapoint_dao: DatapointDAO = Depends(),
) -> List[Datapoint]:
    """
    Retrieve all datapoint objects from the database.

    :param limit: limit of datapoint objects, defaults to 10.
    :param offset: offset of datapoint objects, defaults to 0.
    :param datapoint_dao: DAO for datapoint models.
    :return: list of datapoint objects from database.
    """
    return datapoint_dao.get_all_datapoints(limit=limit, offset=offset)


@router.get("/get_datapoint", response_model=List[DatapointModelResponse])
def get_datapoint(  # noqa: WPS211
    id: Optional[int] = None,  # noqa: WPS125
    benchmark_run_id: Optional[int] = None,
    metric_name: Optional[str] = None,
    value: Optional[float] = None,
    measured_at: Optional[datetime.datetime] = None,
    datapoint_dao: DatapointDAO = Depends(),
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
    endpoint_dao: EndpointDAO = Depends(),
) -> List[Endpoint]:
    """
    Retrieve all endpoint objects from the database.

    :param limit: limit of endpoint objects, defaults to 10.
    :param offset: offset of endpoint objects, defaults to 0.
    :param endpoint_dao: DAO for endpoint models.
    :return: list of endpoint objects from database.
    """
    return endpoint_dao.get_all_endpoints_raw(limit=limit, offset=offset)


@router.get("/get_endpoint", response_model=List[EndpointModelResponse])
def get_endpoint(
    id: Optional[int] = None,  # noqa: WPS125
    mdl_id: Optional[int] = None,
    provider_id: Optional[int] = None,
    created_at: Optional[datetime.datetime] = None,
    endpoint_dao: EndpointDAO = Depends(),
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
    return endpoint_dao.filter(
        id=id,
        mdl_id=mdl_id,
        provider_id=provider_id,
        created_at=created_at,
    )


@router.get("/get_all_licenses", response_model=List[LicenseModelResponse])
def get_license_models(
    limit: int = 10,
    offset: int = 0,
    license_dao: LicenseDAO = Depends(),
) -> List[License]:
    """
    Retrieve all license objects from the database.

    :param limit: limit of license objects, defaults to 10.
    :param offset: offset of license objects, defaults to 0.
    :param license_dao: DAO for license models.
    :return: list of license objects from database.
    """
    return license_dao.get_all_licenses(limit=limit, offset=offset)


@router.get("/get_license", response_model=List[LicenseModelResponse])
def get_license(
    name: str,
    license_dao: LicenseDAO = Depends(),
) -> List[License]:
    """
    Retrieve specific license object from the database.

    :param name: name of license instance.
    :param license_dao: DAO for license models.
    :return: list of license objects from database.
    """
    return license_dao.filter(name=name)


@router.get("/get_all_metrics", response_model=List[MetricModelResponse])
def get_metric_models(
    limit: int = 10,
    offset: int = 0,
    metric_dao: MetricDAO = Depends(),
) -> List[Metric]:
    """
    Retrieve all metric objects from the database.

    :param limit: limit of metric objects, defaults to 10.
    :param offset: offset of metric objects, defaults to 0.
    :param metric_dao: DAO for metric models.
    :return: list of metric objects from database.
    """
    return metric_dao.get_all_metrics(limit=limit, offset=offset)


@router.get("/get_metric", response_model=List[MetricModelResponse])
def get_metric(  # noqa: WPS211
    name: str,
    units: str,
    display_name: str,
    tooltip: str,
    priority: int,
    plottable: bool,
    metric_dao: MetricDAO = Depends(),
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
    modality_dao: ModalityDAO = Depends(),
) -> List[Modality]:
    """
    Retrieve all modality objects from the database.

    :param limit: limit of modality objects, defaults to 10.
    :param offset: offset of modality objects, defaults to 0.
    :param modality_dao: DAO for modality models.
    :return: list of modality objects from database.
    """
    return modality_dao.get_all_modalities(limit=limit, offset=offset)


@router.get("/get_modality", response_model=List[ModalityModelResponse])
def get_modality(
    name: str,
    modality_dao: ModalityDAO = Depends(),
) -> List[Modality]:
    """
    Retrieve specific modality object from the database.

    :param name: name of modality object.
    :param modality_dao: DAO for modality models.
    :return: modality object from database.
    """
    return modality_dao.filter(name=name)


@router.get("/get_all_tasks", response_model=List[TaskModelResponse])
def get_task_models(
    limit: int = 10,
    offset: int = 0,
    task_dao: TaskDAO = Depends(),
) -> List[Task]:
    """
    Retrieve all task objects from the database.

    :param limit: limit of task objects, defaults to 10.
    :param offset: offset of task objects, defaults to 0.
    :param task_dao: DAO for task models.
    :return: list of task objects from database.
    """
    return task_dao.get_all_tasks(limit=limit, offset=offset)


@router.get("/get_task", response_model=List[TaskModelResponse])
def get_task(
    name: str,
    task_dao: TaskDAO = Depends(),
) -> List[Task]:
    """
    Retrieve specific task object from the database.

    :param name: name of task object.
    :param task_dao: DAO for task models.
    :return: task object from database.
    """
    return task_dao.filter(name=name)


@router.get("/get_dataset_evaluation")
def get_dataset_evaluation(
    dataset_name: str,
    dataset_evaluation_dao: DatasetEvaluationDAO = Depends(),
    endpoint_dao: EndpointDAO = Depends(),
    benchmark_run_dao: BenchmarkRunDAO = Depends(),
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """
    Retrieve specific dataset evaluation object from the database.
    """
    raw_data = dataset_evaluation_dao.filter(dataset_name=dataset_name)
    final_points = generate_and_prune_points(
        raw_data, endpoint_dao=endpoint_dao, benchmark_run_dao=benchmark_run_dao
    )
    return final_points


@router.put("/create_datapoint")
def create_datapoint_model(
    new_datapoint_object: DatapointModelRequest,
    datapoint_dao: DatapointDAO = Depends(),
) -> None:
    """
    Creates datapoint model in the database.

    :param new_datapoint_object: new datapoint model item.
    :param datapoint_dao: DAO for datapoint models.
    """
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
    endpoint_dao: EndpointDAO = Depends(),
) -> None:
    """
    Creates endpoint model in the database.

    :param new_endpoint_object: new endpoint model item.
    :param endpoint_dao: DAO for endpoint models.
    """
    created_at = datetime.datetime.now()
    endpoint_dao.create_endpoint(
        mdl_id=new_endpoint_object.mdl_id,
        provider_id=new_endpoint_object.provider_id,
        created_at=created_at,
    )


@router.put("/create_license")
def create_license_model(
    new_license_object: LicenseModelRequest,
    license_dao: LicenseDAO = Depends(),
) -> None:
    """
    Creates license model in the database.

    :param new_license_object: new license model item.
    :param license_dao: DAO for license models.
    """
    license_dao.create_license(
        name=new_license_object.name,
        image_url=new_license_object.image_url,
        description=new_license_object.description,
    )


@router.put("/create_metric")
def create_metric_model(
    new_metric_object: MetricModelRequest,
    metric_dao: MetricDAO = Depends(),
) -> None:
    """
    Creates metric model in the database.

    :param new_metric_object: new metric model item.
    :param metric_dao: DAO for metric models.
    """
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
    modality_dao: ModalityDAO = Depends(),
) -> None:
    """
    Creates modality model in the database.

    :param new_modality_object: new modality model item.
    :param modality_dao: DAO for modality models.
    """
    modality_dao.create_modality(
        name=new_modality_object.name,
    )


@router.put("/create_model")
def create_model(
    new_model_object: ModelRequest,
    model_dao: ModelDAO = Depends(),
) -> None:
    """
    Creates model model in the database.

    :param new_model_object: new model model item.
    :param model_dao: DAO for model models.
    """
    uploaded_at = datetime.datetime.now()
    model_dao.create_model(
        mdl_code=new_model_object.mdl_code,
        user_id=new_model_object.user_id,
        uploaded_at=uploaded_at,
        task=new_model_object.task,
        description=new_model_object.description,
        license=new_model_object.license,
        active=new_model_object.active,
        input_args_format=new_model_object.input_args_format,
        output_format=new_model_object.output_format,
        custom_fields=new_model_object.custom_fields,
    )


@router.put("/update_model")
def update_model(  # noqa: WPS211
    id: int,  # noqa: WPS125
    mdl_code: Optional[str] = None,
    user_id: Optional[str] = None,
    uploaded_at: Optional[datetime.datetime] = None,
    task: Optional[str] = None,
    description: Optional[str] = None,
    license: Optional[str] = None,
    active: Optional[bool] = None,
    input_args_format: Optional[str] = None,
    output_format: Optional[str] = None,
    custom_fields: Optional[str] = None,
    model_dao: ModelDAO = Depends(),
) -> None:
    """
    Update specific model model.

    :param id: id of model instance.
    :param mdl_code: mdl_code of model instance.
    :param user_id: user_id of model instance.
    :param uploaded_at: uploaded_at of model instance.
    :param task: task of model instance.
    :param description: description of model instance.
    :param license: license of model instance.
    :param active: is model instance active.
    :param input_args_format: input_args_format of model instance.
    :param output_format: output_format of model instance.
    :param custom_fields: custom_fields of model instance.
    :param model_dao: DAO for model models.
    """
    model_dao.update_model(
        id=id,
        mdl_code=mdl_code,
        user_id=user_id,
        uploaded_at=uploaded_at,
        task=task,
        description=description,
        license=license,
        active=active,
        input_args_format=input_args_format,
        output_format=output_format,
        custom_fields=custom_fields,
    )


@router.put("/create_provider")
def create_provider_model(
    new_provider_object: ProviderModelRequest,
    provider_dao: ProviderDAO = Depends(),
) -> None:
    """
    Creates provider model in the database.

    :param new_provider_object: new provider model item.
    :param provider_dao: DAO for provider models.
    """
    provider_dao.create_provider(
        name=new_provider_object.name,
        image_url=new_provider_object.image_url,
        description=new_provider_object.description,
    )


@router.post("/create_recharge")
def create_recharge_model(
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

    if (
        new_recharge_object.type == "payment"
        and new_recharge_object.transaction_id is None
    ):
        raise HTTPException(
            status_code=400,
            detail="Transaction id must be specified when adding a payment.",
        )

    at = datetime.datetime.now()
    user_dao.recharge_credit(
        user_id=new_recharge_object.user_id,
        quantity=new_recharge_object.quantity,
    )

    recharge_dao.create_recharge(
        at=at,
        user_id=new_recharge_object.user_id,
        quantity=new_recharge_object.quantity,
        type=new_recharge_object.type,
        transaction_id=new_recharge_object.transaction_id,
    )


@router.put("/create_recharge_type")
def create_recharge_type_model(
    new_recharge_type_object: RechargeTypeModelRequest,
    recharge_type_dao: RechargeTypeDAO = Depends(),
) -> None:
    """
    Creates recharge_type model in the database.

    :param new_recharge_type_object: new recharge_type model item.
    :param recharge_type_dao: DAO for recharge_type models.
    """
    recharge_type_dao.create_recharge_type(
        type=new_recharge_type_object.type,
    )


@router.put("/create_task")
def create_task_model(
    new_task_object: TaskModelRequest,
    task_dao: TaskDAO = Depends(),
) -> None:
    """
    Creates task model in the database.

    :param new_task_object: new task model item.
    :param task_dao: DAO for task models.
    """
    task_dao.create_task(
        name=new_task_object.name,
        modality=new_task_object.modality,
    )


@router.put("/create_dataset_evaluation")
def create_dataset_evaluation_model(
    new_dataset_evaluation_object: DatasetEvaluationModelRequest,
    dataset_evaluation_dao: DatasetEvaluationDAO = Depends(),
) -> None:
    """
    Creates database evaluation model in the database.
    """
    existing = dataset_evaluation_dao.filter(
        mdl_name=new_dataset_evaluation_object.mdl_name,
        dataset_name=new_dataset_evaluation_object.dataset_name,
        prompt=new_dataset_evaluation_object.prompt,
    )
    if existing:
        raise HTTPException(
            status_code=400,
            detail="Dataset evaluation already exists for this model, dataset and prompt.",
        )
    dataset_evaluation_dao.create_dataset_evaluation(
        mdl_name=new_dataset_evaluation_object.mdl_name,
        dataset_name=new_dataset_evaluation_object.dataset_name,
        prompt=new_dataset_evaluation_object.prompt,
        gt_score=new_dataset_evaluation_object.gt_score,
        score=new_dataset_evaluation_object.score,
        metric=new_dataset_evaluation_object.metric,
    )


@router.put("/update_benchmark_run")
def update_benchmark_run(  # noqa: WPS211
    id: int,  # noqa: WPS125
    endpoint_id: Optional[int] = None,
    regime: Optional[str] = None,
    region: Optional[str] = None,
    seq_len: Optional[str] = None,
    measured_at: Optional[datetime.datetime] = None,
    benchmark_run_dao: BenchmarkRunDAO = Depends(),
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
    measured_at: Optional[datetime.datetime] = None,
    datapoint_dao: DatapointDAO = Depends(),
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
    users_dao: UsersDAO = Depends(),
) -> None:
    """
    Update the stripe customer id of a user.

    :param id: id of the user to be updated.
    :param stripe_customer_id: stripe customer id.
    :param users_dao: DAO for users models.
    """
    users_dao.set_stripe_customer_id(user_id=id, stripe_id=stripe_customer_id)
    users_dao.session.commit()


@router.put("/enable_autorecharge")
def update_user_autorecharge(  # noqa: WPS211
    id: str,  # noqa: WPS125
    enable: bool,
    users_dao: UsersDAO = Depends(),
) -> None:
    """
    Update the autorecharge status of a user.

    :param id: id of the user to be updated.
    :param enable: whether to enable or disable autorecharge.
    :param users_dao: DAO for users models.
    """
    users_dao.enable_autorecharge(user_id=id, enable=enable)
    users_dao.session.commit()


@router.put("/autorecharge_threshold")
def update_user_autorecharge_threshold(  # noqa: WPS211
    id: str,  # noqa: WPS125
    threshold: float,
    users_dao: UsersDAO = Depends(),
) -> None:
    """
    Update the autorecharge threshold of a user.

    :param id: id of the user to be updated.
    :param threshold: new autorecharge threshold.
    :param users_dao: DAO for users models.
    """
    users_dao.set_autorecharge_threshold(user_id=id, threshold=threshold)
    users_dao.session.commit()


@router.put("/autorecharge_qty")
def update_user_autorecharge_qty(  # noqa: WPS211
    id: str,  # noqa: WPS125
    qty: float,
    users_dao: UsersDAO = Depends(),
) -> None:
    """
    Update the autorecharge quantity of a user.

    :param id: id of the user to be updated.
    :param qty: new autorecharge quantity.
    :param users_dao: DAO for users models.
    """
    users_dao.set_autorecharge_qty(user_id=id, qty=qty)
    users_dao.session.commit()


@router.put("/dataset_evaluation_task")
def update_dataset_evaluation_task_status(  # noqa: WPS211
    id: str,  # noqa: WPS125
    status: str,
    dataset_evaluation_task: DatasetEvaluationTaskDAO = Depends(),
) -> None:
    """
    Update the status of a dataset evaluation task.
    """
    dataset_evaluation_task.update_dataset_evaluation_task(user_id=id, status=status)
    dataset_evaluation_task.session.commit()


@router.put("/update_dataset_evaluation")
def update_dataset_evaluation(
    dataset_evaluation_object: DatasetEvaluationModelRequest,
    dataset_evaluation_dao: DatasetEvaluationDAO = Depends(),
) -> None:
    """
    Updates database evaluation model in the database.
    """
    existing = dataset_evaluation_dao.filter(
        mdl_name=dataset_evaluation_object.mdl_name,
        dataset_name=dataset_evaluation_object.dataset_name,
        prompt=dataset_evaluation_object.prompt,
    )
    if not existing:
        raise HTTPException(
            status_code=400,
            detail="Dataset evaluation doesn't exist for this model, dataset and prompt.",
        )
    dataset_evaluation_dao.update_dataset_evaluation(
        mdl_name=dataset_evaluation_object.mdl_name,
        dataset_name=dataset_evaluation_object.dataset_name,
        prompt=dataset_evaluation_object.prompt,
        gt_score=dataset_evaluation_object.gt_score,
        score=dataset_evaluation_object.score,
        metric=dataset_evaluation_object.metric,
    )
