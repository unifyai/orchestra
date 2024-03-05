from typing import Dict, List, Any
from datetime import datetime
import time
from providers.completion.base_completion_provider import BaseCompletionProvider
import boto3
import os
import json

from providers.completion.base_completion_provider import (
    BaseCompletionProvider,
    SyncGeneratorWrapper,
)

class AWSBedrock(BaseCompletionProvider):
    """
    A completion provider that uses the AWS Bedrock service.

    Source: https://docs.aws.amazon.com/bedrock/latest/userguide/model-ids.html
    Pricing is per thousand tokens: https://aws.amazon.com/bedrock/pricing/
    """

    def __init__(self, hub_model):
        super().__init__(hub_model)
        self.supported_models = supported_models

    @property
    def api_key_var(self) -> str:
        return "junk"

    @property
    def base_url(self):
        return "more junk"

    @staticmethod
    def str_to_ts(str):
        parsed = datetime.strptime(str, '%a, %d %b %Y %H:%M:%S %Z')
        return int(parsed.timestamp())
        
    @staticmethod
    def usage_from_response(response):
        prompt_tokens = int(response['x-amzn-bedrock-input-token-count'])
        completion_tokens = int(response['x-amzn-bedrock-output-token-count'])
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }


    def client(self):
        return boto3.client(service_name="bedrock-runtime", region_name="us-west-2")

    
    def __call__(self, messages: List, stream: bool = False, **kwargs: Any) -> Any:
        _messages = messages[:]
        if "mistral" in self.provider_endpoint:
            allowed_args = ["max_tokens", "stop", "temperature", "top_p", "top_k"]            
        elif "llama" in self.provider_endpoint:
            allowed_args = ["temperature", "top_p", "max_tokens"]
            if "max_tokens" in kwargs:
                kwargs["max_gen_tokens"] = kwargs.pop("max_tokens")
        kwargs_bedrock = {k: v for k, v in kwargs.items() if k in allowed_args}
        kwargs_bedrock["prompt"] = self.prompt_factory(_messages)
        client = self.client()
        if stream:
            response = client.invoke_model_with_response_stream(
                modelId=self.provider_endpoint, 
                body=json.dumps(kwargs_bedrock),
            )
            return (BedrockSyncGeneratorWrapper(self, response, messages), None)
        else:
            response = client.invoke_model(
                modelId=self.provider_endpoint, body=json.dumps(kwargs_bedrock)
            )
            return (
                self.response_to_chat_completion(response),
                self.compute_cost(
                    response['ResponseMetadata']['HTTPHeaders']['x-amzn-bedrock-input-token-count'],
                    response['ResponseMetadata']['HTTPHeaders']['x-amzn-bedrock-output-token-count'],
                ),
            )
            
    def response_to_chat_completion(self, response):
        metadata = response['ResponseMetadata']['HTTPHeaders']
        body = json.loads(response["body"].read())
        created_at = self.str_to_ts(metadata['date'])
        
        return dict(
            id=metadata['x-amzn-requestid'],
            choices=[
                dict(
                    finish_reason=body['outputs'][0]['stop_reason'],  # TODO: check if these are the actual finish reason strings, else modify output
                    index=0,
                    message=dict(
                        content=body['outputs'][0]['text'],
                        role="assistant",
                    ),
                    logprobs=None,
                ),
            ],
            created=created_at,
            model=self.hub_model,
            object="chat.completion",
            usage=self.usage_from_response(metadata),
        )

    # TODO Put this in a util file as its also used in replicate
    def prompt_factory(self, messages):
        return "\n".join(
            (
                f"[INST] {message['content']} [/INST]"
                if message["role"] == "user"
                else message["content"]
            )
            for message in messages
        )

class BedrockSyncGeneratorWrapper(SyncGeneratorWrapper):
    def generator_iteration(self, part, whole):
        return sse_to_part_dict(part, whole)
    def generator(self):  # noqa: D102, C901, WPS210, WPS231
        whole = []
        try:  # noqa: WPS501
            for part in self._response['body']:
                chunk = json.loads(part.get('chunk').get('bytes').decode())
                chunk['id'] = self._response['ResponseMetadata']['HTTPHeaders']['x-amzn-requestid']
                part_dict = self.generator_iteration(chunk, whole)
                if part_dict is None:
                    continue
                yield part_dict
        finally:
            self.total_cost = self.provider.compute_cost_streaming(
                whole,
                self._messages,
            )

def sse_to_part_dict(part, whole):
    data = part['outputs'][0]['text']
    if 'amazon-bedrock-invocationMetrics' in part:
        metrics = part['amazon-bedrock-invocationMetrics']
        prompt_tokens = int(metrics['inputTokenCount'])
        completion_tokens = int(metrics['outputTokenCount'])
        usage = {'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens, 'total_tokens': prompt_tokens + completion_tokens}
    else:
        usage = {}
    part_dict = {
        "id": part['id'],
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "choices": [
            {
                "index": 0,
                "delta": {"content": data},
                "logprobs": None,  # TODO?
                "finish_reason": part['outputs'][0]['stop_reason'],  # TODO: check if the str of the reaosn provided is OAI
            },
        ],
        "usage": usage,
    }
    if data == "":
        return None
    if not whole:
        whole.extend([""])
    whole[0] += data
    return part_dict

supported_models = {
    "llama-2-13b-chat": {
        "endpoint": "meta.llama2-13b-chat-v1",
        "context_window": 4096,
        "cost": {"prompt": 0.75, "completion": 1},
    },
    "llama-2-70b-chat": {
        "endpoint": "meta.llama2-70b-chat-v1",
        "context_window": 4096,
        "cost": {"prompt": 1.95, "completion": 2.56},
    },
    "mistral-7b-instruct-v0.1": {
        "endpoint": "mistral.mistral-7b-instruct-v0:2",
        "context_window": 16384,
        "cost": {"prompt": 0.15, "completion": 0.2},
    },
    "mixtral-8x7b-instruct-v0.1": {
        "endpoint": "mistral.mixtral-8x7b-instruct-v0:1",
        "context_window": 32768,
        "cost": {"prompt": 0.45, "completion": 0.70},  # noqa: WPS339
    },
}
