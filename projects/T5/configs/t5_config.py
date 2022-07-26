from libai.config import LazyCall
from omegaconf import DictConfig

from projects.T5.models.t5_model import T5Model, T5ForPreTraining

cfg = dict(
    vocab_size=30522,
    hidden_size=768,
    hidden_layers=6,
    num_attention_heads=16,
    head_size=64,
    intermediate_size=1536,
    hidden_dropout_prob=0.0,
    attention_probs_dropout_prob=0.0,
    relative_attention_num_buckets=32,
    embedding_dropout_prob=0.0,
    num_tokentypes=0,
    initializer_range=0.02,
    layernorm_eps=1e-5,
    amp_enabled=False,
    mlp_type="mt5",
)

cfg = DictConfig(cfg)

t5_model = LazyCall(T5Model)(cfg=cfg)

pretrain_model = LazyCall(T5ForPreTraining)(cfg=cfg)
