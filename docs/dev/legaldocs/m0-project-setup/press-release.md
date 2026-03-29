# M0: Project Setup - PR FAQ

> **Milestone**: M0
> **Created**: 2026-03-29
> **Status**: Draft

---

## Press Release

### Privacy-Preserving Legal Document Pipeline -- Infrastructure Foundation

**For** developers building privacy-preserving document processing systems
**Who** need a local development environment that mirrors a production AWS Nitro Enclave architecture
**The** M0 Project Setup
**Is a** foundational infrastructure milestone
**That** gives developers a single-command local environment with all services running, database schema ready, and secure communication channels configured
**Unlike** manually provisioning each service, configuring networks, and writing schema by hand
**Our solution** provides a complete Docker Compose stack with isolated enclave networking, PostgreSQL with migrations, Redis, and mTLS certificate generation -- all standing up in one command

---

Today we are announcing the foundation of the LegalDocs privacy-preserving document processing pipeline. Developers can now run `docker compose up` and have a fully functional local environment that mirrors a production AWS Nitro Enclave architecture. The stack includes a FastAPI parent service, a network-isolated enclave container, PostgreSQL with pre-applied migrations for document and extracted field tracking, and Redis for task queuing. The enclave container runs on an internal-only Docker network with no external access, simulating the hardware isolation guarantees of a real Nitro Enclave.

Setting up secure communication between the parent and enclave is handled automatically. A certificate generation script produces mTLS certificates so that the parent and enclave containers authenticate each other over an encrypted channel. Health check endpoints confirm that every service is reachable and correctly configured. Developers no longer need to understand the full complexity of enclave networking and mutual TLS to get started -- they run one command and begin building on top of a correctly isolated architecture.

## FAQ

### Customer Experience

Q: How does a developer get started with this environment?
A: Run `docker compose up` from the project root. All services (API, Redis, PostgreSQL, enclave) start automatically. Before first run, execute the mTLS certificate generation script to create the certificates needed for parent-enclave communication.

Q: What does the happy path look like step-by-step?
A: (1) Clone the repo. (2) Run `scripts/generate_certs.sh` to create mTLS certificates. (3) Run `docker compose up`. (4) Verify all services are healthy via their health check endpoints. The database schema is applied automatically via migrations.

Q: How do I know all services are running correctly?
A: Each service exposes a health check endpoint. The API, Redis, PostgreSQL, and enclave container all report their status. Docker Compose health checks ensure dependent services wait for their dependencies to be ready.

Q: What happens if a service fails to start?
A: Docker Compose will report the failing service in its output. Health checks will show which dependency is unavailable. Common issues (missing certs, port conflicts) are surfaced clearly in logs.

### Scope & Non-Goals

Q: What is explicitly NOT included in M0?
A: No encryption/decryption logic, no document processing pipeline, no client SDK, no tenant isolation, no attestation flow. M0 is strictly infrastructure: services running, schema created, certificates generated, networks configured.

Q: What is the minimal version that still delivers the core benefit?
A: Docker Compose with all four services starting successfully, the database schema applied, and mTLS certificates generated. Health checks are the validation layer.

Q: What are we deferring to future milestones?
A: Enclave key pair generation and attestation (M1), document ingestion and processing pipeline (M2), multi-tenant scoping (M3), and end-to-end verification and demo scripts (M4).

### Technical Constraint FAQs

Q: What existing system capabilities does this depend on?
A: Docker and Docker Compose installed locally. No cloud services or external dependencies required.

Q: What new capabilities need to be built?
A: Docker Compose configuration with two networks (external-net and enclave-net), Dockerfiles for parent and enclave services, PostgreSQL migration scripts for the documents and extracted_fields tables, an mTLS certificate generation script, and health check endpoints for all services.

Q: Are there performance, scale, or data constraints?
A: No. This is a local development MVP. Performance and scale are explicit non-goals.

Q: How does the enclave container achieve network isolation?
A: The enclave container is attached only to an internal Docker network (enclave-net) with no external connectivity. The parent container bridges both the external-net and enclave-net, acting as the sole communication channel to the enclave, mirroring the vsock pattern in AWS Nitro Enclaves.

