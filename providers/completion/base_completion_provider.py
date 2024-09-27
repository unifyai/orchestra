import json
import logging
import os
from typing import Any, Dict, List

import litellm
import tiktoken
from fastapi import HTTPException

# from litellm.utils import get_model_info  # Uncomment later
from openai import (
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    AsyncStream,
    BadRequestError,
    OpenAI,
    RateLimitError,
    Stream,
)

from orchestra.db.models.orchestra_models import CustomEndpoint
from orchestra.web.api.utils.helpers import (
    check_litellm_supported_args,
    filter_kwargs_for_openai_client,
)
from orchestra.web.api.utils.http_responses import server_error_with_digest

logger = logging.getLogger(__name__)

# Pricing info of providers with pay-per-token model is
# standardized to per million tokens.
PRICING_PER_TOKENS = 1000000


class BaseCompletionProvider:
    """Base class for completion providers."""

    # TODO: Make this a property and enforce definition with NotImplemented
    supported_models: Dict[str, Any] = {}

    def __init__(
        self,
        hub_model,
        litellm_provider_prefix,
        custom_endpoint=None,
        custom_api_key=None,
    ) -> None:
        self.hub_model: str = hub_model
        self.litellm_provider_prefix: str = litellm_provider_prefix
        self.custom_endpoint: CustomEndpoint = custom_endpoint
        self.custom_api_key: str = custom_api_key
        self.supported_models: Dict[str, Any] = {}

    @property
    def api_key_var(self) -> str:
        """
        Get the provider api key var NAME.

        :raises NotImplementedError: This method should be implemented in a subclass.
        """
        raise NotImplementedError("This method should be implemented in a subclass")

    @property
    def litellm_api_key_var(self) -> str:
        """
        Get the provider api key var NAME for using litellm.

        :raises NotImplementedError: This method should be implemented in a subclass.
        """
        raise NotImplementedError("This method should be implemented in a subclass")

    @property
    def base_url(self) -> str:
        """
        Get the base URL.

        :raises NotImplementedError: This method should be implemented in a subclass.
        """
        raise NotImplementedError("This method should be implemented in a subclass")

    @property
    def provider_endpoint(self):
        # TODO: Docs
        # TODO: Add logic to raise an error if self.supported_models is empty
        if self.custom_endpoint:
            return self.custom_endpoint.mdl_name
        return self.supported_models[self.hub_model]["endpoint"]

    @property
    def prompt_cost(self):
        # TODO: Docs
        # TODO: Add logic to raise an error if self.supported_models is empty
        return self.supported_models[self.hub_model]["cost"]["prompt"]

    @property
    def completion_cost(self):
        # TODO: Docs
        # TODO: Add logic to raise an error if self.supported_models is empty
        return self.supported_models[self.hub_model]["cost"]["completion"]

    @property
    def context_window(self):
        # TODO: Docs
        # TODO: Add logic to raise an error if self.supported_models is empty
        return self.supported_models[self.hub_model]["context_window"]

    @property
    def api_key(self) -> str:  # noqa: D102
        if self.custom_api_key:
            return self.custom_api_key
        key = os.getenv(self.api_key_var)
        if key is None:
            raise ValueError("ENV VAR {self.api_key_var} not found.")
        return key

    @property
    def max_cost(self) -> float:  # noqa: D102
        return self.completion_cost * self.context_window / PRICING_PER_TOKENS

    def _modify_output(self, out: Dict, **kwargs) -> Dict:
        output = {}
        output["model"] = out.get("model")
        output["created"] = out.get("created")
        output["id"] = out.get("id")
        output["object"] = out.get("object", "chat.completion.chunk")
        output["usage"] = out.get("usage") if out.get("usage") else {}
        if "estimated_cost" in output["usage"]:
            output["usage"].pop("estimated_cost")
        output["choices"] = out.get("choices") if out.get("choices") else []
        return output  # noqa: WPS420

    # Uncomment later
    # def update_supported_models(self):
    #     for key in self.supported_models:
    #         endpoint = self.supported_models[key]["endpoint"]
    #         try:
    #             model_info = get_model_info(endpoint)
    #             self.supported_models[key] = {
    #                 **self.supported_models[key],
    #                 "context_window": model_info["max_input_tokens"],
    #                 "cost": {
    #                     "prompt": model_info["input_cost_per_token"] * PRICING_PER_TOKENS,
    #                     "completion": model_info["output_cost_per_token"] * PRICING_PER_TOKENS
    #                 }
    #             }
    #         except Exception as e:
    #             if "vertex" in endpoint:
    #                 print(e)  # Just for debugging
    #             pass

    def compute_cost(self, prompt_tks, output_tks) -> float:
        prompt_cost = prompt_tks * self.prompt_cost / PRICING_PER_TOKENS
        completion_cost = output_tks * self.completion_cost / PRICING_PER_TOKENS
        return prompt_cost + completion_cost

    def get_response_cost(
        self,
        response,
        prompt_tokens,
        completion_tokens,
        using_litellm,
    ):
        cost = self.compute_cost(prompt_tokens, completion_tokens)
        return cost
        # if not using_litellm:
        #     return cost
        # hidden_param_cost = response._hidden_params.get("response_cost")
        # litellm_cost = hidden_param_cost if hidden_param_cost is not None else cost
        # return litellm_cost

    def get_usage_info(  # noqa: WPS210
        self,
        completions: List[str],
        messages: List[Dict],
        response: Any,
        using_litellm: bool,
    ) -> Dict:
        """
        Returns a usage dict with cost and token data when streaming.

        :param completions: The completed text.
        :type completions: str
        :param messages: List of input prompts.
        :type messages: List[Dict]
        :param response: Response from the llm
        :type response: Any
        :param using_litellm: Whether or not litellm was used
        :type using_litellm: bool

        :return: a loaded usage dict
        """
        try:
            total_prompt = ""
            for item in messages:  # noqa: WPS519
                total_prompt += item["content"]
            # TODO: We need to standarise this and check for gpt models
            encoding = tiktoken.get_encoding("cl100k_base")
            prompt_tokens = len(encoding.encode(total_prompt))
            tokens = [encoding.encode(completion) for completion in completions]
            completion_tokens = sum(len(token) for token in tokens)
            return {
                "cost": self.get_response_cost(
                    response,
                    prompt_tokens,
                    completion_tokens,
                    using_litellm,
                ),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }

        except Exception:  # TODO: This need to be scoped down and prob moved inside
            return {"cost": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def __call__(  # noqa: D102, WPS211, C901, WPS231, WPS238, WPS210
        self,
        messages: List,  # type: ignore
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:
        kwargs, extra_body = filter_kwargs_for_openai_client(kwargs)
        using_litellm = bool(self.litellm_provider_prefix)
        try:  # noqa: WPS225
            if not using_litellm:
                client = kwargs.pop(
                    "client",
                    OpenAI(api_key=self.api_key, base_url=self.base_url),
                )
                response = client.chat.completions.create(
                    model=self.provider_endpoint,
                    messages=messages,
                    stream=stream,
                    extra_body=extra_body,
                    **kwargs,
                )
            else:
                # set credentials from custom api keys
                if self.custom_api_key:
                    if self.custom_api_key.startswith("{"):
                        custom_api_key = json.loads(self.custom_api_key)
                        kwargs = {
                            **kwargs,
                            **custom_api_key,
                        }
                    else:
                        kwargs["api_key"] = self.custom_api_key
                else:
                    os.environ[self.litellm_api_key_var] = self.api_key

                # add provider prefix for custom endpoints
                model = self.provider_endpoint
                if self.custom_endpoint:
                    model = self.litellm_provider_prefix + "/" + model

                # check if the kwargs are accepted by litellm
                check_litellm_supported_args(kwargs, model)

                # extra_body can't be passed to anthropic, bedrock or vertex_ai
                if self.litellm_provider_prefix not in [
                    "anthropic",
                    "bedrock",
                    "vertex_ai",
                ]:
                    kwargs["extra_body"] = extra_body
                drop_params = extra_body.pop("drop_params", True)

                # llm call
                response = litellm.completion(
                    model=model,
                    messages=messages,
                    stream=stream,
                    drop_params=drop_params,
                    **kwargs,
                )

            if isinstance(response, Stream) or stream:
                return (
                    SyncGeneratorWrapper(self, response, messages, using_litellm),
                    None,
                )

            # TODO: Maybe remove this dump unless neccesary?
            response_dict = self._modify_output(response.model_dump(), stream=stream)
            return (
                response_dict,
                self.get_response_cost(
                    response,
                    response.usage.prompt_tokens,
                    response.usage.completion_tokens,
                    using_litellm,
                )
                if not self.custom_api_key
                else 0,
            )
        # TODO: These needs to be processed correctly in our endpoint
        except APITimeoutError as error:
            logger.error(f"Raised openai.APITimeoutError, Error: {error}")
            raise HTTPException(status_code=408, detail=str(error))  # noqa: WPS432
        except RateLimitError as error:
            logger.error(f"Raised openai.RateLimitError, Error: {error}")
            raise HTTPException(status_code=429, detail=str(error))  # noqa: WPS432
        except BadRequestError as error:
            logger.error(f"Raised openai.BadRequestError, Error: {error}")
            raise HTTPException(status_code=400, detail=str(error))  # noqa: WPS432
        except APIError as error:
            logger.error(f"Raised openai.APIError, Error: {error}")
            raise HTTPException(status_code=400, detail=str(error))  # noqa: WPS432
        except Exception as e:
            error, digest = server_error_with_digest(str(e))
            logger.error(f"Digest {digest}: {e}")
            raise error

    def __call_async__(  # noqa: D102, WPS211, C901, WPS231, WPS238, WPS210
        self,
        messages: List,  # type: ignore
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:
        kwargs, extra_body = filter_kwargs_for_openai_client(kwargs)
        using_litellm = bool(self.litellm_provider_prefix)
        try:  # noqa: WPS225
            if not using_litellm:
                client = kwargs.pop(
                    "client",
                    AsyncOpenAI(api_key=self.api_key, base_url=self.base_url),
                )
                response = client.chat.completions.create(
                    model=self.provider_endpoint,
                    messages=messages,
                    stream=stream,
                    extra_body=extra_body,
                    **kwargs,
                )
            else:
                # set credentials from custom api keys
                if self.custom_api_key:
                    if self.custom_api_key.startswith("{"):
                        custom_api_key = json.loads(self.custom_api_key)
                        kwargs = {
                            **kwargs,
                            **custom_api_key,
                        }
                    else:
                        kwargs["api_key"] = self.custom_api_key
                else:
                    os.environ[self.litellm_api_key_var] = self.api_key

                # add provider prefix for custom endpoints
                model = self.provider_endpoint
                if self.custom_endpoint:
                    model = self.litellm_provider_prefix + "/" + model

                # check if the kwargs are accepted by litellm
                check_litellm_supported_args(kwargs, model)

                # extra_body can't be passed to anthropic, bedrock or vertex_ai
                if self.litellm_provider_prefix not in [
                    "anthropic",
                    "bedrock",
                    "vertex_ai",
                ]:
                    kwargs["extra_body"] = extra_body
                drop_params = extra_body.pop("drop_params", True)

                # llm call
                response = litellm.acompletion(
                    model=model,
                    messages=messages,
                    stream=stream,
                    drop_params=drop_params,
                    **kwargs,
                )
            if isinstance(response, AsyncStream) or stream:
                return (
                    AsyncGeneratorWrapper(self, response, messages, using_litellm),
                    None,
                )

            # TODO: Maybe remove this dump unless neccesary?
            response_dict = self._modify_output(response.model_dump(), stream=stream)
            return (
                response_dict,
                self.get_response_cost(
                    response,
                    response.usage.prompt_tokens,
                    response.usage.completion_tokens,
                    using_litellm,
                )
                if not self.custom_api_key
                else 0,
            )
        # TODO: These needs to be processed correctly in our endpoint
        except APITimeoutError as error:
            logger.error(f"Raised openai.APITimeoutError, Error: {error}")
            raise HTTPException(status_code=408, detail=str(error))  # noqa: WPS432
        except RateLimitError as error:
            logger.error(f"Raised openai.RateLimitError, Error: {error}")
            raise HTTPException(status_code=429, detail=str(error))  # noqa: WPS432
        except BadRequestError as error:
            logger.error(f"Raised openai.BadRequestError, Error: {error}")
            raise HTTPException(status_code=400, detail=str(error))  # noqa: WPS432
        except APIError as error:
            logger.error(f"Raised openai.APIError, Error: {error}")
            raise HTTPException(status_code=400, detail=str(error))  # noqa: WPS432


class BaseGeneratorWrapper:
    def __init__(self, provider, response, messages, using_litellm):
        self.provider = provider
        self._response = response
        self._messages = messages
        self._using_litellm = using_litellm
        self.total_cost = None

    def generator_iteration(self, part, whole):
        part_dict = part.model_dump()
        part_dict = self.provider._modify_output(part_dict, stream=True)
        choices = part_dict["choices"]
        if choices:
            if choices[0]["delta"]["content"] is None:
                if not part_dict.get("usage") and not choices[0]["finish_reason"]:
                    if "tool_calls" in choices[0]["delta"] and (
                        choices[0]["delta"]["tool_calls"] is not None
                    ):  # TODO this is a bit hacky ...
                        return part_dict
                    return None
                return part_dict

        part_text = choices[0]["delta"]["content"] if choices else ""
        index = choices[0]["index"] if choices else 0
        if len(whole) <= index:
            whole.extend([""] * (index - len(whole) + 1))
        whole[index] += part_text
        return part_dict

    def get_final_chunk(self, part_dict, whole):
        """
        Get the final chunk of a streaming response.

        :param part_dict: The part dict.
        :type part_dict: Dict
        :param whole: The whole text.
        :type whole: List

        :yield: part_dict.
        """
        if part_dict and part_dict.get("usage"):
            part_dict["usage"]["cost"] = self.provider.get_response_cost(
                self._response,
                part_dict["usage"]["prompt_tokens"],
                part_dict["usage"]["completion_tokens"],
                self._using_litellm,
            )
        else:
            if part_dict is None:
                part_dict = {}
            part_dict["usage"] = self.provider.get_usage_info(
                whole,
                self._messages,
                self._response,
                self._using_litellm,
            )
        self.total_cost = part_dict["usage"]["cost"]
        yield part_dict

    def is_final_chunk(self, part_dict):
        if part_dict:
            if part_dict.get("usage"):
                return True
            if "finish_reason" in part_dict.get("choices", [{}])[0]:
                if part_dict["choices"][0]["finish_reason"]:
                    return True
        return False


class SyncGeneratorWrapper(BaseGeneratorWrapper):  # noqa: D101
    def generator(self):  # noqa: D102, C901, WPS210, WPS231
        whole = []
        part_dict = None
        try:  # noqa: WPS501
            for part in self._response:
                part_dict = self.generator_iteration(part, whole)
                if not part_dict or self.is_final_chunk(part_dict):
                    continue
                yield part_dict
        finally:
            yield from self.get_final_chunk(part_dict, whole)


# TODO: Remove code duplication here
class AsyncGeneratorWrapper(BaseGeneratorWrapper):  # noqa: D101
    async def generator(self):  # noqa: D102, C901, WPS210, WPS231
        whole = []
        part_dict = None
        try:  # noqa: WPS501
            async for part in await self._response:
                part_dict = self.generator_iteration(part, whole)
                if not part_dict or self.is_final_chunk(part_dict):
                    continue
                yield part_dict
        finally:
            for val in self.get_final_chunk(part_dict, whole):
                yield val
