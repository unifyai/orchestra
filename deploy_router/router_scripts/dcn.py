# DCN: deep cross network, taking inspiration from
# https://hci.stanford.edu/publications/2022/gordon_jury_learning_chi22.pdf

import torch
import torch.nn as nn
from transformers import AutoModel


class CrossNetwork(nn.Module):
    def __init__(self, num_cross_layers, num_fc_layers, d_e, dropout):
        super().__init__()
        self.cross_layers = nn.ModuleList(
            [nn.Linear(d_e, d_e) for _ in range(num_cross_layers)],
        )
        self.fc_layers = nn.ModuleList(
            [nn.Linear(d_e, d_e) for _ in range(num_fc_layers)],
        )
        self.act_fn = nn.functional.gelu
        self.predict = nn.Linear(d_e, 1)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        x_in = x
        for l in self.cross_layers:
            x = x_in * l(x) + x

        for l in self.fc_layers:
            x = l(x)
            x = self.act_fn(x)
            x = self.dropout(x)

        return self.predict(x)


class DCN(nn.Module):
    def __init__(
        self,
        num_models,
        embed_dim,
        dropout=0.2,
        device="cuda",
        model_name="microsoft/deberta-v3-xsmall",
    ):
        super().__init__()
        self.prompt_encoder = AutoModel.from_pretrained(model_name)
        self.model_embedding = nn.Embedding(num_models, embed_dim)
        d_cn = self.prompt_encoder.config.hidden_size
        self.prompt_proj = nn.Linear(
            self.prompt_encoder.config.hidden_size,
            d_cn - embed_dim,
        )
        self.dev = device
        self.dropout = torch.nn.Dropout(dropout)
        self.cn = CrossNetwork(
            num_cross_layers=3,
            num_fc_layers=3,
            d_e=d_cn,
            dropout=dropout,
        )

    def forward(self, datum_id, model_id, attn_mask):
        datum_id = datum_id.to(self.dev)
        prompt_emb = self.prompt_encoder(
            input_ids=datum_id,
            attention_mask=attn_mask.to(self.dev),
        ).last_hidden_state
        model_id = model_id.to(self.dev)
        model_emb = self.model_embedding(model_id)
        x = prompt_emb[:, 0]
        x = self.prompt_proj(x)
        x_cat = torch.cat([x, model_emb], dim=-1)
        x_cat = self.dropout(x_cat)
        x = self.cn(x_cat)
        return x
