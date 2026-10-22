"""Benchmarks for the email RAG system. Results go to docs/benchmarks/.

  python scripts/benchmark.py embeddings   # embedding models: speed and dense-retrieval quality
  python scripts/benchmark.py retrieval    # hybrid search ablation, latency and ingest throughput
  python scripts/benchmark.py generation   # LLM comparison on the gold questions
  python scripts/benchmark.py report       # rebuild RESULTS.md from results.json
  python scripts/benchmark.py all

Needs the pgvector container (retrieval, generation) and Ollama (generation) to be running.
The gold questions live in data/eval/gold.json; scoring helpers are in rag/evaluation.py.
"""

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from rag import evaluation as ev
from rag.db import repo
from rag.db.connection import connect, connect_app, lookup_user_id
from rag.embed.embedder import Embedder, get_embedder
from rag.ingest.mbox_loader import iter_emails
from rag.ingest.pipeline import IngestStats, _prepare, ingest_emails
from rag.retrieve.retriever import retrieve

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "benchmarks"
RESULTS = OUT_DIR / "results.json"
DEFAULT_EMBED_MODELS = [
    "BAAI/bge-small-en-v1.5",
    "sentence-transformers/all-MiniLM-L6-v2",
    "intfloat/e5-small-v2",
    "BAAI/bge-base-en-v1.5",
]
DEFAULT_LLMS = ["llama3.1:8b", "llama3.1:8b-instruct-q8_0", "phi3:mini"]
KS = (1, 3, 5)


# --- shared helpers ---------------------------------------------------------------------------


def load_gold() -> dict:
    return json.loads((ROOT / "data" / "eval" / "gold.json").read_text())


def scaled_mailboxes(noise: int, workdir: Path) -> dict[str, Path]:
    """Synthetic mailboxes with `noise` extra filler emails each (generated once, reused)."""
    marker = workdir / f"noise-{noise}.done"
    if not marker.exists():
        workdir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "generate_synthetic.py"), "--noise", str(noise), "--out", str(workdir)],
            check=True, capture_output=True,
        )
        marker.write_text("ok")
    return {"alex.rivera@example.com": workdir / "alex_rivera.mbox", "jordan.lee@example.com": workdir / "jordan_lee.mbox"}


def retrieval_row(sources, specs) -> dict:
    rank = ev.first_rank(sources, specs)
    row = {f"hit@{k}": bool(rank and rank <= k) for k in KS}
    row |= {"recall@5": ev.recall_at_k(sources, specs, 5), "rr": 1 / rank if rank else 0.0, "rank": rank}
    return row


def aggregate(rows: list[dict]) -> dict:
    if not rows:
        return {}
    out = {f"hit@{k}": statistics.fmean(r[f"hit@{k}"] for r in rows) for k in KS}
    out["recall@5"] = statistics.fmean(r["recall@5"] for r in rows)
    out["mrr"] = statistics.fmean(r["rr"] for r in rows)
    out["n"] = len(rows)
    return out


def by_category(rows: list[dict]) -> dict:
    cats = sorted({r["category"] for r in rows})
    return {c: aggregate([r for r in rows if r["category"] == c]) for c in cats}


def positive_questions(gold: dict):
    for user, spec in gold.items():
        for q in spec["questions"]:
            if q["relevant"]:
                yield user, q


def system_info() -> dict:
    chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    mem = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip()
    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "platform": platform.platform(), "chip": chip or platform.processor(),
        "ram_gb": round(int(mem) / 2**30) if mem.isdigit() else None, "python": platform.python_version(),
    }


def load_results() -> dict:
    return json.loads(RESULTS.read_text()) if RESULTS.exists() else {}


