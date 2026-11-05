"""LoRA-finetune Qwen3.5-4B on the distilled SFT set (runs on the GPU server).

Loss is computed only on the assistant reply; the prompt (system + retrieved
context + question, rendered with thinking disabled exactly as at serving time)
is context.  LoRA covers the language model's attention, linear-attention and
MLP projections -- the modules vLLM can serve as packed LoRA layers -- and leaves
the vision tower untouched.  An adapter is saved after every epoch.

    CUDA_VISIBLE_DEVICES=2 python train_lora.py --model <Qwen3.5-4B dir> --data data --out runs/r16
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
from pathlib import Path

import torch
import torch.nn.functional as F
import transformers
from peft import LoraConfig, get_peft_model
from transformers import AutoConfig, AutoTokenizer

TARGET_MODULES = (
    r"model\.language_model\.layers\.\d+\."
    r"(self_attn\.(q|k|v|o)_proj|linear_attn\.(in_proj_qkv|in_proj_z|out_proj)|mlp\.(gate|up|down)_proj)"
)


def encode(tokenizer, messages: list[dict]) -> tuple[list[int], int]:
    """Token ids of the full conversation and the index where the reply starts."""
    prompt = tokenizer.apply_chat_template(messages[:-1], tokenize=False, add_generation_prompt=True, enable_thinking=False)
    text = tokenizer.apply_chat_template(messages, tokenize=False, enable_thinking=False)
    if not text.startswith(prompt):
        raise ValueError("chat template renders the prompt differently with the reply appended")
    prompt_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
    ids = tokenizer(text.rstrip("\n"), add_special_tokens=False)["input_ids"]
    return ids, len(prompt_ids)


def load_split(tokenizer, path: Path, max_len: int) -> list[tuple[list[int], int]]:
    examples = []
    for line in path.read_text(encoding="utf-8").splitlines():
        ids, start = encode(tokenizer, json.loads(line)["messages"])
        if len(ids) <= max_len:
            examples.append((ids, start))
    return examples


def reply_loss(model, ids: list[int], start: int, device) -> tuple[torch.Tensor, int]:
    """Summed cross-entropy over the reply tokens; the LM head only runs on those positions."""
    input_ids = torch.tensor([ids], device=device)
    hidden = model.base_model.model.model(input_ids=input_ids).last_hidden_state
    logits = model.base_model.model.lm_head(hidden[0, start - 1 : len(ids) - 1]).float()
    targets = input_ids[0, start:]
    return F.cross_entropy(logits, targets, reduction="sum"), targets.numel()


@torch.no_grad()
def evaluate(model, examples, device) -> float:
    model.eval()
    total, count = 0.0, 0
    for ids, start in examples:
        loss, n = reply_loss(model, ids, start, device)
        total, count = total + loss.item(), count + n
    model.train()
    return total / count


def train(args: argparse.Namespace) -> None:
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = (out / "train.log").open("a", encoding="utf-8")

    def emit(record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    device = torch.device("cuda")
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    train_set = load_split(tokenizer, Path(args.data) / "train.jsonl", args.max_len)
    val_set = load_split(tokenizer, Path(args.data) / "val.jsonl", args.max_len)

    config = AutoConfig.from_pretrained(args.model)
    model_cls = getattr(transformers, config.architectures[0])
    model = model_cls.from_pretrained(args.model, dtype=torch.bfloat16, attn_implementation="sdpa").to(device)
    model.config.use_cache = False
    if args.grad_checkpointing:
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        model.enable_input_require_grads()
    lora = LoraConfig(r=args.rank, lora_alpha=args.alpha, lora_dropout=args.dropout, target_modules=TARGET_MODULES, task_type="CAUSAL_LM")
    model = get_peft_model(model, lora)
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    emit({"event": "start", "train": len(train_set), "val": len(val_set), "trainable_params": trainable, "args": vars(args)})

    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(params, lr=args.lr, weight_decay=0.0, betas=(0.9, 0.99))
    steps_per_epoch = math.ceil(len(train_set) / args.accum)
    total_steps = steps_per_epoch * args.epochs
    warmup = max(1, int(total_steps * 0.05))
    schedule = torch.optim.lr_scheduler.LambdaLR(
        optimizer,
        lambda s: (s + 1) / warmup if s < warmup else 0.5 * (1 + math.cos(math.pi * (s - warmup) / max(1, total_steps - warmup))),
    )

    emit({"event": "eval", "epoch": 0, "val_loss": round(evaluate(model, val_set, device), 4)})
    step, started = 0, time.time()
    model.train()
    for epoch in range(1, args.epochs + 1):
        order = train_set[:]
        random.shuffle(order)
        for i in range(0, len(order), args.accum):
            window = order[i : i + args.accum]
            reply_tokens = sum(len(ids) - start for ids, start in window)
            window_loss = 0.0
            for ids, start in window:
                loss, _ = reply_loss(model, ids, start, device)
                (loss / reply_tokens).backward()
                window_loss += loss.item()
            torch.nn.utils.clip_grad_norm_(params, 1.0)
            optimizer.step()
            schedule.step()
            optimizer.zero_grad(set_to_none=True)
            step += 1
            if step % args.log_every == 0 or step == total_steps:
                elapsed = time.time() - started
                emit({"event": "step", "epoch": epoch, "step": step, "of": total_steps, "loss": round(window_loss / reply_tokens, 4),
                      "lr": f"{schedule.get_last_lr()[0]:.2e}", "elapsed_min": round(elapsed / 60, 1),
                      "eta_min": round(elapsed / step * (total_steps - step) / 60, 1),
                      "max_mem_gb": round(torch.cuda.max_memory_allocated() / 2**30, 1)})
            if args.max_steps and step >= args.max_steps:
                emit({"event": "stop", "reason": "max_steps"})
                return
        model.save_pretrained(out / f"epoch{epoch}")
        emit({"event": "eval", "epoch": epoch, "val_loss": round(evaluate(model, val_set, device), 4), "saved": str(out / f"epoch{epoch}")})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", required=True)
    parser.add_argument("--data", required=True, help="dir with train.jsonl / val.jsonl from build_sft.py")
    parser.add_argument("--out", required=True)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--rank", type=int, default=16)
    parser.add_argument("--alpha", type=int, default=32)
    parser.add_argument("--dropout", type=float, default=0.05)
    parser.add_argument("--accum", type=int, default=16, help="sequences per optimizer step")
    parser.add_argument("--max-len", type=int, default=6144)
    parser.add_argument("--grad-checkpointing", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--log-every", type=int, default=5)
    parser.add_argument("--max-steps", type=int, default=0, help="stop early (smoke tests)")
    parser.add_argument("--seed", type=int, default=13)
    train(parser.parse_args())


if __name__ == "__main__":
    main()