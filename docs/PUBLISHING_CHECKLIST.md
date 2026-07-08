# Publishing Checklist — Making This Repository Public

This repository was sanitized so the **working tree** contains zero private data.
However, private data was committed in the past, so it **still exists in this
repository's git history**. Deleting files in a new commit does **not** remove
them from history. Complete the steps below before publishing.

## Why a fresh repository (not this one)

The safest, least error-prone path is to publish a **fresh-history snapshot** to
a brand-new public repository rather than force-pushing rewritten history here:

- A history rewrite (`git filter-repo` / BFG) is riskier and every existing
  clone, fork, cached view, and PR of the current repo still holds the data.
- A new repo built from a single fresh initial commit guarantees none of the old
  blobs (the SQLite WAL/SHM sidecars, the private knowledge base under
  `LeisureLLM/docs/`, and the internal inventory) travel along.

## What was removed from the working tree

- `assistant.db-wal`, `assistant.db-shm` (repo root) — live DB sidecar pages
- `LeisureLLM/assistant.db-wal`, `LeisureLLM/assistant.db-shm` — live DB sidecar pages
- `LeisureLLM/docs/` — the private knowledge base (business/client/partner
  documents, Discord logs, meeting notes, real people's names)
- `docs/internal/repository_annotations.md` — internal inventory that indexed the
  private knowledge-base filenames and real names
- Real personal names removed from code comments in
  `LeisureLLM/services/hyde_retrieval.py` and
  `LeisureLLM/services/interaction_memory.py`

`.gitignore` and a pre-commit guard were hardened to prevent these from being
re-introduced.

## Step 1 — Create the clean public repository

1. Create a new empty repository on GitHub (private first, flip to public last).
2. From this sanitized working tree, publish a **single fresh initial commit**
   with no imported history. For example, in a fresh working copy of the
   sanitized tree:
   ```bash
   rm -rf .git
   git init
   git add -A
   git commit -m "Initial public release"
   git remote add origin <new-public-repo-url>
   git push -u origin main
   ```
   > Do this from a copy that has **no** reference to the old history.

## Step 2 — Rotate every credential that could have been exposed

Because a live database and internal docs were committed, treat all deployment
credentials as potentially compromised and rotate them:

- Discord bot token
- OpenAI / Anthropic / OpenRouter API keys
- Tavily API key (if used)
- Regenerate `LeisureLLM/config/.admin_token` (delete it; a new one is generated
  on next launch)

## Step 3 — Notify affected individuals

Private chat logs and a partnership agreement were committed. Notify the people
whose data appeared in the knowledge base, since that data may already exist in
prior clones or forks of the original repository.

## Step 4 — Verification gates (run against the NEW repo)

- [ ] Secret scan the **full history**, e.g.:
      `gitleaks detect --source . --redact` — must report no leaks.
- [ ] Grep the tree for tells and confirm no hits (aside from placeholders):
      `C:\Users`, `MTRMK`, real usernames, `@mtrmagickey.com`, `sk-`, Discord IDs.
- [ ] Confirm no tracked-but-ignored files remain:
      `git ls-files | git check-ignore --stdin` prints nothing.
- [ ] App still builds/tests: run Ruff, `pre-commit run --all-files`, and pytest.

## Step 5 — Ongoing safeguards on the public repo

- [ ] Enable GitHub **secret scanning** and **push protection**.
- [ ] Enable **Dependabot** alerts/updates.
- [ ] Ensure contributors run `pre-commit install` (the config now includes a
      secret-scanning hook and a guard that blocks DB files and the private
      knowledge base).
- [ ] Keep `SECURITY.md` disclosure policy in place.