def save_section(name: str, data: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results = load_results()
    results["system"] = system_info()
    results[name] = data
    RESULTS.write_text(json.dumps(results, indent=2, default=str))
    write_report(results)


# --- embeddings -------------------------------------------------------------------------------


class DocScore:
    def __init__(self, fields: dict, score: float):
        self.subject, self.sender, self.filename, self.score = fields["subject"], fields["sender"], fields.get("filename"), score


def build_corpus(mbox: Path) -> list[tuple[dict, str]]:
    """Every chunk text that ingestion would embed, paired with its document's metadata."""
    items = []
    for email in iter_emails(mbox):
        _, _, parent = _prepare(email, IngestStats())
        for doc in (parent, *parent.attachments):
            items += [(doc.fields, text) for text in doc.texts]
    return items


def run_embeddings(args) -> None:
    gold = load_gold()
    corpora = {u: build_corpus(p) for u, p in scaled_mailboxes(args.noise, ROOT / "data" / "bench").items()}
    n_chunks = sum(len(c) for c in corpora.values())
    print(f"corpus: {n_chunks} chunks across {len(corpora)} mailboxes ({args.noise} filler emails each)")
    results = {"noise_per_mailbox": args.noise, "chunks": n_chunks, "models": {}}

    for name in args.models:
        print(f"\n== {name}")
        try:
            embedder = Embedder(name)
        except Exception as exc:
            print(f"  skipped: {type(exc).__name__}: {exc}")
            results["models"][name] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        embedder.embed_documents(["warm up"] * 4)
        rows, query_ms, embed_s = [], [], 0.0
        for user, corpus in corpora.items():
            texts = [t for _, t in corpus]
            t0 = time.perf_counter()
            matrix = embedder.embed_documents(texts)
            embed_s += time.perf_counter() - t0
            for u, q in positive_questions(gold):
                if u != user:
                    continue
                t0 = time.perf_counter()
                qvec = embedder.embed_query(q["q"])
                query_ms.append((time.perf_counter() - t0) * 1000)
                sims = matrix @ qvec
                best: dict[int, tuple[float, dict]] = {}
                for (fields, _), sim in zip(corpus, sims):
                    cur = best.get(id(fields))
                    if cur is None or sim > cur[0]:
                        best[id(fields)] = (float(sim), fields)
                docs = [DocScore(f, s) for s, f in best.values()]
                rows.append({**retrieval_row(docs, q["relevant"]), "category": q["category"]})
        agg = aggregate(rows)
        results["models"][name] = {
            "dim": embedder.dim,
            "chunks_per_s": n_chunks / embed_s,
            "ms_per_chunk": embed_s / n_chunks * 1000,
            "query_ms_p50": ev.percentile(query_ms, 50),
            "dense_only": agg,
        }
        print(f"  dim {embedder.dim}, {n_chunks / embed_s:.0f} chunks/s, query p50 {ev.percentile(query_ms, 50):.1f} ms, "
              f"hit@1 {agg['hit@1']:.2f} hit@5 {agg['hit@5']:.2f} MRR {agg['mrr']:.3f}")
    save_section("embeddings", results)


# --- retrieval --------------------------------------------------------------------------------

CONFIGS = {
    "dense only": {"mode": "dense", "analysis": False},
    "keyword only (full-text)": {"mode": "fts", "analysis": False},
    "hybrid (RRF)": {"mode": "hybrid", "analysis": False},
    "hybrid + query analysis (production)": {"mode": "hybrid", "analysis": True},
}
PRODUCTION = "hybrid + query analysis (production)"


def run_retrieval(args) -> None:
    gold = load_gold()
    embedder = get_embedder()
    mailboxes = scaled_mailboxes(args.noise, ROOT / "data" / "bench")
    results: dict = {"embed_model": embedder.model_name, "noise_per_mailbox": args.noise}

    # Load the scaled mailboxes into throwaway users, through the restricted role like the real app.
    bench_ids, ingest = {}, {"emails": 0, "chunks": 0, "embed_s": 0.0, "total_s": 0.0}
    with connect() as admin:
        for user in gold:
            email = f"bench-{user}"
            admin.execute("DELETE FROM users WHERE email = %s", (email,))
            bench_ids[user] = repo.get_or_create_user(admin, email)
    try:
        for user, uid in bench_ids.items():
            with connect_app(uid) as conn:
                s = ingest_emails(conn, uid, iter_emails(mailboxes[user]), embedder)
            ingest["emails"] += s.emails_ingested
            ingest["chunks"] += s.chunks
            ingest["embed_s"] += s.embed_seconds
            ingest["total_s"] += s.total_seconds
        ingest["emails_per_s"] = ingest["emails"] / ingest["total_s"]
        ingest["embed_ms_per_email"] = ingest["embed_s"] / ingest["emails"] * 1000
        ingest["embed_ms_per_1k_chunks"] = ingest["embed_s"] / ingest["chunks"] * 1e6
        results["ingest_scaled"] = ingest
        print(f"ingested {ingest['emails']} emails / {ingest['chunks']} chunks in {ingest['total_s']:.1f}s "
              f"({ingest['emails_per_s']:.0f} emails/s, embedding {ingest['embed_ms_per_email']:.1f} ms/email)")

        corpora = {"small": {u: (lookup_user_id(u), u) for u in gold},
                   "scaled": {u: (bench_ids[u], f"bench-{u}") for u in gold}}
        results["corpora"] = {}
        for corpus, users in corpora.items():
            with connect() as admin:
                docs = sum(admin.execute("SELECT count(*) FROM documents WHERE user_id = %s", (uid,)).fetchone()[0]
                           for uid, _ in users.values())
            results["corpora"][corpus] = {"documents": docs}
            results[corpus] = {}
            latency = {"embed_ms": [], "search_ms": [], "total_ms": []}
            for config, kwargs in CONFIGS.items():
                rows = []
                for user, q in positive_questions(gold):
                    uid, own = users[user]
                    with connect_app(uid) as conn:
                        r = retrieve(conn, embedder, uid, q["q"], own_email=own, **kwargs)
                        rows.append({**retrieval_row(r.sources, q["relevant"]), "category": q["category"], "id": q["id"]})
                        if config == PRODUCTION:
                            for i in range(args.repeat + 1):
                                t0 = time.perf_counter()
                                r = retrieve(conn, embedder, uid, q["q"], own_email=own, **kwargs)
                                if i:  # first pass is warm-up
                                    latency["total_ms"].append((time.perf_counter() - t0) * 1000)
                                    latency["embed_ms"].append(r.embed_ms)
                                    latency["search_ms"].append(r.search_ms)
                results[corpus][config] = {"overall": aggregate(rows), "by_category": by_category(rows),
                                           "misses": [r["id"] for r in rows if not r["hit@5"]]}
                a = results[corpus][config]["overall"]
                print(f"[{corpus:6}] {config:40} hit@1 {a['hit@1']:.2f} hit@5 {a['hit@5']:.2f} "
                      f"recall@5 {a['recall@5']:.2f} MRR {a['mrr']:.3f}  misses: {results[corpus][config]['misses']}")
            results[corpus]["latency"] = {k: ev.summarize(v) for k, v in latency.items()}
            lt = results[corpus]["latency"]["total_ms"]
            print(f"[{corpus:6}] retrieval latency p50 {lt['p50']:.0f} ms, p95 {lt['p95']:.0f} ms ({lt['n']} calls)")
    finally:
        if not args.keep:
            with connect() as admin:
                for user in gold:
                    admin.execute("DELETE FROM users WHERE email = %s", (f"bench-{user}",))
    save_section("retrieval", results)


# --- generation -------------------------------------------------------------------------------


def run_generation(args) -> None:
    import ollama

    from rag.config import get_settings
    from rag.generate.llm import generate

    gold = load_gold()
    embedder = get_embedder()
    client = ollama.Client(host=get_settings().ollama_host)

    items = []
    for user, spec in gold.items():
        uid = lookup_user_id(user)
        for q in spec["questions"]:
            with connect_app(uid) as conn:
                retrieval = retrieve(conn, embedder, uid, q["q"], own_email=user)
            items.append((user, q, retrieval))

    results = {"questions": len(items), "models": {}}
    for model in args.llm:
        print(f"\n== {model}")
        try:
            client.chat(model=model, messages=[{"role": "user", "content": "hi"}], options={"num_predict": 1})
        except Exception as exc:
            print(f"  skipped: {type(exc).__name__}: {exc}")
            results["models"][model] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        records = []
        for user, q, retrieval in items:
            ans = generate(q["q"], retrieval, model=model)
            score = ev.score_answer(
                ans.text, len(retrieval.sources), q.get("facts", []), q.get("forbidden", []), q.get("abstain", False)
            )
            rank = ev.first_rank(retrieval.sources, q["relevant"]) if q["relevant"] else None
            gen_only = max(ans.generation_s - ans.ttft_s, 1e-9)
            records.append({
                "id": q["id"], "category": q["category"], "question": q["q"], "answer": ans.text,
                "ttft_s": ans.ttft_s, "generation_s": ans.generation_s, "tokens": ans.tokens,
                "tokens_per_s": ans.tokens / gen_only, "prompt_tokens": ans.prompt_tokens,
                "retrieved_ok": (rank is not None and rank <= 5) if q["relevant"] else None,
                "passed": ev.answer_passes(score), **score,
            })
            print(f"  {q['id']} {'PASS' if records[-1]['passed'] else 'FAIL'} {ans.generation_s:5.1f}s  {q['q'][:60]}")
        results["models"][model] = summarize_generation(records)
        write_answers(model, records)
    save_section("generation", results)


def summarize_generation(records: list[dict]) -> dict:
    cats = sorted({r["category"] for r in records})
    answerable = [r for r in records if r["category"] != "negative"]
    negatives = [r for r in records if r["category"] == "negative"]
    return {
        "pass_rate": statistics.fmean(r["passed"] for r in records),
        "answerable_pass_rate": statistics.fmean(r["passed"] for r in answerable),
        "fact_recall": sum(r["facts_found"] for r in answerable) / max(sum(r["facts_total"] for r in answerable), 1),
        "abstain_correct": statistics.fmean(r["abstained"] for r in negatives) if negatives else None,
        "hallucinated_forbidden": sum(bool(r["forbidden_hit"]) for r in records),
        "invalid_citations": sum(bool(r["invalid_citations"]) for r in records),
        "cites_sources": statistics.fmean(r["cited"] for r in answerable),
        "ttft_s": ev.summarize([r["ttft_s"] for r in records]),
        "generation_s": ev.summarize([r["generation_s"] for r in records]),
        "tokens_per_s": statistics.fmean(r["tokens_per_s"] for r in records if r["tokens"] > 5),
        "prompt_tokens_mean": statistics.fmean(r["prompt_tokens"] for r in records),
        "by_category": {c: statistics.fmean(r["passed"] for r in records if r["category"] == c) for c in cats},
        "failed": [r["id"] for r in records if not r["passed"]],
    }


def write_answers(model: str, records: list[dict]) -> None:
    """Full answers for manual review, since the automated check is only a proxy."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    safe = model.replace(":", "_").replace("/", "_")
    lines = [f"# Answers from `{model}`\n", "Review these by hand: the PASS/FAIL flag is an automated keyword check.\n"]
    for r in records:
        lines.append(f"## {r['id']} [{r['category']}] {'PASS' if r['passed'] else 'FAIL'}: {r['question']}\n")
        lines.append(f"{r['answer']}\n")
        lines.append(f"_facts {r['facts_found']}/{r['facts_total']}, abstained={r['abstained']}, "
                     f"forbidden={r['forbidden_hit']}, {r['generation_s']:.1f}s, {r['tokens_per_s']:.1f} tok/s_\n")
    (OUT_DIR / f"answers_{safe}.md").write_text("\n".join(lines))


# --- report -----------------------------------------------------------------------------------


def pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def write_report(results: dict) -> None:
    sysinfo = results.get("system", {})
    out = ["# Benchmark results\n",
           f"Run {sysinfo.get('date')} on {sysinfo.get('chip')}, {sysinfo.get('ram_gb')} GB RAM, Python {sysinfo.get('python')}. "
           "Generated by `scripts/benchmark.py`; do not edit by hand.\n"]

    if emb := results.get("embeddings"):
        out += ["## Embedding models (dense retrieval only)\n",
                f"Corpus: {emb['chunks']} chunks ({emb['noise_per_mailbox']} filler emails per mailbox plus the real threads). "
                "Quality is document-level hit@k and MRR over the gold questions that have a known answer.\n",
                "| Model | Dim | Chunks/s | ms/chunk | Query p50 (ms) | hit@1 | hit@3 | hit@5 | MRR |", "|---|---|---|---|---|---|---|---|---|"]
        for name, m in emb["models"].items():
            if "error" in m:
                out.append(f"| {name} | skipped: {m['error']} | | | | | | | |")
                continue
            d = m["dense_only"]
            out.append(f"| {name} | {m['dim']} | {m['chunks_per_s']:.0f} | {m['ms_per_chunk']:.1f} | {m['query_ms_p50']:.1f} | "
                       f"{pct(d['hit@1'])} | {pct(d['hit@3'])} | {pct(d['hit@5'])} | {d['mrr']:.3f} |")
        out.append("")

    if ret := results.get("retrieval"):
        out += ["## Retrieval (pgvector, full pipeline)\n", f"Embedding model: `{ret['embed_model']}`.\n"]
        for corpus in ("small", "scaled"):
            if corpus not in ret:
                continue
            out += [f"### {corpus.capitalize()} corpus ({ret['corpora'][corpus]['documents']} documents across both users)\n",
                    "| Configuration | hit@1 | hit@3 | hit@5 | recall@5 | MRR | Missed (not in top 5) |", "|---|---|---|---|---|---|---|"]
            for config in CONFIGS:
                a = ret[corpus][config]
                o = a["overall"]
                out.append(f"| {config} | {pct(o['hit@1'])} | {pct(o['hit@3'])} | {pct(o['hit@5'])} | {pct(o['recall@5'])} | "
                           f"{o['mrr']:.3f} | {', '.join(a['misses']) or 'none'} |")
            cats = ret[corpus][PRODUCTION]["by_category"]
            out += ["", "Production configuration by question type (hit@5):", "",
                    "| " + " | ".join(cats) + " |", "|" + "---|" * len(cats),
                    "| " + " | ".join(f"{pct(v['hit@5'])} (n={v['n']})" for v in cats.values()) + " |", ""]
            lat = ret[corpus]["latency"]
            out += ["Retrieval latency of the production configuration:", "", "| Stage | p50 (ms) | p95 (ms) | max (ms) |", "|---|---|---|---|"]
            for key, label in (("embed_ms", "query embedding"), ("search_ms", "database search"), ("total_ms", "total")):
                out.append(f"| {label} | {lat[key]['p50']:.1f} | {lat[key]['p95']:.1f} | {lat[key]['max']:.1f} |")
            out.append("")
        if i := ret.get("ingest_scaled"):
            out += ["### Ingestion throughput (scaled corpus, end to end)\n",
                    f"{i['emails']} emails, {i['chunks']} chunks in {i['total_s']:.1f} s: {i['emails_per_s']:.0f} emails/s; "
                    f"embedding {i['embed_ms_per_email']:.1f} ms per email, {i['embed_ms_per_1k_chunks'] / 1000:.2f} s per 1,000 chunks.\n"]

    if gen := results.get("generation"):
        out += ["## Answer generation\n",
                f"{gen['questions']} gold questions (retrieval is identical for every model). 'Pass' means every expected fact "
                "appears, nothing forbidden appears, citations are valid, and unanswerable questions are declined. "
                "This is an automated proxy: read `answers_*.md` for the full text.\n",
                "| Model | Pass | Answerable pass | Fact recall | Declined unanswerable | Forbidden/hallucinated | First token p50 (s) | Generation p50 (s) | p95 (s) | Tokens/s |",
                "|---|---|---|---|---|---|---|---|---|---|"]
        for name, m in gen["models"].items():
            if "error" in m:
                out.append(f"| {name} | skipped: {m['error']} | | | | | | | | |")
                continue
            out.append(f"| {name} | {pct(m['pass_rate'])} | {pct(m['answerable_pass_rate'])} | {pct(m['fact_recall'])} | "
                       f"{pct(m['abstain_correct'])} | {m['hallucinated_forbidden']} | {m['ttft_s']['p50']:.1f} | "
                       f"{m['generation_s']['p50']:.1f} | {m['generation_s']['p95']:.1f} | {m['tokens_per_s']:.1f} |")
        out += ["", "Failed questions per model:", ""]
        for name, m in gen["models"].items():
            if "failed" in m:
                out.append(f"- `{name}`: {', '.join(m['failed']) or 'none'}")
        out.append("")

    (OUT_DIR / "RESULTS.md").write_text("\n".join(out))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("embeddings", "retrieval", "generation", "report", "all"):
        p = sub.add_parser(name)
        p.add_argument("--noise", type=int, default=1000, help="filler emails per mailbox for the scaled corpus")
        p.add_argument("--models", nargs="+", default=DEFAULT_EMBED_MODELS, help="embedding models to compare")
        p.add_argument("--llm", nargs="+", default=DEFAULT_LLMS, help="Ollama models to compare")
        p.add_argument("--repeat", type=int, default=5, help="latency repetitions per question")
        p.add_argument("--keep", action="store_true", help="keep the throwaway benchmark users")
    args = parser.parse_args()

    if args.cmd in ("embeddings", "all"):
        run_embeddings(args)
    if args.cmd in ("retrieval", "all"):
        run_retrieval(args)
    if args.cmd in ("generation", "all"):
        run_generation(args)
    if args.cmd == "report":
        write_report(load_results())
    print(f"\nresults: {RESULTS.relative_to(ROOT)}, report: {(OUT_DIR / 'RESULTS.md').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
