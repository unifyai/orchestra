from typing import List, Set, Tuple

from fastapi import APIRouter
from providers.completion import PROVIDER_CLASSES

from orchestra.web.api.models.schema import ModelInfo, ModelInfoList

router = APIRouter()


def _num_providers_and_name(model: ModelInfo) -> Tuple[float, str]:
    return (1 / len(model.providers), model.id)


def _get_all_models() -> Set[str]:
    all_model_ids: Set[str] = set()
    for provider in PROVIDER_CLASSES.values():
        model_ids = (
            provider.supported_models.keys()
            if isinstance(provider.supported_models, dict)
            else provider.supported_models
        )
        all_model_ids = all_model_ids.union(set(model_ids))
    return all_model_ids


def _generate_model_list() -> List[ModelInfo]:
    # hacky placeholder until the db is populated don't look
    all_model_ids = _get_all_models()

    models = []
    for model_id in all_model_ids:
        model_providers = []
        for provider_id in PROVIDER_CLASSES.keys():
            if model_id in PROVIDER_CLASSES[provider_id].supported_models:
                model_providers += [provider_id]
        models += [
            ModelInfo(
                id=model_id,
                modality="ToDo",
                task="ToDo",
                providers=model_providers,
            ),
        ]

    return sorted(models, key=_num_providers_and_name)


models = _generate_model_list()


@router.get("/models")
async def list_models() -> ModelInfoList:
    """
    Sends list of models to the user.

    :returns: list of models.
    """
    return ModelInfoList(models=models)
