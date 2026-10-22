"""HTTP API. The caller's identity comes only from a verified bearer token.

There is deliberately no user_id in any request: bodies reject unknown fields, and the database
connection used for each request is scoped (via row-level security) to the token's user.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from rag.auth.tokens import AuthError, make_verifier
from rag.config import Settings, get_settings
from rag.db.connection import connect_app, lookup_user_id
from rag.embed.embedder import get_embedder
from rag.generate.llm import generate
from rag.retrieve.retriever import retrieve

log = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthedUser:
    id: int
    email: str


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a smuggled user_id is a 422, not silently ignored
    question: str = Field(min_length=1, max_length=2000)
    k: int | None = Field(None, ge=1, le=20, description="source documents to retrieve")


class SourceOut(BaseModel):
    n: int
    source: str
    subject: str | None
    sender: str | None
    sent_at: datetime | None
    filename: str | None
    link: str | None


class QueryResponse(BaseModel):
    answer: str
    sources: list[SourceOut]
    intent: str
    timings: dict[str, float]


class MeResponse(BaseModel):
    email: str
    documents: int


def create_app(settings: Settings | None = None, generate_fn=generate, embedder_fn=get_embedder) -> FastAPI:
    settings = settings or get_settings()
    verify = make_verifier(settings)  # raises ConfigError at startup if auth is not safely configured
    app = FastAPI(title="Email RAG")

    def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> AuthedUser:
        unauthorized = HTTPException(401, "Invalid or missing token", headers={"WWW-Authenticate": "Bearer"})
        if creds is None:
            raise unauthorized
        try:
            email = verify(creds.credentials)
        except AuthError as exc:
            log.info("rejected token: %s", exc)
            raise unauthorized
        user_id = lookup_user_id(email)
        if user_id is None:
            raise HTTPException(403, "No mailbox is registered for this account")
        return AuthedUser(user_id, email)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/me", response_model=MeResponse)
    def me(user: AuthedUser = Depends(current_user)) -> MeResponse:
        with connect_app(user.id) as conn:
            count = conn.execute("SELECT count(*) FROM documents").fetchone()[0]  # RLS-scoped
        return MeResponse(email=user.email, documents=count)

    @app.post("/query", response_model=QueryResponse)
    def query(req: QueryRequest, user: AuthedUser = Depends(current_user)) -> QueryResponse:
        embedder = embedder_fn()
        with connect_app(user.id) as conn:  # released before the slow LLM call
            retrieval = retrieve(conn, embedder, user.id, req.question, own_email=user.email, k_docs=req.k)
        answer = generate_fn(req.question, retrieval)
        return QueryResponse(
            answer=answer.text,
            sources=[
                SourceOut(
                    n=s.n, source=s.source, subject=s.subject, sender=s.sender, sent_at=s.sent_at,
                    filename=s.filename, link=s.web_link,
                )
                for s in answer.sources
            ],
            intent=retrieval.intent,
            timings={
                "embed_ms": round(retrieval.embed_ms, 1),
                "retrieve_ms": round(retrieval.search_ms, 1),
                "first_token_s": round(answer.ttft_s, 2),
                "generate_s": round(answer.generation_s, 2),
                "tokens": float(answer.tokens),
            },
        )

    return app
