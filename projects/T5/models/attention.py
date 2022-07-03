import math
from typing import Tuple

import oneflow as flow
from oneflow import nn

from libai.utils import distributed as dist
from libai.layers.embedding import Embedding
from libai.layers.linear import Linear


class MultiheadAttention(nn.Module):
    """Multi-head attention layer, support self attention and cross attention.

    Args:
        hidden_size: size of hidden state.
        num_attention_heads: number of attention heads.
        is_cross_attention: used to specify whether it is self attention or cross attention.
            Defaults to False.
        attention_dropout_prob: dropout probability of attention weights.
            Defaults to 0.0.
        output_dropout_prob: dropout probability of output. Defaults to 0.0.
        init_method: method to initialize the input layer weights.
            Defaults to ``init.xavier_normal_``.
        output_layer_init_method: method to initialize the output layer weights.
            If None, use ``init_method``.
        bias_dropout_fusion: whether to fuse add bias and dropout.
            Defaults to False.
        scale_mask_softmax_fusion: whether to fuse scale, mask and softmax.
            Defaults to False.
        apply_query_key_layer_scaling: if `True`, scaling the attention score by layer index.
            Defaults to False.
        layer_idx: a layer_idx sign which determines the placements.
            It will be used in pipeline parallelism. Defaults to 0.
    """

    def __init__(
        self,
        hidden_size,
        num_attention_heads,
        relative_attention_num_buckets,
        is_cross_attention=False,
        attention_dropout_prob=0.0,
        output_dropout_prob=0.0,
        init_method=nn.init.xavier_normal_,
        output_layer_init_method=None,
        bias_dropout_fusion=False,
        scale_mask_softmax_fusion=False,
        apply_query_key_layer_scaling=False,
        *,
        layer_idx=0,
        has_relative_attention_bias=False,
        is_decoder=False
    ):
        super().__init__()
        self.hidden_size = hidden_size
        self.relative_attention_num_buckets = relative_attention_num_buckets
        self.has_relative_attention_bias = has_relative_attention_bias
        self.is_decoder=is_decoder

        if output_layer_init_method is None:
            output_layer_init_method = init_method

        assert (
            hidden_size % num_attention_heads == 0
        ), "hidden_size must be divisible by num_attention_heads."

        self.num_heads = num_attention_heads
        self.head_size = hidden_size // num_attention_heads

        self.dropout = nn.Dropout(p=attention_dropout_prob)
        self.norm_factor = 1.0 / math.sqrt(float(self.head_size))
        self.coeff = None
        if apply_query_key_layer_scaling:
            self.coeff = layer_idx + 1
            self.norm_factor /= self.coeff

        self.is_cross_attention = is_cross_attention
        self.scale_mask_softmax_fusion = scale_mask_softmax_fusion
        self.bias_dropout_fusion = bias_dropout_fusion

        if self.bias_dropout_fusion:
            self.output_dropout_prob = output_dropout_prob
        else:
            self.output_dropout = nn.Dropout(p=output_dropout_prob)

        if self.is_cross_attention:
            self.query = Linear(
                self.hidden_size,
                self.hidden_size,
                bias=False,
                parallel="col",
                init_method=init_method,
                layer_idx=layer_idx,
            )
            self.key_value = Linear(
                self.hidden_size,
                self.hidden_size * 2,
                bias=False,
                parallel="col",
                init_method=init_method,
                layer_idx=layer_idx,
            )
        else:
            self.query_key_value = Linear(
                self.hidden_size,
                self.hidden_size * 3,
                bias=False,
                parallel="col",
                init_method=init_method,
                layer_idx=layer_idx,
            )

        self.dense = Linear(
            self.hidden_size,
            self.hidden_size,
            bias=False,
            parallel="row",
            init_method=output_layer_init_method,
            skip_bias_add=self.bias_dropout_fusion,
            layer_idx=layer_idx,
        )
        if self.has_relative_attention_bias:
            self.relative_attention_bias = Embedding(self.relative_attention_num_buckets, self.num_heads)

    def forward(
        self,
        hidden_states: flow.Tensor,
        encoder_states: flow.Tensor = None,
        attention_mask: flow.Tensor = None,
        past_key_value: Tuple[flow.Tensor, flow.Tensor] = None,
        use_cache: bool = False,
        position_bias=None,
        query_length=None,
    ):
        """

        Args:
            hidden_states (flow.Tensor): shape is [bsz, tgt_len, hidden_size].
            encoder_states (flow.Tensor, optional): shape is [bsz, src_len, hidden_size].
                Defaults to None.
            attention_mask (flow.Tensor, optional): shape is [bsz, 1, tgt_len, src_len].
                It should be the combination of padding mask and casual mask.
                It is the padding mask of source input when used with self-attention in encoder.
                And it is the combination of padding mask of target input and casual mask when
                used with self-attention in decoder. It is the padding mask of source input when
                used with cross-attention in decoder.
                Defaults to None.
            past_key_value (Tuple[flow.Tensor, flow.Tensor], optional): tuple of key and value,
                each shape is [bsz, num_heads, src_len, head_size]. Defaults to None.
            use_cache (bool, optional): it will be set to True, when the model is in the inference
                phase and used for incremental decoding. Defaults to False.
        """

        # hidden_states, encoder_states: [S(0), B]
        # attention_mask: [S(0), B]
        
        if encoder_states is not None:
            encoder_states = encoder_states.to_global(placement=hidden_states.placement)

        if attention_mask is not None:
            attention_mask = attention_mask.to_global(placement=hidden_states.placement)

        bsz, tgt_len = hidden_states.size()[:2]

        real_seq_length = tgt_len
        
        if past_key_value is not None:
            assert (
                len(past_key_value) == 2
            ), f"past_key_value should have 2 past states: keys and values. Got { len(past_key_value)} past states"
            real_seq_length += past_key_value[0].shape[2] if query_length is None else query_length
        
        key_length = real_seq_length if encoder_states is None else encoder_states.shape[1]

        if self.is_cross_attention:
            # if it is cross attention, key and value should be calculated only once, and the
            # result can be reused.
            query = self.query(hidden_states)
            query = query.view(bsz, -1, self.num_heads, self.head_size)
            query = query.permute(0, 2, 1, 3)
            if past_key_value is not None:
                key, value = past_key_value
            elif encoder_states is not None:
                key_value = self.key_value(encoder_states)
                key_value = key_value.view(bsz, -1, self.num_heads, 2 * self.head_size)
                key_value = key_value.permute(0, 2, 1, 3)
                key, value = flow.chunk(key_value, chunks=2, dim=-1)
            else:
                raise ValueError(
                    "past_key_value and encoder_states cannot be None at the same time."
                )
        else:
            # if it is self attention, query, key, and value are all obtained from hidden_states.
            # when in the inference phase of an incremental decoder,
            # hidden_states is the last-added state,
            # the full key and value could be obtained by concatenating with past_key_value.
            query_key_value = self.query_key_value(hidden_states)
            query_key_value = query_key_value.view(bsz, -1, self.num_heads, 3 * self.head_size)
            query_key_value = query_key_value.permute(
                0, 2, 1, 3
            )  # [bsz, num_heads, src_len, 3 * head_size]
            query, key, value = flow.chunk(query_key_value, chunks=3, dim=-1)
            if past_key_value is not None:
                past_key, past_value = past_key_value
                key = flow.cat((past_key.type_as(key), key), dim=2)
                value = flow.cat((past_value.type_as(value), value), dim=2)

        # query, key, value: [S(0), S(1)], shape: [bsz, num_heads, seq_length, head_size]
        if use_cache:
            past_key_value = (key, value)

        # [bsz, num_heads, tgt_len, src_len] with [S(0), S(1)]
        attention_scores = flow.matmul(query, key, transpose_b=True)


        if position_bias is None:
            if not self.has_relative_attention_bias:
                position_bias = flow.zeros(
                    (1, self.num_heads, real_seq_length, key_length), 
                    sbp=dist.get_nd_sbp([flow.sbp.broadcast, flow.sbp.broadcast]), 
                    placement=attention_scores.placement,
                )
            else:
                position_bias = self.compute_bias(
                    real_seq_length, 
                    key_length, 
                    placement=attention_scores.placement
                )

            if past_key_value is not None:
                position_bias = position_bias[:, :, -hidden_states.size(1) :, :]

            # print('position_bias',position_bias.size(),position_bias.sbp, position_bias.placement)
            # print('attention_mask',attention_mask.size(),attention_mask.sbp, position_bias.placement)
            sbp=attention_mask.sbp
            attention_mask = attention_mask.to_global(sbp=position_bias.sbp)
            position_bias += (1 - attention_mask)*-1000
            attention_mask = attention_mask.to_global(sbp=sbp)
            
        attention_scores += position_bias
        
        # [S(0), S(1)] x [S(0), B] = [S(0), S(1)]
        if attention_mask is not None:
            if self.scale_mask_softmax_fusion:
                attention_weights = flow._C.fused_scale_mask_softmax(
                    attention_scores, attention_mask, fill_value=-10000.0
                )
            else:
                if self.coeff is not None:
                    attention_scores *= self.coeff
                attention_scores = flow.mul(attention_scores, attention_mask)
                attention_scores = attention_scores - 10000.0 * (1 - attention_mask)
                # TODO(l1aoxingyu): graph will occur `where_scalar` errors when using `masked_fill`
                # attention_scores = attention_scores.masked_fill(1 - attention_mask, -10000.0)
                attention_weights = flow.softmax(attention_scores, dim=-1)
        else:
            attention_weights = flow.softmax(attention_scores, dim=-1)

        # [bsz, num_heads, tgt_len, src_len]
        attention_weights = self.dropout(attention_weights)

        # Context shape: [bsz, num_heads, tgt_len, head_size] with [S(0), S(1)]
        context = flow.matmul(attention_weights, value)
        # Change shape: [bsz, num_heads, tgt_len, head_size] -> [bsz, tgt_len, num_heads, head_size]
        context = context.transpose(1, 2)

        # Concat multi-head results from
        # [bsz, tgt_len, num_heads, head_size] -> [bsz, tgt_len, num_heads * head_size]
        # SBP sign: [S(0), S(2)]
        context = context.view(bsz, tgt_len, self.hidden_size)

        # [S(0), S(2)] x [B, S(0)] = [S(0), P] -> [S(0), B]
        output = self.dense(context)
        
        if self.bias_dropout_fusion:
            output, bias = output
            output = flow._C.fused_bias_add_dropout(
                output, bias, p=self.output_dropout_prob, axis=output.ndim - 1
            )
        else:
            output = self.output_dropout(output)

        if use_cache:
            output = (output, past_key_value)

        output = (output,) + (position_bias, )
        return output

    def extra_repr(self) -> str:
        return "hidden_size={}, num_heads={}, is_cross_attention={}".format(
            self.hidden_size,
            self.num_heads,
            self.is_cross_attention,
        )

    @staticmethod
    def _relative_position_bucket(relative_position, bidirectional=True, num_buckets=32, max_distance=128):
        # relative_position: (seq_len, seq_len)
        relative_buckets = 0
        if bidirectional:
            num_buckets //= 2
            relative_buckets += (relative_position > 0).to(flow.long) * num_buckets
            relative_position = flow.abs(relative_position)
        else:
            relative_position = -1 * flow.min(
                relative_position, 
                flow.zeros(
                    relative_position.size(),
                    sbp=relative_position.sbp, 
                    placement=relative_position.placement
                )
            ).to(flow.long)
        
        max_exact = num_buckets // 2
        is_small = relative_position < max_exact

        relative_postion_if_large = max_exact + (
            flow.log(relative_position.float() / max_exact)
            / math.log(max_distance / max_exact)
            * (num_buckets - max_exact)
        ).to(flow.long)

        relative_postion_if_large = flow.min(
            relative_postion_if_large, 
            flow.zeros(
                relative_postion_if_large.size(),
                dtype=relative_postion_if_large.dtype,
                sbp=relative_postion_if_large.sbp, 
                placement=relative_postion_if_large.placement
            ).fill_(num_buckets - 1)
        )
        
        relative_buckets += flow.where(is_small, relative_position, relative_postion_if_large)
        return relative_buckets

    def compute_bias(self, query_length, key_length, placement=None):
        """Compute binned relative position bias"""
        context_position = flow.arange(
            query_length, dtype=flow.long, sbp=dist.get_nd_sbp([flow.sbp.broadcast, flow.sbp.broadcast]), placement=placement
        )[:, None]
        memory_position = flow.arange(
            key_length, dtype=flow.long, sbp=dist.get_nd_sbp([flow.sbp.broadcast, flow.sbp.broadcast]), placement=placement
        )[None, :]
        relative_position = memory_position - context_position  # shape (query_length, key_length)
        
        relative_position_bucket = self._relative_position_bucket(
            relative_position,  # shape (query_length, key_length)
            bidirectional=(not self.is_decoder),
            num_buckets=self.relative_attention_num_buckets,
        )
        
        values = self.relative_attention_bias(relative_position_bucket)  # shape (query_length, key_length, num_heads)
        values = values.permute([2, 0, 1]).unsqueeze(0)  # shape (1, num_heads, query_length, key_length)
        return values
