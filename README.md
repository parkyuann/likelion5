# DiffuRank instance 1 deployment

This is a deliberately minimal deployment branch for the full-L4 DiffuRank
node (instance 1). It contains no frontend, application backend, KOSIS
crawler, PostgreSQL, OpenSearch, Qdrant, or Redis source.

The only tracked runtime source is in
[`deploy/diffurank_service`](deploy/diffurank_service): the container recipe,
candidate-ordering service, fixed-source downloader, and GPU preflight.

The following are **server-only artifacts**, not Git content:

- the LLaDA base snapshot;
- the final LoRA adapter;
- receipts produced on the server;
- the internal token and `runtime.env` configuration.

The service is intentionally candidate-ordering only. It does not connect to
the data stores and cannot select a final table/cell or issue a verdict.
Instance 2 communicates with it only through its private `8203` HTTP endpoint,
release ID, candidate-scope SHA-256, and the server-only internal token.

## Provenance

- Minimal branch materialized from `feat/diffurank-shadow-service` commit
  `fdb37e1a6fdc14678ae2959eebca3f86ed05eceb`.
- Running service-code image source commit:
  `dd2958c56dcecbd39249523577eee2a934b6d01b`.
- See `deploy/diffurank_service/README.md` for model, adapter, and launch
  details.
