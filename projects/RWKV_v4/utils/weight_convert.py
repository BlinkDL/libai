import logging

import oneflow as flow
import torch
import numpy as np

logger = logging.getLogger(__name__)


def convert_qkv_weight(cfg, value):
    """
    Convert qkv.weight to be compatible with LiBai transformer layer

    Args:
        cfg: config file
        value: qkv.weight in the loaded checkpoint
    """
    num_heads = cfg.model.num_heads
    hidden_size = cfg.model.embed_dim
    head_size = int(hidden_size / num_heads)
    qkv_weight = (
        value.view([3, num_heads, head_size, hidden_size])
        .permute(1, 0, 2, 3)
        .contiguous()
        .view(hidden_size * 3, hidden_size)
    )
    return qkv_weight


def convert_qkv_bias(cfg, value):
    """
    Convert qkv.bias to be compatible with LiBai transformer layer

    Args:
        cfg: config file
        value: qkv.bias in the loaded checkpoint
    """
    num_heads = cfg.model.num_heads
    hidden_size = cfg.model.embed_dim
    head_size = int(hidden_size / num_heads)
    qkv_bias = (
        value.view(3, num_heads, head_size).permute(1, 0, 2).contiguous().view(hidden_size * 3)
    )
    return qkv_bias


def filter_keys(key, value, cfg):
    """
    Filtering the state_dict keys and values to match LiBai's MAE model
    """
    if "norm1" in key:
        key = key.replace("norm1", "input_layernorm")
    elif "attn.qkv" in key:
        key = key.replace("attn.qkv", "self_attention.query_key_value")
        if "weight" in key:
            value = convert_qkv_weight(cfg, value)
        if "bias" in key:
            value = convert_qkv_bias(cfg, value)
    elif "attn.proj" in key:
        key = key.replace("attn.proj", "self_attention.dense")
    elif "norm2" in key:
        key = key.replace("norm2", "post_attention_layernorm")
    elif "mlp.fc1" in key:
        key = key.replace("mlp.fc1", "mlp.dense_h_to_4h")
    elif "mlp.fc2" in key:
        key = key.replace("mlp.fc2", "mlp.dense_4h_to_h")
    elif "fc_norm" in key:
        key = key.replace("fc_norm", "norm")

    return key, value


def load_torch_checkpoint(model, cfg, path="/home/zhangxiaoyu/RWKV-LM/RWKV-v4/trained-1.pth", strict=False):
    """
    Load checkpoint from the given torch weights.
    Torch weight can be downloaded from the original repo:
        https://github.com/facebookresearch/mae
    """
    # torch_dict = torch.load(path, map_location="cpu")["model"]
    torch_dict = torch.load(path, map_location="cpu")

    parameters = torch_dict
    new_parameters = dict()
    for key, value in parameters.items():
        if "num_batches_tracked" not in key:
            # to global tensor
            key, val = filter_keys(key, value, cfg)
            val = val.detach().cpu().numpy().astype(np.float32)
            val = flow.tensor(val).to_global(
                sbp=flow.sbp.broadcast, placement=flow.placement("cuda", ranks=[0])
            )
            new_parameters[key] = val
    model.load_state_dict(new_parameters, strict=True)
    print("Successfully load torch RWKV checkpoint.")
    return model
