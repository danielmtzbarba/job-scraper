# Google Cloud database options for the job tracker

Checked 2026-10-04 against Google documentation. Prices are public USD list prices and can vary by region and billing currency. No database resource was created.

## Conventional Cloud SQL for PostgreSQL

- Cloud SQL Enterprise supports a shared-core `db-f1-micro` with 0.6 GiB RAM, the smallest PostgreSQL instance Google documents. Shared-core machines have no Cloud SQL SLA. The [pricing page](https://cloud.google.com/sql/pricing) displays **$0.0105/hour for its default Iowa selection**, or about **$7.67 for 730 running hours**, before storage and backups. This is **not a verified Frankfurt quote**; use the page's Frankfurt (`europe-west3`) selector or the [pricing calculator](https://cloud.google.com/products/calculator) before provisioning. [Cloud SQL pricing](https://cloud.google.com/sql/pricing), [PostgreSQL FAQ](https://docs.cloud.google.com/sql/docs/postgres/faq).
- Minimum provisioned storage is **10 GB**. Provisioned SSD/HDD storage is billed in addition to the running instance; backup storage is billed on used backup bytes. The default Iowa pricing table shows SSD at $0.000465753/GiB-hour (about $3.40 per 10 GiB for 730 hours) and backups at $0.000109589/GiB-hour, with Frankfurt rates to be selected separately. Therefore even the default-region micro plus minimum SSD is roughly **$11/month before backup, network, or app hosting**; the existing $10/month total-cost aspiration cannot safely assume an always-on managed PostgreSQL instance. [Minimum storage](https://docs.cloud.google.com/sql/docs/postgres/issues-updating-storage-capacity), [pricing](https://cloud.google.com/sql/pricing), [backup FAQ](https://docs.cloud.google.com/sql/docs/postgres/faq).
- Cloud SQL pricing also distinguishes traffic to Google services and internet egress; a locally running client can incur outbound transfer charges. Check connection architecture and region before estimating. [Cloud SQL pricing](https://cloud.google.com/sql/pricing).
- The [$300/90-day Google Cloud Welcome credit](https://docs.cloud.google.com/free/docs/free-cloud-features) can cover eligible Cloud SQL usage if this billing account still has credit within its original eligibility window; remaining balance and expiry have **not** been verified. The standard Cloud SQL instance is not a perpetual free-tier product. Trial credit is temporary and does not change steady-state economics.

### Separate 30-day Cloud SQL trial

Google offers a PostgreSQL Cloud SQL trial instance at no instance-resource cost for 30 days, **in a new GCP project**, one per project. It has fixed Enterprise Plus 8-vCPU/64-GiB/100-GB configuration; its trial instance cannot use backups, and its data stops serving after 30 days unless upgraded. Public-internet/out-of-region transfer and a final backup can still be charged. Since this repo already has a dedicated project, eligibility for that project's trial path should be verified in the console; do not assume it applies. This is useful for a temporary migration experiment, but not a stable database plan. [PostgreSQL trial terms](https://docs.cloud.google.com/sql/docs/postgres/free-trial-instance).

### AI Studio developer edition caveat

Google also documents a Cloud SQL PostgreSQL **developer edition** with 50 compute units and 10 GB storage per billing account per month free, and scale-to-zero for the smallest configuration. However, Google says developer edition can be created **only through Google AI Studio** while building an AI Studio app, not by the Cloud SQL console, Admin API, `gcloud`, or Terraform. It lacks backups, private networking, and HA. Importing an existing billed project is possible through that workflow, but it would tie database setup to AI Studio and does not match the repository's reproducible deployment path. Do not treat this as a general free Cloud SQL tier. [Developer-edition requirements and pricing](https://docs.cloud.google.com/sql/docs/postgres/ai-assisted-coding-and-cloud-sql).

## Firestore Standard alternative

Firestore supports Frankfurt (`europe-west3`) and offers one free database per project: 1 GiB stored, 50,000 document reads/day, 20,000 writes/day, 20,000 deletes/day, and 10 GiB outbound transfer/month. Backup and point-in-time recovery are separately billed, even under the free database quota. It has no always-running database-instance floor; pricing is based on documents, indexes, storage, and transfer. [Firestore pricing and regions](https://cloud.google.com/firestore/pricing), [quotas](https://docs.cloud.google.com/firestore/quotas).

Firestore would require a larger application rewrite: the current SQLite schema uses relational tables, unique constraints, transactional claims, and SQL queries for jobs, classifications, evaluation history, and sync state. It might be cheaper at this scale, but it is not a drop-in PostgreSQL migration. Keep SQLite for local testing and compare a Cloud Run plus Firestore architecture only after mapping those queries and invariants to document transactions/indexes.

## Decision implication

For minimal code change and conventional hosted Postgres, Cloud SQL is the direct path, but obtain a Frankfurt quote and accept a likely recurring cost above the original $10/month total target. For a low idle bill, Firestore is worth a design spike, with a deliberate repository rewrite. Do not create either resource until the target architecture and expected post-credit monthly spend are agreed.
