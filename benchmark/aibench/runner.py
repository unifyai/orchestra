class AIBenchRunner:
    def __init__(self, fn, load_testing_policy, test_load, input_length, output_length):
        # Config
        self.fn = fn  # assumes fn takes a string as the input and returns strings asynchronously (streaming)
        self.load_testing_policy = load_testing_policy  # concurrent | QPS
        self.test_load = test_load  # 1 | 10
        self.target_input_length = input_length
        self.target_output_length = output_length

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

    def __call__(self):
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
        raise NotImplementedError
