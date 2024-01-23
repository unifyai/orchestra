class AIBenchRunner:
    def __init__(self, fn, test_load, input_policy):
        # Config
        self.fn = fn  # assumes fn takes a string as the input and returns strings asynchronously (streaming)
        self.test_load = test_load
        self.input_policy = input_policy  # short | long | mixed

        # Computed metrics
        self.ttft = list()
        self.end_to_end_latency = list()
        self.itl = list()
        self.cold_start = list()
        self.output_tokens = list()
        self.failed_queries = 0

    @property
    def output_tks_per_sec(self):
        raise NotImplementedError

    def __repr__(self):
        # developer facing print (for logging)
        raise NotImplementedError

    def concurrent_requests(self):
        raise NotImplementedError

    def QPS(self):
        raise NotImplementedError

    async def say_after(self, delay, what):
        print(time.time())
        await asyncio.sleep(delay)
        print(what)

    async def __call__(self):
        concurrent_requests = []
        num_concurrent_req = 100
        for i in range(num_concurrent_req):
            concurrent_requests.append(asyncio.create_task(
            self.say_after(10, 'hello')))

        print(f"started at {time.strftime('%X')}")

        await asyncio.gather(*concurrent_requests)
        # all "computed metrics" should be calculated here depending on the load_testing_policy
        # for test_load > 1 we should store **all** the data. This means that the returned metrics should be
        # lists, it's up to the higher layers to aggregate them if they want.
        # the returned dict should include `output_tks_per_sec` as well.
        # i.e.
        # result = {
        #   "load_testing_policy": str,
        #   "test_load": int,
        #   "target_input_length": int,
        #   "target_output_length": int",
        #   "ttft": "List<float>",
        #   "end_to_end_latency": "List<float>",
        #   "itl": "List<float>",
        #   "cold_start": "List<float>",
        #   "output_tokens": "List<int>",
        #   "output_tks_per_sec": List<float>,
        #   "failed_queries": int,
        # }
        # raise NotImplementedError
