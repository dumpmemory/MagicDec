from dataclasses import dataclass
from typing import Optional

import torch
import torch.nn as nn
from torch import Tensor
from torch.nn import functional as F
import torch.distributed as dist
import math 

def find_multiple(n: int, k: int) -> int:
    if n % k == 0:
        return n
    return n + k - (n % k)

@dataclass
class ModelArgs:
    block_size: int = 2048
    vocab_size: int = 32000
    n_layer: int = 32
    n_head: int = 32
    dim: int = 4096
    intermediate_size: int = None
    n_local_heads: int = -1
    head_dim: int = 64
    rope_base: float = 10000
    norm_eps: float = 1e-5
    scaling_factor:float = 1.0
    # llama 3.1 with high_freq_factor and low_freq_factor
    low_freq_factor: int = None # added new
    high_freq_factor: int = None  # added new
    original_max_position_embeddings: int = None   # added new

    def __post_init__(self):
        if self.n_local_heads == -1:
            self.n_local_heads = self.n_head
        if self.intermediate_size is None:
            hidden_dim = 4 * self.dim
            n_hidden = int(2 * hidden_dim / 3)
            self.intermediate_size = find_multiple(n_hidden, 256)
        self.head_dim = self.dim // self.n_head

    @classmethod
    def from_name(cls, name: str):
        if name in transformer_configs:
            return cls(**transformer_configs[name])
        # fuzzy search
        config = [config for config in transformer_configs if config.lower() in str(name).lower()]
        # We may have two or more configs matched (e.g. "7B" and "Mistral-7B"). Find the best config match,
        # take longer name (as it have more symbols matched)
        if len(config) > 1:
            config.sort(key=len, reverse=True)
            assert len(config[0]) != len(config[1]), name # make sure only one 'best' match
        print(config)
        return cls(**transformer_configs[config[0]])


transformer_configs = {
    "llama-2-7b": dict(block_size=4096, n_layer=32, n_head=32, dim=4096),
    'llama-2-7b-32k': dict(block_size=32768, n_layer=32, dim= 4096, vocab_size=32000, scaling_factor=8),
    "llama-2-13b": dict(block_size=4096, n_layer=40, n_head=40, dim=5120),
    "llama-2-70b": dict(block_size=4096, n_layer=80, n_head=64, dim=8192, n_local_heads=8, intermediate_size=28672),
    "llama-3-8b": dict(block_size=8192, n_layer=32, n_head=32, n_local_heads=8, dim=4096, intermediate_size=14336, vocab_size=128256, rope_base=500000),
    "llama-3-70b": dict(block_size=8192, n_layer=80, n_head=64, n_local_heads=8, dim=8192, intermediate_size=28672, vocab_size=128256, rope_base=500000),
    "68m": dict(block_size=2048, n_layer=2, n_head=12, n_local_heads=12, dim=768, intermediate_size=3072, vocab_size=32000),
    "tinyllama": dict(block_size =2048, n_layer=22, n_head=32, n_local_heads=4, dim=2048, intermediate_size=5632, vocab_size=32000),
    "llama-3.1-8b": dict(block_size=131072, n_layer=32, n_head=32, n_local_heads=8, dim=4096, intermediate_size=14336, vocab_size=128256, rope_base=500000.0, scaling_factor=8, high_freq_factor=4, low_freq_factor=1, original_max_position_embeddings=8192),
}

class KVCache(nn.Module):
    def __init__(self, max_num_pages, page_size, n_heads, head_dim, dtype=torch.bfloat16):
        super().__init__()
        cache_shape = (max_num_pages, 2, page_size, n_heads, head_dim)
        self.register_buffer('kv_cache', torch.zeros(cache_shape, dtype=dtype))
        
    def update(self, k, v, kv_append_indptr, kv_page_indices, kv_page_indptr, kv_page_lastlen):
        torch.ops.mylib.update_kv(
            k,
            v,
            kv_append_indptr,
            self.kv_cache,
            kv_page_indices,
            kv_page_indptr,
            kv_page_lastlen,
        )
        return self.kv_cache

