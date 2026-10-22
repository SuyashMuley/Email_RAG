# Evaluation report

Measured on an Apple M4 laptop (24 GB RAM), Python 3.14, PostgreSQL 17 + pgvector 0.8, Ollama. All numbers come from
`scripts/benchmark.py`; full tables are in [benchmarks/RESULTS.md](benchmarks/RESULTS.md) and the raw answers in
`benchmarks/answers_*.md`. Rerun with `python scripts/benchmark.py all`.

## Summary

| Question | Result |
|---|---|
| Does retrieval find the right email? | Yes: the correct email is in the top 5 for 100% of the 21 answerable gold questions (top 1 for 81%). |
| Are answers correct and grounded? | Llama 3.1 8B passed 25 of 25 automated checks, including declining all 4 unanswerable questions. A manual read found one minor misstatement the checks missed. |
| Is it fast enough? | Retrieval takes about 20 ms. An answer starts after about 2.6 s and finishes in about 3.6 s (median), 12 s for the longest summaries. |
| Does user isolation hold? | Yes, at the API and the database. No cross-user read, write or token-forgery attempt succeeded in the 47 isolation and authentication tests. |

These results come from small, synthetic data. Read the caveats in [Limitations](#limitations) before generalizing.

## Method

- **Data:** two synthetic mailboxes (52 emails each) with threads on the example topics, PDF/DOCX attachments, an HTML-only email, and a
  private thread per user. For scale tests, 1,000 filler emails per user were added (2,027 chunks in total).
- **Gold set** (`data/eval/gold.json`): 25 questions: 14 for one user and 11 for the other. 21 have a known answer and 4 are unanswerable by design.
  Types: sender ("What did Sarah say…"), topic, "last heard from", summarize, attachment-only facts, specific facts, and unanswerable.
- **Retrieval scoring:** document-level. A hit is a returned email (or its attachment) matching the expected subject, sender and filename. hit@k, recall@k and MRR.
- **Answer scoring (automated):** every expected fact must appear, nothing forbidden may appear, citations must be valid, and unanswerable questions must be declined.
  This is a proxy. I also read every Llama answer by hand.

## Performance

| Metric | Value |
|---|---|
| Embedding time, ingestion | 1.8 ms per chunk (570 chunks/s); 2.1 ms per email; 2.1 s per 1,000 chunks |
| End-to-end ingestion (parse, chunk, embed, store) | 225 emails/s (2,024 emails in 9 s) |
| Query embedding | 14 ms p50 |
| Database search (hybrid) | 4 ms p50 / 5 ms p95 at 107 documents; 5 ms / 7 ms at 2,027 |
| Retrieval total | 19 ms p50 / 23 ms p95 (small); 20 ms / 25 ms (2,027 documents) |
| First token (Llama 3.1 8B Q4) | 2.6 s p50 (about 630-token prompt) |
| Answer generation (Llama 3.1 8B Q4) | 3.6 s p50, 11.9 s p95, 20 tokens/s |

Answer generation dominates: retrieval is under 1% of the time to an answer. First-token time is mostly prompt processing, so it grows with the number of sources. After idle periods the model reload adds a few seconds.

## Retrieval quality

Production configuration against ablations (2,027-document corpus; the small corpus is the same within one question):

| Configuration | hit@1 | hit@5 | recall@5 | MRR |
|---|---|---|---|---|
| Dense only (vectors) | 76% | 90% | 87% | 0.825 |
| Keyword only (full-text) | 86% | 95% | 95% | 0.897 |
| Hybrid (rank fusion) | 76% | 100% | 98% | 0.869 |
| Hybrid + query analysis (production) | 81% | 100% | 98% | 0.893 |

- Hybrid search is what makes the right email reliably appear: each method alone missed 1-2 questions that the combination found.
- Keyword search alone ranks the best match first slightly more often (86%), because email questions often share exact words with the answer. Fusion trades a little rank-1 precision for recall.
- Query analysis adds about 5 points of hit@1 and is what makes "last heard from…" exact.
- Adding 1,000 filler emails per user changed almost nothing, but the fillers come from only seven templates and are easy to ignore, so this mostly demonstrates stable latency, not robustness to hard distractors.
- Embedding models (dense only): bge-small 90% hit@5, MiniLM 95%, e5-small-v2 95%, bge-base 95%; MRR 0.83 to 0.90. With 21 questions these gaps are one question each and not significant.

## Answer quality

| Model | Pass | Answerable pass | Declined unanswerable | Cites sources | Median generation | Tokens/s |
|---|---|---|---|---|---|---|
| Llama 3.1 8B, Q4 | 100% | 100% | 4 of 4 | 100% | 3.6 s | 20 |
| Llama 3.1 8B, Q8 | 100% | 100% | 4 of 4 | 95% | 5.6 s | 11 |
| Phi-3 Mini | 88% | 95% | 2 of 4 | 62% | 3.6 s | 36 |

- **Q4 vs Q8:** identical quality on this task, 1.8x slower at Q8. Q4 is the default.
- **Phi-3 Mini** failed three questions. Two are real failures: asked where the Lisbon offsite is (not in that mailbox), it answered "Café Lumière" (a café-crawl newsletter); asked for the Atlas launch date (also not there), it gave another project's date.
  The third (a06) is a strict-matching artifact: it answered "Three beta customer quotes", which is correct but lacks the exact phrase "3 of 5". It also cites sources far less often, which weakens verifiability.
  Its higher tokens/s did not translate into faster answers: median time to first token and to completion were about the same as Llama Q4, and its prompt is longer in its own tokenizer (785 vs 631 tokens on average).
- **Manual review of Llama Q4:** answers were correct and cited. Two small problems the automated check missed, both in one answer (j01): it said cloud spend rose 12% "from previous years" (the email gives no baseline), and it
  described "cloud spend is unchanged" as an update to her earlier statement, which it was not. Treat the automated pass rate as an upper bound.

## Challenges and solutions

| Challenge | Solution |
|---|---|
| Full-text search matched nearly every email because of request words ("emails", "show", "say") | Removed generic request words from the keyword query; the right threads then ranked first. |
| The model quoted a superseded figure ($412,000) and ignored Sarah's later update ($427,000) | Present sources in chronological order, add an explicit "report the latest value" rule and a "cover every relevant block" rule. |
| "Sarah" appears in several roles; names are a weak embedding signal | Put sender, recipients and date in every chunk, and add a sender filter from names found in the mailbox (including mail addressed to that person). |
| Facts living only in attachments | Extract attachment text, index it as a document linked to the parent email, and link answers to that email. |
| Re-running ingestion duplicated or re-embedded mail; regenerated test data looked like new mail | Content hashes and stable ids make ingestion idempotent; the synthetic generator was made byte-deterministic. |
| Row-level security silently does nothing for superusers | Separate restricted role that cannot bypass it; a test confirms the role's flags; disabling the policy was shown to fail 5 tests. |
| Per-connection user setting lost after a rollback | Commit immediately after setting it. |
| The app role should not read the user table, yet must map a token's email to a user | `SECURITY DEFINER` lookup function. |
| Gmail raw messages use unpadded base64; a "partial" sync must not skip later mail | Pad before decoding; only a complete sync advances the history cursor. |
| Loading arbitrary embedding models executed remote code by default | `trust_remote_code` is off unless explicitly requested. |
| Small models invent answers when nothing matches | Negative gold questions measure this; Llama declines all, Phi-3 does not, which drove the model choice. |

## Limitations

- **Small, synthetic, self-authored gold set.** 25 questions (21 retrievable). One question is about 5 points, no confidence intervals, and I wrote both the data and the questions, so scores are optimistic. Embedding-model differences are within noise.
- **Easy distractors.** The filler emails are templated. Real mailboxes have far more near-duplicates and long threads.
- **Automated answer checks are a proxy** (see the Llama and Phi-3 notes above). Single run per question at temperature 0.1, so answers vary run to run: the product-launch summary was cited in the benchmark run but came back without any `[n]` citations in a later demo run, so the 100% citation rate is not guaranteed. The source list is still shown, but the sentence-level links are lost.
- **Gmail:** tested on fakes and one tiny real mailbox (2 messages, no attachments). Real attachments, large mailboxes, pagination and Google ID-token auth are not exercised against Google.
- **Large summaries are trimmed** to a context budget. There is no map-reduce, so "summarize everything about X" over dozens of emails can drop material.
- **No OCR**, so scanned PDFs and images are not indexed. No Google Drive ingestion.
- **Scale:** tested to about 2,000 documents. Filtered HNSW behavior, planner choices with row-level security, and ingestion at hundreds of thousands of emails were not tested.
- **Security scope:** row-level security guards against query bugs, not arbitrary SQL by a compromised process. The API serves plain HTTP on localhost with no rate limiting. Development secrets are in the defaults.
- **Hardware-specific numbers:** speeds are for an M4 with 24 GB; other machines will differ, especially for token rates.

## Improvements and production considerations

- **Retrieval:** a cross-encoder reranker on the top results (to lift hit@1), tuned fusion weights, date-expression parsing ("last week"), thread-level context, and query rewriting for follow-up questions.
- **Long answers:** map-reduce or hierarchical summaries; verify each cited claim against its source and flag unsupported sentences.
- **Evaluation:** a larger gold set built from real (consenting) mail, human-graded answers, multiple runs with confidence intervals, and harder distractors.
- **Ingestion:** a background worker with a queue, Gmail push notifications instead of polling, OCR for scans, and Google Drive.
- **Scale:** pgvector iterative scans, partitioning or per-tenant indexes, connection pooling that sets the user per transaction, and monitoring of query plans under row-level security.
- **Security:** a database login or signed claims per user, TLS and rate limiting on the API, a secrets manager instead of `.env`, encrypted storage for embeddings and token files (embeddings can leak content), audit logging, and a retention/deletion path for a user's data.
- **Serving:** keep the model warm, cap concurrent generations (one local model serves one request well), and stream tokens to clients.