### Edge Cases & Error States FAQs

Q: What happens if the mTLS certificates have not been generated before starting services?
A: The parent and enclave containers will fail to establish their mTLS connection. Health checks will report the enclave as unreachable. The developer must run the certificate generation script first.

Q: What happens if PostgreSQL is not ready when the API starts?
A: Docker Compose health checks and dependency ordering ensure the API waits for PostgreSQL to be ready before starting. If it still fails, the API will exit with a clear connection error.

Q: What happens if ports are already in use?
A: Docker Compose will report a port binding conflict. The developer can either stop the conflicting service or adjust the port mapping in the compose file.

### Business & Operational FAQs

Q: How will we know if M0 is successful?
A: All services start via `docker compose up`, health checks pass for every service, the database schema is applied correctly, and the enclave container has no external network access.

Q: What could go wrong post-setup?
A: Certificate expiration (not a concern for local dev with long-lived self-signed certs), Docker version incompatibilities, or schema drift if migrations are modified without re-applying.

Q: Are there compliance or security considerations?
A: The mTLS certificates are self-signed and intended for local development only. The enclave network isolation is enforced by Docker networking, not hardware -- this is a simulation, not a production security boundary.

## Implementation Scope

### Current State -> End State

| Current State | End State |
| --- | --- |
| No project structure exists | Complete repo structure with parent/, enclave/, client/, scripts/, alembic/ directories |
| No local development environment | `docker compose up` starts API, Redis, PostgreSQL, and enclave containers |
| No database schema | PostgreSQL has documents and extracted_fields tables via Alembic migrations |
| No secure communication channel | mTLS certificates generated via script; parent and enclave communicate over mutual TLS |
| No network isolation for enclave | Enclave container on internal-only Docker network with no external access |
| No way to verify services are running | Health check endpoints on all services confirm readiness |

### User Stories

- [ ] As a developer, I can run `docker compose up` and have all services start (API, Redis, PostgreSQL, enclave) so that I have a working local environment
- [ ] As a developer, I can run DB migrations to create the documents and extracted_fields tables so that the schema is ready for document processing
- [ ] As a developer, I can run a script to generate mTLS certificates so that parent-enclave communication is secured
- [ ] As a developer, I can hit health check endpoints on each service so that I can verify the environment is correctly configured
- [ ] As a developer, I can confirm the enclave container has no external network access so that I know the isolation model is working

### Success Criteria

- [ ] `docker compose up` starts all four services without errors
- [ ] PostgreSQL contains documents and extracted_fields tables after migrations run
- [ ] mTLS certificate generation script produces valid CA, server, and client certificates
- [ ] Health check endpoints return successful responses for API, PostgreSQL, Redis, and enclave
- [ ] Enclave container cannot reach external networks (e.g., `curl google.com` fails from inside the enclave)
- [ ] Parent container can reach the enclave over the internal network via mTLS

### Known Compromises

- [ ] Self-signed certificates instead of a proper CA -- acceptable for local development simulation
- [ ] Docker network isolation instead of hardware enclave isolation -- this is the core simulation trade-off of the entire project
- [ ] PostgreSQL stores everything (including what would be S3 blobs in production) -- simplifies the MVP without losing the privacy model
- [ ] No real health check logic beyond basic connectivity -- sufficient to validate services are running

### Open Questions / Needs Research

- [ ] What specific columns and types should the documents and extracted_fields tables have? (Need to check if PROJECT_PLAN.md specifies this or if it needs to be designed)
- [ ] Should Alembic migrations run automatically on container startup or require a manual step?
- [ ] What format should the health check responses use (simple 200 OK vs structured JSON)?

---

## Next Steps

When ready to implement:

1. Run `/pipe-research --project legaldocs --ref m0 project setup` to analyze codebase
2. Run `/pipe-plan-implementation --project legaldocs --ref m0 project setup` to create implementation plan
3. Run `/pipe-implement docs/dev/legaldocs/m0-project-setup/implementation-plan.md` to execute the plan
