"""Prompt construction and answer generation with a local Ollama model."""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import ollama

from rag.config import get_settings
from rag.retrieve.retriever import Retrieval, Source

NUM_CTX = 8192
CONTEXT_CHAR_BUDGET = 14_000  # about 3.5k tokens, leaving room for the answer

SYSTEM_PROMPT = """You answer questions about the user's own email, using only the numbered context blocks provided.

Rules:
- Use only facts stated in the context. Never invent senders, dates, numbers or events.
- Cite the block number in square brackets after each fact, like [1] or [2][3].
- If the context does not contain the answer, say you could not find it in the emails.
- Dates in the context are authoritative. Resolve "last" and "recent" by comparing them.
- Read every block before answering and cover all that are relevant, especially the most recent ones.
- Blocks are in chronological order. If a later email changes a figure or decision from an earlier one, report the latest value and say it was updated.
- Write natural prose or a short list. Do not repeat the block headers.
- Blocks marked "attachment" come from a file attached to the email that is named in the block.
- Be concise."""


@dataclass
class Answer:
    text: str
    sources: list[Source]  # sources cited in the answer (all of them if none were cited)
    retrieved: list[Source]
    ttft_s: float
    generation_s: float
    tokens: int
    prompt_tokens: int


def _describe(s: Source) -> str:
    when = s.sent_at.strftime("%a %Y-%m-%d %H:%M UTC") if s.sent_at else "unknown date"
    kind = f"attachment {s.filename}" if s.source == "attachment" else "email"
    return f"[{s.n}] ({kind}) From: {s.sender} | Date: {when} | Subject: {s.subject}"


def build_context(sources: list[Source], budget: int = CONTEXT_CHAR_BUDGET) -> str:
    per_source = max(budget // max(len(sources), 1), 500)
    blocks = []
    for s in sources:
        text = "\n...\n".join(s.chunks)
        if len(text) > per_source:
            text = text[:per_source] + " [truncated]"
        blocks.append(f"{_describe(s)}\n{text}")
    return "\n\n---\n\n".join(blocks)


def build_messages(question: str, sources: list[Source]) -> list[dict]:
    user = (
        f"Today's date: {date.today().isoformat()}\n\n"
        f"Context:\n\n{build_context(sources)}\n\n---\n\nQuestion: {question}"
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def generate(
    question: str,
    retrieval: Retrieval,
    model: str | None = None,
    on_token: Callable[[str], None] | None = None,
) -> Answer:
    settings = get_settings()
    if not retrieval.sources:
        return Answer("I could not find anything relevant in your emails.", [], [], 0.0, 0.0, 0, 0)

    client = ollama.Client(host=settings.ollama_host)
    start = time.perf_counter()
    first_token_at = None
    parts: list[str] = []
    tokens = prompt_tokens = 0
    stream = client.chat(
        model=model or settings.llm_model,
        messages=build_messages(question, retrieval.sources),
        options={"temperature": 0.1, "num_ctx": NUM_CTX},
        stream=True,
    )
    for chunk in stream:
        piece = chunk["message"]["content"]
        if piece and first_token_at is None:
            first_token_at = time.perf_counter()
        parts.append(piece)
        if on_token and piece:
            on_token(piece)
        if chunk.get("done"):
            tokens = chunk.get("eval_count") or 0
            prompt_tokens = chunk.get("prompt_eval_count") or 0
    end = time.perf_counter()

    text = "".join(parts).strip()
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", text)}
    sources = [s for s in retrieval.sources if s.n in cited] or retrieval.sources
    return Answer(
        text, sources, retrieval.sources,
        ttft_s=(first_token_at or end) - start, generation_s=end - start,
        tokens=tokens, prompt_tokens=prompt_tokens,
    )
