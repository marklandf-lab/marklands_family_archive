---
name: ship
description: Commit all working tree changes, push to the current claude/... branch, and open or update its PR. Use when the user says "ship", "/ship", "ship it", or asks to commit + push + PR the current work.
---

# /ship — Commit, Push, and Open or Update PR

Commit all working tree changes, push to the current branch, and create a PR if one does not already
exist. All GitHub operations use the `gh` CLI (installed and authenticated here). Do NOT use GitHub
MCP tools or ToolSearch — there is no GitHub MCP server configured in this environment, and reaching
for one is what makes this skill stall.

## Steps

1. **Inspect the working tree**
   - Run `git status` to see all modified, new, and deleted files.
   - Run `git diff HEAD` to understand what changed (use this to write the commit message).
   - Warn the user and stop if any file looks like it contains secrets (`.env`, `credentials`, `*_key`, `*_secret`, `*_token` in the filename). Ask before staging it.

2. **Pick a safe branch**
   - Run `git branch --show-current`.
   - If it is `main` (the default branch), create a fresh `claude/<short-topic>` branch first
     (`git switch -c claude/<topic>`) — never commit straight to `main`. Use a NEW branch name; do
     not reuse a previously merged one.

3. **Stage everything**
   - Run `git add -A` to stage all changes in the working tree, including untracked files.

4. **Commit**
   - Write a concise commit message (1–2 sentences) describing WHY the changes were made, not just what files changed.
   - Pass it with a HEREDOC, and end with the commit trailer your harness specifies
     for this session. Take the model name and session URL from the environment, not
     from this file — pinning a model version here is what left the trailer reading
     "Opus 4.8" long after the model had moved on. The shape is:
     ```bash
     git commit -m "$(cat <<'EOF'
     <subject line>

     <optional body explaining why>

     Co-Authored-By: Claude <model> <noreply@anthropic.com>
     Claude-Session: <session URL>
     EOF
     )"
     ```
   - If the harness gives no trailer, match the most recent commit on `main`
     (`git log -1 --format=%b`) rather than inventing one.
   - If there is nothing to commit, skip to step 6.

5. **Push**
   - Run `git push -u origin "$(git branch --show-current)"`.
   - If push fails due to a network error, retry up to 4 times with exponential backoff (2s, 4s, 8s, 16s).

6. **Check for an existing PR** (via `gh`)
   ```bash
   BR="$(git branch --show-current)"
   gh pr list --repo marklandf-lab/marklands_family_archive --head "$BR" --state open --json number,url --jq '.[0].url'
   ```
   - If that prints a URL: an open PR already exists — report it and stop. The new commits are already in it.
   - If it prints nothing: create one (step 7).

7. **Create a PR** (only if none exists)
   ```bash
   gh pr create --repo marklandf-lab/marklands_family_archive --base main --head "$BR" \
     --title "<short title, under 70 chars>" \
     --body "$(cat <<'EOF'
   ## Summary
   <bullet points covering what changed and why>

   ## Test plan
   <bulleted checklist of things to verify>

   🤖 Generated with [Claude Code](https://claude.com/claude-code)
   EOF
   )"
   ```
   - `gh pr create` prints the new PR URL — report it when done.

## Context

- Repo: `marklandf-lab/marklands_family_archive` (this fork — remote `origin`); default branch `main`.
  GitHub auth: `gh auth status`.
- The `wyeast` remote is Wyeast's own `WyeastCorp/mac_family_archive`, which this repo is forked
  FROM. Never open a PR against it: this fork exists for UI experiments that are not upstream's to
  review. Every `gh` call here names the fork explicitly for that reason.
- Primary development branch pattern: `claude/...`. Never push to `main` directly.
- Use `gh` for every GitHub call (PR list/create, issues, API). No MCP, no ToolSearch.