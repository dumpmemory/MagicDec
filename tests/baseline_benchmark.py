import time
import torch
import sys
sys.path.append("..")
from pathlib import Path
import torch.distributed as dist
from MagicDec.Engine.utils import setup_seed, sampling_argmax_batch, cuda_graph_for_sampling_argmax_batch
from MagicDec.Data.data_converter import convert_pg19_dataset
from transformers import AutoTokenizer
from torch.utils.data.dataloader import DataLoader
from tqdm import tqdm
import argparse
from MagicDec.Engine.backend import LMBackend

parser = argparse.ArgumentParser(description='Process model configuration and partitions.')
parser.add_argument('--model', type=Path, default=Path("checkpoints/meta-llama/Meta-Llama-3.1-8B/model.pth"), help='model')
parser.add_argument('--model_name', type=str, default="meta-llama/Meta-Llama-3.1-8B", help='model name')

parser.add_argument('--B', type=int, default=8, help='Batch size.')
parser.add_argument('--prefix_len', type=int, default=4000, help='Prefix length')
parser.add_argument('--max_len', type=int, default=4096, help='Generate length')

parser.add_argument('--seed', type=int, default=123, help='Random seed.')

parser.add_argument('--compile', action='store_true', help='Whether to compile the model.')
parser.add_argument('--rank_group', nargs='+', type=int, help='Target group of ranks')
parser.add_argument('--printoutput', action='store_true', help='Whether to compile the model.')

args = parser.parse_args()
assert args.prefix_len < args.max_len
assert args.max_len % 128 == 0

DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
global print
from MagicDec.Engine.tp import init_dist
use_tp = len(args.rank_group) > 1
global_group = None
if use_tp:
    rank, global_group = init_dist()
    if rank != args.rank_group[0]:
        # only print on rank 0
        print = lambda *args, **kwargs: None
setup_seed(args.seed)
print(f"Using device={DEVICE}")
# MAX_LEN = args.prefix_len + args.gen_len - 1
MAX_LEN = args.max_len
DTYPE = torch.bfloat16
BATCH_SIZE = args.B
checkpoint_path = args.model

engine = LMBackend(dtype=DTYPE, device=DEVICE)
engine.load_model(checkpoint_path, use_tp=use_tp, rank_group = args.rank_group, group=global_group)
if args.compile:
    engine.compile()

engine.setup_caches(max_batch_size=BATCH_SIZE, max_seq_length=MAX_LEN)

tokenizer = AutoTokenizer.from_pretrained(args.model_name)
tokenizer.pad_token = tokenizer.eos_token
eot_1 = tokenizer.eos_token_id
if tokenizer.unk_token_id is not None:
    eot_2 = tokenizer.unk_token_id
else:
    eot_2 = tokenizer.encode("<|eot_id|>")[-1]
print(f"eot_1: {eot_1}, eot_2: {eot_2}")

dataset = convert_pg19_dataset(tokenizer=tokenizer, seq_len=args.prefix_len)
dataloader = DataLoader(dataset, batch_size=BATCH_SIZE, shuffle=False, drop_last=True)
num_eval_steps = min(20, len(dataloader))

# vocab_size = engine.model.config.vocab_size
# target_sample = cuda_graph_for_sampling_argmax_batch(device=DEVICE, dtype=DTYPE, batch_size=BATCH_SIZE, idx_len=1, dim=vocab_size)

total_time = 0.0
model_steps = 0
for step, batch in tqdm(enumerate(dataloader), total=num_eval_steps):
    if step >= num_eval_steps:
        break
    input_ids = batch[0].to(DEVICE)
    terminate = False
    output = input_ids.clone()

    # logits = engine.encode(input_ids=input_ids)[:,-1]
    # next_tokens = sampling_argmax_batch(logits=logits)

    next_tokens = engine.encode(input_ids=input_ids)[:,-1:]
    output = torch.cat((output, next_tokens),dim=-1)
    torch.cuda.synchronize()
    t1 = time.perf_counter()
    while output.size(1)<MAX_LEN and terminate == False:
        input_ids=next_tokens.clone()
        # logits = engine.inference(input_ids=input_ids)[:, -1]
        # next_tokens = sampling_argmax_batch(logits=logits)

        # logits = engine.inference(input_ids=input_ids)
        # next_tokens = target_sample(logits)

        next_tokens = engine.inference(input_ids=input_ids)
        output = torch.cat((output, next_tokens),dim=-1)
        model_steps += 1
        if (next_tokens[:,-1] == eot_1)._is_any_true() or (next_tokens[:,-1] == eot_2)._is_any_true(): terminate = True
    torch.cuda.synchronize()
    t2=time.perf_counter()
    total_time += t2 - t1

    if args.printoutput:
        for i in range(BATCH_SIZE):
            print(tokenizer.decode(output[i, args.prefix_len:]))

    print(f"Tokens per second :{total_time/model_steps}")
    if step < 10:
        total_time = 0.0
        model_steps = 0
    if use_tp:
        dist.barrier()