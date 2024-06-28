def load_model(config, num_models):
    if config["model"]["architecture"] == "simple_model":
        from .simple_model import CoMP

        comp = CoMP(
            num_models,
            config["model"]["embed_dim"],
            8,
            config["model"]["embed_dim"] * 4,
            num_layers=1,
            num_classes=5 if loss_type == "ordinal_regression" else None,
        )
    elif config["model"]["architecture"] == "dcn":
        from .dcn import DCN

        comp = DCN(
            num_models=num_models,
            embed_dim=config["model"]["embed_dim"],
            dropout=config["model"]["dropout"],
            device="cuda",
            model_name=config["model"]["prompt_encoder"],
        )
    elif config["model"]["architecture"] == "deepseek":
        from .deepseek import DCN

        comp = DCN(
            num_models=num_models,
            embed_dim=config["model"]["embed_dim"],
            dropout=config["model"]["dropout"],
            device="cuda",
            model_name=config["model"]["prompt_encoder"],
        )
    elif config["model"]["architecture"] == "dcn_random":
        from .dcn_random import DCN

        comp = DCN(
            num_models=num_models,
            embed_dim=config["model"]["embed_dim"],
            dropout=config["model"]["dropout"],
            device="cuda",
            model_name=config["model"]["prompt_encoder"],
        )
    return comp
