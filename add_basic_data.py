from sqlalchemy import create_engine, insert
from cloud_db import get_cloud_sql_data
from orchestra.db.models.orchestra_models import (
    Modality,
    Task,
    Model,
    Provider,
    Endpoint,
    Users,
)

# PostgreSQL database URL
database_url = f"postgresql://orchestra:orchestra@localhost/orchestra"
local_engine = create_engine(database_url)

tables = {
    "modality": {"model": Modality},
    "task": {"model": Task},
    "model": {"model": Model},
    "provider": {"model": Provider},
    "endpoint": {"model": Endpoint},
    "users": {"model": Users},
}
data = get_cloud_sql_data(list(tables.keys()))
data = {
    table: {"model": tables[table]["model"], "rows": data[table]} for table in tables
}
data["users"] = [["clxlmko0900539b72enccyi1o", 0, "", "f", 0, 0, "t"]]

with local_engine.connect() as conn:
    for key, content in data.items():
        print(f"key {key}")
        model = content["model"]
        rows = content["rows"]
        stmt = insert(model)
        conn.execute(stmt.values(rows))
        conn.commit()


# """
# # the new dataset evaluation stuff
# just copy over the standard dataset info in the script
# dataset
# evaluator
# stored_prompt
# dataset_prompt
# evaluation
# stored_prompt_extra_field
# stored_prompt_response
# judgement


# needed but don't populate
# custom_api_key
# custom_endpoint
# custom_router
# local_endpoint
# query
# tags
# """


# import os
# from litellm import Router

# model_list = [{
#         "model_name": "gpt-4o",
#         "litellm_params": { # params for litellm completion/embedding call
#             "model": "gpt-4o",
#             "api_key": "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"
#         },
#     }, {
#     "model_name": "gpt-3.5-turbo",
#     "litellm_params": { # params for litellm completion/embedding call
#         "model": "gpt-3.5-turbo",
#         "api_key": "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"
#     },
# }]
# router = Router(
#     model_list=model_list,
#     routing_strategy="usage-based-routing-v2", # 👈 KEY CHANGE
# )

# response = router.completion(
#     model="gpt-3.5-turbo",
#     messages=[{"role": "user", "content": "Hey, how's it going?"}]
# )

# print(response)

# response = router.completion(
#     model="bad-model",
#     messages=[{"role": "user", "content": "Hey, how's it going?"}],
#     # mock_testing_fallbacks=True,
# )
