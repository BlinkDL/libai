# coding=utf-8
# Copyright 2021 The OneFlow Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import oneflow.nn as nn

from libai.layers.droppath import DropPath
from libai.utils import distributed as dist
from projects.T5.models.attention import MultiheadAttention
from projects.T5.models.layer_norm import LayerNorm
from projects.T5.models.mlp import MT5MLP, T5MLP


class TransformerLayer(nn.Module):
    """A single transformer layer.

    Transformer layer takes input with size [bsz, seq_length, hidden size] and returns an
    output of the same size.
    The input and output has same sbp sign, (S(0), B).

    Arguments:
        hidden_size: size of hidden state.
        ffn_hidden_size: size of feed forword neural network.
        num_attention_heads: number of attention heads.
        is_decoder: used to specify whether this is transformer encoder layer or transformer
            decoder layer. Default: ``False``.
        attention_dropout_prob: dropout probability of attention weights.
        output_dropout_prob: dropout probability of output.
        layernorm_epsilon: epsilon used in layernorm layer. Default: `1e-5`.
        init_method: method to initialize the input layer weights.
        output_layer_init_method: method to initialize the output layer weights.
            If None, use `init_method`.
        layer_idx: the layer index, which determines the placement.
    """

    def __init__(
        self,
        hidden_size,
        ffn_hidden_size,
        num_attention_heads,
        head_size,
        relative_attention_num_buckets,
        is_decoder=False,
        attention_dropout_prob=0.0,
        output_dropout_prob=0.0,
        drop_path_prob=0.0,
        layernorm_epsilon=1e-5,
        init_method=nn.init.xavier_normal_,
        output_layer_init_method=None,
        *,
        layer_idx=0,
        mlp_type="t5",
        has_relative_attention_bias=False
    ):
        super().__init__()
        self.hidden_size = hidden_size
        self.ffn_hidden_size = ffn_hidden_size
        self.num_attention_heads = num_attention_heads
        self.head_size = head_size
        self.attention_dropout_prob = attention_dropout_prob
        self.output_dropout_prob = output_dropout_prob
        self.layernorm_epsilon = layernorm_epsilon
        self.layer_idx = layer_idx
        self.is_decoder = is_decoder

        self.init_method = init_method
        if output_layer_init_method is None:
            output_layer_init_method = init_method
        self.output_layer_init_method = output_layer_init_method

        self.drop_path = DropPath(drop_path_prob) if drop_path_prob > 0.0 else nn.Identity()

        self.input_layernorm = LayerNorm(
            self.hidden_size, eps=self.layernorm_epsilon, layer_idx=self.layer_idx
        )

        self.self_attention = self.build_attention(
            is_cross_attention=False,
            relative_attention_num_buckets=relative_attention_num_buckets,
            has_relative_attention_bias=has_relative_attention_bias,
            is_decoder=self.is_decoder,
        )
        self.post_attention_layernorm = LayerNorm(
            self.hidden_size, eps=self.layernorm_epsilon, layer_idx=self.layer_idx
        )

        if self.is_decoder:
            self.cross_attention = self.build_attention(
                is_cross_attention=True,
                relative_attention_num_buckets=relative_attention_num_buckets,
                is_decoder=self.is_decoder,
            )
            self.post_cross_attention_layernorm = LayerNorm(
                self.hidden_size, eps=self.layernorm_epsilon, layer_idx=self.layer_idx
            )
        if mlp_type == "mt5":
            self.mlp = MT5MLP(
                self.hidden_size,
                self.ffn_hidden_size,
                self.output_dropout_prob,
                self.init_method,
                output_layer_init_method=self.output_layer_init_method,
                layer_idx=self.layer_idx,
            )
        elif mlp_type == "t5":
            self.mlp = T5MLP(
                self.hidden_size,
                self.ffn_hidden_size,
                self.output_dropout_prob,
                self.init_method,
                output_layer_init_method=self.output_layer_init_method,
                layer_idx=self.layer_idx,
            )

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        encoder_states=None,
        encoder_attention_mask=None,
        past_key_value=None,
        use_cache=False,
        position_bias=None,
        encoder_decoder_position_bias=None,
    ):
        """
        Args:
            hidden_states: shape is (batch_size, seq_length, hidden_size),
                sbp signature is (S(0), B).
            attention_mask: the combination of key padding mask and casual mask of hidden states
                with shape (batch_size, 1, seq_length, seq_length) and the sbp
                signature is (S(0), B),
            encoder_states: encoder output with shape (batch_size, seq_length, hidden_size)
                and the sbp signature is (S(0), B), which will be used in cross attention.
            encoder_attention_mask: key padding mask of encoder states with shape
                (batch_size, 1, seq_length, seq_length) and the sbp signature is (S(0), B).
            past_key_value: tuple of key and value, each shape is
                (seq_length, bsz, num_heads, head_size), For decoder layer,
                the past_key_value contains the states both from self attention
                and cross attention.
            use_cache: it will be set to `True` when the model is in the inference phase and
                used for incremental decoding.
        """
        # Change placement for pipeline parallelsim
        hidden_states = hidden_states.to_global(placement=dist.get_layer_placement(self.layer_idx))

        # hidden_states shape: (batch_size, seq_length, hidden_size)
        if attention_mask is not None:
            attention_mask = attention_mask.to_global(
                placement=dist.get_layer_placement(self.layer_idx)
            )

        if past_key_value is not None:
            if self.is_decoder:
                assert len(past_key_value) == 4
                self_attn_past_key_value = past_key_value[:2]
                cross_attn_past_key_value = past_key_value[2:]
            else:
                self_attn_past_key_value = past_key_value
                cross_attn_past_key_value = None
        else:
            self_attn_past_key_value, cross_attn_past_key_value = None, None

        layernorm_output = self.input_layernorm(hidden_states)

        attention_output, position_bias = self.self_attention(
            layernorm_output,
            attention_mask=attention_mask,
            past_key_value=self_attn_past_key_value,
            position_bias=position_bias,
            use_cache=use_cache,
        )

        attention_output = self.drop_path(attention_output)

        if use_cache:
            attention_output, presents = attention_output
        else:
            presents = None

        hidden_states = hidden_states + attention_output

        layernorm_output = self.post_attention_layernorm(hidden_states)

        if self.is_decoder:
            if presents is not None:
                query_length = presents[0].shape[2]
            else:
                query_length = None

            attention_output, encoder_decoder_position_bias = self.cross_attention(
                layernorm_output,
                encoder_states,
                attention_mask=encoder_attention_mask,
                past_key_value=cross_attn_past_key_value,
                position_bias=encoder_decoder_position_bias,
                use_cache=use_cache,
                query_length=query_length,
            )
            if use_cache:
                attention_output, decoder_presents = attention_output
                presents = presents + decoder_presents

            attention_output = self.drop_path(attention_output)

            hidden_states = hidden_states + attention_output
            layernorm_output = self.post_cross_attention_layernorm(hidden_states)

        mlp_output = self.mlp(layernorm_output)
        mlp_output = self.drop_path(mlp_output)

        output = hidden_states + mlp_output

        if use_cache:
            output = (output, presents)
        output = (output,) + (position_bias,)
        if self.is_decoder:
            output = output + (encoder_decoder_position_bias,)
        return output

    def build_attention(
        self,
        is_cross_attention=False,
        relative_attention_num_buckets=None,
        has_relative_attention_bias=False,
        is_decoder=False,
    ):
        return MultiheadAttention(
            self.hidden_size,
            self.num_attention_heads,
            head_size=self.head_size,
            relative_attention_num_buckets=relative_attention_num_buckets,
            is_cross_attention=is_cross_attention,
            attention_dropout_prob=self.attention_dropout_prob,
            output_dropout_prob=self.output_dropout_prob,
            init_method=self.init_method,
            output_layer_init_method=self.output_layer_init_method,
            layer_idx=self.layer_idx,
            has_relative_attention_bias=has_relative_attention_bias,
            is_decoder=is_decoder,
        )
