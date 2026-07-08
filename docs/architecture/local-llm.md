# Local LLM Architecture

> How Magic Key Assistant stays useful with local inference as the default path.

## Design Goal

Local inference is the primary operating mode, not a degraded fallback.

The system is designed so a user can run the product end-to-end on one machine with a local model runtime, then optionally layer in cloud models for specific higher-cost or higher-quality roles.

## Main Pieces

### Runtime Detection

The application detects available local inference backends during startup and setup.

The main supported local paths are:

- Ollama for straightforward local model management
- llama.cpp for users who want tighter hardware control or faster local serving on specific models

### Model Router

The model router is the central dispatch point for language-model calls.

It is responsible for:

- backend registration
- per-role model selection
- fallback behavior
- timeout handling
- cost and trace instrumentation

The router allows different tasks to use different models without scattering backend logic across the codebase.

### Multi-Stage Generation

The product supports a staged answer pipeline rather than assuming a single model call is always enough.

The common roles are:

- initial generation
- critique or verification
- synthesis

Those roles can all run locally, or a user can keep the fast draft local and route more expensive passes elsewhere.

### Embeddings and Retrieval

Local inference is not only for chat output. It also supports retrieval.

The local stack is used for:

- embeddings for the knowledge base
- retrieval helpers such as HyDE-style expansion
- chunk enrichment and metadata extraction
- document-grounded answer generation

This matters because a local-first product that only localizes the chat layer is still partially cloud-shaped. Here the retrieval path is also designed to stay local when possible.

### Onboarding and Fallbacks

Setup does not force the user into a cloud dependency when local inference is not immediately ready.

The system can:

- continue onboarding without a final provider decision
- prefer local runtimes when available
- fall back gracefully when a local path is not ready yet
- keep cloud usage explicit instead of implicit

For the operator-facing onboarding rules, see [Local Inference Fallback](../internal/local_inference_fallback.md).

## Why This Matters

A local-first operations product needs more than a local chat endpoint.

It needs:

- local answer generation
- local retrieval and indexing support
- local-friendly onboarding
- operator-visible routing and health controls
- a clean escape hatch to cloud models when the user explicitly wants one

That is the shape this architecture is trying to preserve.

## Control Surface

The admin console remains the place where operators inspect and change model behavior.

Typical controls include:

- backend availability and health
- pipeline role assignment
- timeout and routing behavior
- model testing and diagnostics
- device-capability checks and recommendations

## Related Docs

- [System Architecture](system.md)
- [Getting Started](../../GET_STARTED.md)
- [Internal Local Inference Notes](../internal/local_inference_fallback.md)
