# Retrieval benchmark

Generated: 2026-10-19T18:10:58Z

Corpus: 18 verified documents; top-k=5; embedding=hash/feature-hash-1536.

> Caveat: the default `hash` provider is lexical feature hashing, not a neural semantic model. These numbers validate retrieval plumbing and latency; re-run with OpenAI/Ollama before making model-quality claims.

| Mode | Recall@k | MRR | Mean latency (ms) | P95 latency (ms) |
|---|---:|---:|---:|---:|
| semantic | 1.000 | 1.000 | 166.0 | 192.8 |
| keyword | 1.000 | 1.000 | 155.6 | 176.7 |
| hybrid | 1.000 | 1.000 | 161.1 | 209.5 |
| auto | 1.000 | 1.000 | 171.7 | 213.5 |
