# Local Inference Fallback

## Purpose

Document what the onboarding flow should do when local inference is not available during first-run setup.

## Required Behavior

- Do not block setup on provider choice.
- Do not force the user to choose between Ollama, OpenAI, Anthropic, or other providers on the first screen.
- Allow setup to complete even when no local runtime is ready.
- Seed the continuity demo workspace if the user asked for it.
- Attempt automatic local provisioning after setup when no assistant path is configured.
- Keep cloud access optional and clearly labeled as external processing.

## Current Runtime Behavior

When `/api/v1/setup/complete` runs:

- It writes the setup-complete marker.
- It attempts automatic pipeline configuration.
- If no usable assistant path is configured, it starts background local provisioning through the llama.cpp manager.
- It seeds demo workspace data when requested.

## User-Facing Copy Requirements

- Say "local-only" instead of opening with provider names.
- Say "cloud-assisted" instead of opening with vendor jargon.
- State explicitly that cloud-assisted mode may send prompts and selected context to the configured provider.
- State explicitly that local-only keeps prompts on the device by default.

## Troubleshooting Guidance

If local inference still is not available after onboarding:

1. Open Settings and confirm whether a cloud key was intentionally added.
2. Open Model Router only if you need to inspect or override the default assistant path.
3. Check the local runtime status if you expected Ollama or llama.cpp to be available.
4. Continue using the seeded continuity workspace while local provisioning completes or while you decide whether cloud assistance is acceptable.