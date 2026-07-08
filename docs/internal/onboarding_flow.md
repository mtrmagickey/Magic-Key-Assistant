# Onboarding Flow Spec

## Goal

Reduce first-run cognitive load for a non-technical micro-team operator by making onboarding product-first, local-first, and privacy-explicit.

## Principles

- Ask what the user wants to do before asking about infrastructure.
- Default to the simplest local path when the machine can support it.
- Label local-only and cloud-assisted modes in plain language.
- Make privacy consequences explicit before any cloud credential step.
- Seed a continuity-focused demo workspace so the product feels like an operational system, not a blank chatbot shell.
- Explain in plain language that the product stores notes, decisions, and follow-ups so later conversations can use them.

## Flow

### Step 1: Goal Selection

Prompt shown:

- What do you want help with first?

Selectable goals:

- Keep track of actions and follow-through
- Preserve decisions and rationale
- Keep shared notes and knowledge in one place
- Try a sample workspace first

Reasoning:

- These options anchor the product in day-to-day work outcomes rather than model choice.

### Step 2: Operating Path and Privacy

Options shown:

- Local-only
- Cloud-assisted

Presentation rules:

- Local-only is marked as recommended whenever a local runtime is detected.
- Cloud-assisted is clearly described as optional and external.
- Privacy notes are shown inline before any key-entry step.
- The screen gives a clear order of operations: choose local or cloud, choose sample data, then check local AI status.

### Step 3: Connection Details

Behavior:

- If local-only is selected, setup can continue with no cloud key.
- Cloud keys are optional and appear only after the privacy explanation.
- Tavily is framed as separate web search capability, not core setup.

### Step 4: Review and Finish

Summary shown:

- Primary job to be done
- How AI runs
- Work style
- Sample workspace choice
- Whether cloud keys were added now

Finish behavior:

- Save local config.
- Persist onboarding goal and privacy mode in org profile when possible.
- Seed the sample continuity workspace if chosen.
- Attempt automatic local provisioning when no local assistant path is ready.

## Demo Workspace Semantics

The seeded demo should showcase continuity, not generic productivity fluff.

Expected sample signals:

- One overdue follow-up
- One unowned action
- One documented decision with rationale
- One recurring obligation
- One continuity-themed feedback example

## Manual QA Notes

1. Open `/setup` on a clean install.
2. Confirm the first screen contains no provider-brand choice.
3. Confirm local-only is recommended when Ollama is present.
4. Confirm cloud-assisted copy explicitly says prompts may leave the device.
5. Finish setup with demo workspace enabled and confirm the dashboard is populated with continuity-shaped sample data.
6. Confirm the first setup screen explains that the product stores notes, decisions, and follow-ups for later conversations.
7. Finish setup without cloud keys and confirm setup still succeeds.