class Transformer(nn.Module):
    def __init__(self, config: ModelArgs) -> None:
        super().__init__()
        self.config = config
        self.tok_embeddings = nn.Embedding(config.vocab_size, config.dim)
        self.layers = nn.ModuleList(TransformerBlock(config) for _ in range(config.n_layer))
        self.norm = RMSNorm(config.dim, eps=config.norm_eps)
        self.output = nn.Linear(config.dim, config.vocab_size, bias=False)
        self.freqs_cis: Optional[Tensor] = None

    def setup_caches(self, num_pages, page_size):

        head_dim = self.config.dim // self.config.n_head
        dtype = self.output.weight.dtype

        for b in self.layers:
            b.attention.kv_cache = KVCache(num_pages, page_size, self.config.n_local_heads, head_dim, dtype)
            b.attention.attn_decode = torch.ops.mylib.target_decode
            b.attention.attn_prefill = torch.ops.mylib.target_prefill

        if (self.config.high_freq_factor is not None) and (self.config.low_freq_factor is not None):
            self.freqs_cis = precompute_freqs_cis(self.config.block_size, self.config.dim // self.config.n_head, self.config.rope_base,dtype,
                                                  self.config.scaling_factor, self.config.low_freq_factor, self.config.high_freq_factor, self.config.original_max_position_embeddings)
        else:
            self.freqs_cis = precompute_freqs_cis(self.config.block_size, self.config.dim // self.config.n_head, self.config.rope_base,dtype,
                                                  self.config.scaling_factor)

    def forward(self, idx: Tensor, input_pos: Optional[Tensor], kv_append_indptr: Tensor, kv_page_indices: Tensor, kv_page_indptr: Tensor, kv_page_lastlen: Tensor) -> Tensor:
        freqs_cis = self.freqs_cis[input_pos]
        x = self.tok_embeddings(idx)
        for i, layer in enumerate(self.layers):
            x = layer(x, freqs_cis, kv_append_indptr, kv_page_indices, kv_page_indptr, kv_page_lastlen)
        x = self.norm(x)
        logits = self.output(x)
        return logits

    def prefill(self, idx: Tensor, input_pos: Optional[Tensor], kv_append_indptr: Tensor, kv_page_indices: Tensor, kv_page_indptr: Tensor, kv_page_lastlen: Tensor) -> Tensor:
        freqs_cis = self.freqs_cis[input_pos]
        x = self.tok_embeddings(idx)
        for i, layer in enumerate(self.layers):
            x = layer.prefill(x, freqs_cis, kv_append_indptr, kv_page_indices, kv_page_indptr, kv_page_lastlen)
        x = self.norm(x)
        logits = self.output(x)
        return logits

    @classmethod
    def from_name(cls, name: str):
        return cls(ModelArgs.from_name(name))


class TransformerBlock(nn.Module):
    def __init__(self, config: ModelArgs) -> None:
        super().__init__()
        self.attention = Attention(config)
        self.feed_forward = FeedForward(config)
        self.ffn_norm = RMSNorm(config.dim, config.norm_eps)
        self.attention_norm = RMSNorm(config.dim, config.norm_eps)

    def forward(self, x: Tensor, freqs_cis: Tensor, kv_append_indptr: Tensor, kv_page_indices: Tensor, kv_page_indptr: Tensor, kv_page_lastlen: Tensor) -> Tensor:
        h = x + self.attention(self.attention_norm(x), freqs_cis, kv_append_indptr, kv_page_indices, kv_page_indptr, kv_page_lastlen)
        out = h + self.feed_forward(self.ffn_norm(h))
        return out

    def prefill(self, x: Tensor, freqs_cis: Tensor, kv_append_indptr: Tensor, kv_page_indices: Tensor, kv_page_indptr: Tensor, kv_page_lastlen: Tensor) -> Tensor:
        h = x + self.attention.prefill(self.attention_norm(x), freqs_cis, kv_append_indptr, kv_page_indices, kv_page_indptr, kv_page_lastlen)
        out = h + self.feed_forward(self.ffn_norm(h))
        return out


