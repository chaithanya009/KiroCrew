# Fork maintenance

This fork tracks `kirodotdev/KiroCrew` as `upstream` and
`chaithanya009/KiroCrew` as `origin`. Its local changes are intentional product
requirements. Read this file and the repository's `AGENTS.md` before every
upstream sync.

## Required behavior

- Artifacts are disabled throughout the agent, bundled skills, MCP tool list,
  API, and dashboard. Do not restore artifact creation, saving, listing,
  publishing, inline-widget artifact registration, or instructions promoting
  those features. Ordinary file and image display remains available.
- Persistent agent memory is disabled throughout context assembly, bundled
  skills, MCP tools, API, scheduled learning, and dashboard. Conversation
  transcripts and session recovery are separate features and must continue to
  work. Do not reintroduce memory read/write instructions or tools.
- The source-controlled bundled `config/prompt.md` is the agent prompt. Do not
  restore a user-home override. Keep it concise. User-facing responses should
  lead with the outcome and explain behavior in plain language; do not list
  internal file or function names unless requested or needed to act.
- Automatic recovery of an interrupted turn must preserve the original request
  and bounded context from completed tool results. The agent must be able to
  continue from the last completed step without assuming it has history it
  cannot actually see or repeating successful actions.
- Keep the running `/Applications/KiroCrew.app` separate from source syncs.
  Fetching, merging, building, and pushing code do not authorize restarting or
  replacing the installed app. Ask the user before installing or restarting.

The fork's feature flags and API exclusions are collected in
`src/kiro_crew/fork_profile.py`. Review the fork commits as well as that file
when upstream refactors these areas; one flag alone does not cover the UI,
prompt, or skill surfaces.

## Upstream sync procedure

1. Inspect `git status` and the current branch. If there are unrelated local
   changes or an active merge, leave them intact and report the blocker.
2. Fetch `upstream main`. Review the incoming commits and overlaps with the
   required behavior above. Integrate upstream into the fork's `main` using a
   merge, preserving upstream improvements outside the fork's requirements.
   Never discard a conflict wholesale just to finish a merge.
3. Resolve each conflict against the actual upstream behavior. Trace renamed
   producers and consumers: prompt assembly, skills, MCP registrations, API,
   dashboard, and interruption recovery. Update the owning documentation when
   behavior changes. Add focused regression coverage for any adaptation of the
   recovery fix.
4. Inspect the final diff against both parents and check that artifact and
   memory features remain absent from the product. Run the smallest relevant
   validation for the merged changes, including the docs gate when docs change.
   Do not claim checks passed if they were skipped or failed.
5. Commit the sync if clean. Push `main` to `origin` only when explicitly
   authorized by the user; never force-push. Report what changed and any
   unresolved conflict or validation failure. Do not install or restart the app
   unless separately authorized.

A missed scheduled run needs no special replay: the next successful fetch and
merge includes all upstream commits that arrived since the last sync. The
desktop scheduler itself depends on the computer and Codex app being available;
this file does not imply it will wake a sleeping Mac.
