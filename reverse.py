import json

# Load the JSON data
with open("best_metrics.json") as f:
    data = json.load(f)

# Initialize the transformed data dictionary
transformed_data = {}

# Iterate over the keys and values in the original data
for model, configs in data.items():
    for config, provider_value in configs.items():
        # Get the provider and value
        provider, value = provider_value

        # Check if the model is already in the transformed data
        if model in transformed_data and provider in transformed_data[model]:
            # If it is, add the config, provider, and value to the model's data
            transformed_data[model][provider].append(config)
        else:
            # If it's not, add the model to the transformed data with the config, provider, and value
            transformed_data[model] = {provider: [config]}
        print("transformed data is ", transformed_data)

# Print the transformed data
print(json.dumps(transformed_data, indent=4))
with open("readable_best_metrics.json", "w") as f:
    json.dump(transformed_data, f, indent=4)