class Attention(nn.Module):
    def __init__(self, config: ModelArgs):
        super().__init__()
        assert config.dim % config.n_head == 0

        total_head_dim = (config.n_head + 2 * config.n_local_heads) * config.head_dim
        # key, query, value projections for all heads, but in a batch
        self.wqkv = nn.Linear(config.dim, total_head_dim, bias=False)
        self.wo = nn.Linear(config.dim, config.dim, bias=False)
        self.kv_cache = None
        self.process_group = None
        self.attn_decode = None
        self.attn_prefill = None

        self.n_head = config.n_head
        self.head_dim = config.head_dim
        self.n_local_heads = config.n_local_heads
        self.dim = config.dim
        self._register_load_state_dict_pre_hook(self.load_hook)

    def load_hook(self, state_dict, prefix, *args):
        if prefix + "wq.weight" in state_dict:
            wq = state_dict.pop(prefix + "wq.weight")
            wk = state_dict.pop(prefix + "wk.weight")
            wv = state_dict.pop(prefix + "wv.weight")
            state_dict[prefix + "wqkv.weight"] = torch.cat([wq, wk, wv])

    def forward(self, x: Tensor, freqs_cis: Tensor, kv_append_indptr: Tensor, kv_page_indices: Tensor, kv_page_indptr: Tensor, kv_page_lastlen: Tensor) -> Tensor:
        bsz, seqlen, _ = x.shape

        kv_size = self.n_local_heads * self.head_dim
        q, k, v = self.wqkv(x).split([self.dim, kv_size, kv_size], dim=-1)

        q = q.view(bsz, seqlen, self.n_head, self.head_dim)
        k = k.view(bsz, seqlen, self.n_local_heads, self.head_dim)
        v = v.view(bsz, seqlen, self.n_local_heads, self.head_dim).contiguous().view(-1, self.n_local_heads, self.head_dim)
        q = apply_rotary_emb(q, freqs_cis).contiguous().view(-1, self.n_head, self.head_dim)
        k = apply_rotary_emb(k, freqs_cis).contiguous().view(-1, self.n_local_heads, self.head_dim)

        kv_cache = self.kv_cache.update(k, v, kv_append_indptr, kv_page_indices, kv_page_indptr, kv_page_lastlen)

        y = self.attn_decode(q, kv_cache)

        y = y.contiguous().view(bsz, seqlen, self.dim)

        y = self.wo(y)
        if self.process_group != None:
            dist.all_reduce(y)
        return y

    def prefill(self, x: Tensor, freqs_cis: Tensor, kv_append_indptr: Tensor, kv_page_indices: Tensor, kv_page_indptr: Tensor, kv_page_lastlen: Tensor) -> Tensor:
        bsz, seqlen, _ = x.shape

        kv_size = self.n_local_heads * self.head_dim
        q, k, v = self.wqkv(x).split([self.dim, kv_size, kv_size], dim=-1)

        q = q.view(bsz, seqlen, self.n_head, self.head_dim)
        k = k.view(bsz, seqlen, self.n_local_heads, self.head_dim)
        v = v.view(bsz, seqlen, self.n_local_heads, self.head_dim).contiguous().view(-1, self.n_local_heads, self.head_dim)

        q = apply_rotary_emb(q, freqs_cis).contiguous().view(-1, self.n_head, self.head_dim)
        k = apply_rotary_emb(k, freqs_cis).contiguous().view(-1, self.n_local_heads, self.head_dim)

        kv_cache = self.kv_cache.update(k, v, kv_append_indptr, kv_page_indices, kv_page_indptr, kv_page_lastlen)

        y = self.attn_prefill(q, kv_cache)

        y = y.contiguous().view(bsz, seqlen, self.dim)

        y = self.wo(y)
        if self.process_group != None:
            dist.all_reduce(y)
        return y

