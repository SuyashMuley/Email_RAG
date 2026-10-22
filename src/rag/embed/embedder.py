"""Sentence-transformers embedding wrapper with model-specific query/document prefixes."""

import threading
from functools import lru_cache

import numpy as np

from rag.config import get_settings

# (query prefix, document prefix) by model family
_PREFIXES = {
    "bge": ("Represent this sentence for searching relevant passages: ", ""),
    "nomic": ("search_query: ", "search_document: "),
    "e5-": ("query: ", "passage: "),
}


class Embedder:
    def __init__(self, model_name: str | None = None, trust_remote_code: bool = False):
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name or get_settings().embed_model
        self._lock = threading.Lock()  # the API serves requests from a thread pool
        self.model = SentenceTransformer(self.model_name, trust_remote_code=trust_remote_code)
        get_dim = getattr(self.model, "get_embedding_dimension", None) or self.model.get_sentence_embedding_dimension
        self.dim = get_dim()
        self.query_prefix, self.doc_prefix = next(
            (p for family, p in _PREFIXES.items() if family in self.model_name.lower()), ("", "")
        )

    def embed_documents(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        with self._lock:
            return self.model.encode(
                [self.doc_prefix + t for t in texts],
                batch_size=batch_size, normalize_embeddings=True, show_progress_bar=False,
            )

    def embed_query(self, text: str) -> np.ndarray:
        with self._lock:
            return self.model.encode(self.query_prefix + text, normalize_embeddings=True)


@lru_cache
def get_embedder() -> Embedder:
    return Embedder()
