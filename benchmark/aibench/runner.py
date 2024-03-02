import asyncio
import json
import random
import time

import aiohttp
import tiktoken


class AIBenchRunner:
    def __init__(self, fn, load, input_policy):
        # Config
        self.fn = fn  # assumes fn takes a string as the input and returns strings asynchronously (streaming)
        self.load = load
        self.input_policy = input_policy  # short | long

        # Computed metrics
        self.ttft = []
        self.end_to_end_latency = []
        self.cold_start = 0
        self.prompt_tokens = []
        self.output_tokens = []
        self.total_tokens = []
        self.failed_queries = 0

        # Queues
        self.prompt_queue = asyncio.Queue()
        self.results_queue = asyncio.Queue()

        # Tokenizer
        self.tokenizer = tiktoken.get_encoding("cl100k_base")

    @property
    def itl(self):
        return [
            (e2e_lat - ttft) / (o_tks - 1)
            for e2e_lat, ttft, o_tks in zip(
                self.end_to_end_latency,
                self.ttft,
                self.output_tokens,
            )
        ]

    @property
    def output_tks_per_sec(self):
        return [1000 / i for i in self.itl]

    def as_dict(self):
        # TODO: Prob validate rules among the metrics
        # i.e. ttft + itl * num_output_toks <= e2e latency
        return {
            "load": self.load,
            "input_policy": self.input_policy,
            "ttft": self.ttft,
            "e2e_latency": self.end_to_end_latency,
            "itl": self.itl,
            "cold_start": self.cold_start,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "output_tks_per_sec": self.output_tks_per_sec,
            "failed_queries": self.failed_queries,
        }

    async def unpack_metrics(self):
        while not self.results_queue.empty():
            req_result: dict = await self.results_queue.get()
            self.ttft.append(req_result["ttft"])
            self.end_to_end_latency.append(req_result["e2e_latency"])
            self.prompt_tokens.append(req_result["prompt_tokens"])
            self.output_tokens.append(req_result["output_tokens"])
            self.total_tokens.append(req_result["total_tokens"])
            self.failed_queries += req_result["failed_queries"]
            self.results_queue.task_done()

    @staticmethod
    def _get_samples(filename):
        with open(filename, "r") as file:
            f = json.load(file)
            samples = [(item["prompt"].replace("\n", ""), item["length"]) for item in f]
        return samples

    def prepare_prompts(self):
        samples_fname = f"prompts_{self.input_policy}.json"
        prompts = random.sample(self._get_samples(samples_fname), self.load)
        all_prompts = []
        for prompt in prompts:
            count = {random.randint(0, int((4096 - prompt[1]) / prompt[1]))}
            preamble = f"Repeat the following line {count} times without generating the EOS token earlier than that: \n"
            all_prompts.append(preamble + prompt[0])
        return all_prompts

    def _max_token_sampler(self):
        if self.input_policy == "short":
            return int(random.normalvariate(200, 20))
        else:
            return int(random.normalvariate(1000, 100))

    async def stream_output(self, result, completions):
        async for part in result.generator():
            choices = part.get("choices")
            if choices:
                content = choices[0].get("delta", {}).get("content", "")
                if content:
                    completions.append(
                        {
                            "content": content,
                            "reception_time": time.perf_counter(),
                        },
                    )
            await asyncio.sleep(0)

    async def compute_metrics(self):
        prompt = await self.prompt_queue.get()
        max_tokens = self._max_token_sampler()
        completions = []
        metrics_dict = {}
        # TODO: max_tokens seem to be not respected?
        # check by printing max_tokens value here
        # then check the `output_tokens` in processed results
        # TODO: remove the double return        
        result, _ = self.fn(  # type: ignore
            prompt=prompt,
            max_tokens=max_tokens,
            stream=True,
        )

        metrics_dict["failed_queries"] = 0
        if result is None:
            metrics_dict["failed_queries"] = 1
            return

        start_time = time.perf_counter()
        await self.stream_output(result, completions)
        end_time = time.perf_counter()
        # TODO: remove?
        # these artifacts sort of give away we're using litellm
        content = "".join(
            [
                completion["content"]
                for completion in completions
                if completion["content"] is not None
            ],
        )

        metrics_dict["ttft"] = (completions[0]["reception_time"] - start_time) * 1000
        metrics_dict["e2e_latency"] = (end_time - start_time) * 1000
        metrics_dict["prompt_tokens"] = len(self.tokenizer.encode(prompt))
        metrics_dict["output_tokens"] = len(self.tokenizer.encode(content))
        metrics_dict["total_tokens"] = (
            metrics_dict["prompt_tokens"] + metrics_dict["output_tokens"]
        )

        await self.results_queue.put(metrics_dict)
        self.prompt_queue.task_done()

    async def check_coldstart(self, threshold):
        prompt = "2+2 is "
        completions = []

        start_time = time.perf_counter()
        result, _ = self.fn(  # type: ignore
            prompt=prompt,
            max_tokens=10,
            stream=True,
        )

        if result is None:
            print("Run during cold start failed")
            return 0

        await self.stream_output(result, completions)
        first_token_time = completions[0]["reception_time"]
        try:
            second_token_time = completions[1]["reception_time"]
        except IndexError:
            # for provider where streaming is broken and whole text
            # is returned at once, check only the first token
            second_token_time = 0

        cold_start = first_token_time - start_time
        if (
            cold_start > threshold
            and (second_token_time - first_token_time) * 10 <= cold_start
        ):
            return cold_start
        else:
            return 0

    async def __call__(self):
        self.cold_start = await self.check_coldstart(threshold=15)  # TODO: magic number
        concurrent_requests = []
        for prompt in self.prepare_prompts():
            await self.prompt_queue.put(prompt)
        for i in range(self.load):
            concurrent_requests.append(asyncio.create_task(self.compute_metrics()))
        await asyncio.gather(*concurrent_requests)
        await self.unpack_metrics()
        return self.as_dict()

    # clean up the following code to comply with .complete provider i/o
    # needed for presenting as dummy fn
    async def dummy_fn(url, data, headers, auth):
        async with aiohttp.ClientSession() as session:
            async with session.put(
                url,
                data=json.dumps(data),
                headers=headers,
                auth=auth,
            ) as response:
                return await response.json()