class FeedForward(nn.Module):
    def __init__(self, config: ModelArgs) -> None:
        super().__init__()
        self.w1 = nn.Linear(config.dim, config.intermediate_size, bias=False)
        self.w3 = nn.Linear(config.dim, config.intermediate_size, bias=False)
        self.w2 = nn.Linear(config.intermediate_size, config.dim, bias=False)
        self.process_group = None

    def forward(self, x: Tensor) -> Tensor:
        y = self.w2(F.silu(self.w1(x)) * self.w3(x))
        if self.process_group != None:
            dist.all_reduce(y)
        return y


class RMSNorm(nn.Module):
    def __init__(self, dim: int, eps: float = 1e-5):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def _norm(self, x):
        return x * torch.rsqrt(torch.mean(x * x, dim=-1, keepdim=True) + self.eps)

    def forward(self, x: Tensor) -> Tensor:
        output = self._norm(x.float()).type_as(x)
        return output * self.weight


def _compute_llama3_parameters(inv_freq, old_context_len=8192, scaling_factor=8,low_freq_factor=1,high_freq_factor=4):
    """
    To be used for llama 3.1 models
        - borrowing the logic from: https://github.com/huggingface/transformers/blob/c85510f958e6955d88ea1bafb4f320074bfbd0c1/src/transformers/modeling_rope_utils.py
        - source: _compute_llama3_parameters in modeling_rope_utils.py
    """
    low_freq_wavelen = old_context_len / low_freq_factor
    high_freq_wavelen = old_context_len / high_freq_factor
    new_freqs = []
    for freq in inv_freq:
        wavelen = 2 * math.pi / freq
        if wavelen < high_freq_wavelen:
            new_freqs.append(freq)
        elif wavelen > low_freq_wavelen:
            new_freqs.append(freq / scaling_factor)
        else:
            assert low_freq_wavelen != high_freq_wavelen
            smooth = (old_context_len / wavelen - low_freq_factor) / (high_freq_factor - low_freq_factor)
            new_freqs.append((1 - smooth) * freq / scaling_factor + smooth * freq)
    inv_freq = torch.tensor(new_freqs, dtype=inv_freq.dtype, device=inv_freq.device)
    return inv_freq

def precompute_freqs_cis(
    seq_len: int, n_elem: int, base: int = 10000,
    dtype: torch.dtype = torch.bfloat16,
    scaling_factor: float = 1.0, # added new 
    low_freq_factor: int = None, # added new
    high_freq_factor: int = None, # added new
    original_max_position_embeddings: int = None, # added new
) -> Tensor:
    print(f"target: seq_len: {seq_len}, n_elem: {n_elem}, base: {base}, dtype: {dtype}, scaling_factor: {scaling_factor}, low_freq_factor: {low_freq_factor}, high_freq_factor: {high_freq_factor}, original_max_position_embeddings: {original_max_position_embeddings}"
          )
    freqs = 1.0 / (base ** (torch.arange(0, n_elem, 2)[: (n_elem // 2)].float() / n_elem))
    
    if (low_freq_factor is not None) and (high_freq_factor is not None):
        freqs = _compute_llama3_parameters(freqs, original_max_position_embeddings, scaling_factor, low_freq_factor,high_freq_factor)
    else:
        freqs /= scaling_factor
    t = torch.arange(seq_len, device=freqs.device, dtype=freqs.dtype)
    freqs = torch.outer(t, freqs)
    freqs_cis = torch.polar(torch.ones_like(freqs), freqs)
    cache = torch.stack([freqs_cis.real, freqs_cis.imag], dim=-1)
    return cache.to(dtype=dtype)



def apply_rotary_emb(x: Tensor, freqs_cis: Tensor) -> Tensor:
    xshaped = x.float().reshape(*x.shape[:-1], -1, 2)
    freqs_cis = freqs_cis.view(x.shape[0], xshaped.size(1), 1, xshaped.size(3), 2)
    x_out2 = torch.stack(
        [
            xshaped[..., 0] * freqs_cis[..., 0] - xshaped[..., 1] * freqs_cis[..., 1],
            xshaped[..., 1] * freqs_cis[..., 0] + xshaped[..., 0] * freqs_cis[..., 1],
        ],
        -1,
    )

    x_out2 = x_out2.flatten(3)
    return x_out2.type_as(x)