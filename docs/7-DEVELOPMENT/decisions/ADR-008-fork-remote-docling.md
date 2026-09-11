# Fork decision: shared Docling Serve for Open Notebook 1.14.0

This is a deployment-specific extension in the 2Elemental fork, based on upstream
tag v1.14.0 (30c7e2a63e43b7f270fc2c638f0b6246934a53f4). It does not change the
upstream decision to install local Docling only on request.

The application adapter offloads supported uploaded files to Docling Serve when
DOCLING_SERVE_URL is configured and the document engine is auto or docling.
It uses content-core 2.0.4 for MIME identification and all other extraction.
This avoids maintaining a second content-core fork. It does not patch installed
packages or fake a locally installed Docling runtime.

The API preflight and the source worker use the same adapter. The existing
capabilities response treats a valid remote endpoint as configured, as it already
does for remote Crawl4AI. This is not proof of server health or valid credentials;
conversion failures remain visible job errors. The existing UI Docling install
hint still describes the local mode; no frontend bundle is changed.

Uploads use multipart bytes and the asynchronous submission/poll/result API,
with X-Api-Key on every request. URLs retain the existing Crawl4AI/content-core
route. OCR, formula, picture-description and chart-extraction settings are sent
to the server, whose own model configuration controls their implementation.
Markdown feeds the existing embedding/storage flow. Partial or empty conversion
results are rejected. No fallback to another processor after a remote failure.
Redirects and environment HTTP proxies are disabled for this internal connection.

The total deadline defaults to 600 seconds. On timeout/cancellation the remote
task may finish on the server; it is not cancelled or deleted by this adapter.
The existing worker retry policy may submit a new task. Docling Serve remains
responsible for its result retention. This PR does not add a shared filesystem,
change networks, migrate data, or deploy services.

Build Dockerfile.remote-docling for the derivative image. Its version guard
deliberately fails on an unexpected base dependency version. For an upstream
upgrade, rebase/review the four copied Python files and rerun the tests before
changing the base image. Preserve the upstream LICENSE in every distribution.
