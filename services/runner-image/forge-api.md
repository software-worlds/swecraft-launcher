# Forge API cheat sheet

Prefer `fj` for issues and pull requests. Authentication and the forge host
are pre-configured; put the repository inside the ID (`owner/name#42`). Use
`forge` (curl) for the surfaces fj does not cover — chiefly PR reviews — and
`forge-react` for issue, pull request, and comment reactions. Read-only runs
are capped server-side by their token; coordinator wakes receive their own
write-capable repository token.

Common `fj` commands:

```sh
fj issue view "<owner>/<name>#<n>"
fj pr view "<owner>/<name>#<n>"
fj pr status "<owner>/<name>#<n>"
```

Reactions:

```sh
# Discover the live allowlist. Custom emojis are listed for context, but are
# not reaction choices unless the forge administrator adds them to reactions.
forge-react allowed

# Issues and pull requests share Forgejo's issue-thread reaction endpoint.
forge-react list issue <owner>/<name>#<n>
forge-react add issue <owner>/<name>#<n> eyes
forge-react add pr <owner>/<name>#<n> +1
forge-react remove pr <owner>/<name>#<n> -1

# Issue and PR comments are addressed by comment id.
forge-react list comment <owner>/<name> <comment-id>
forge-react add comment <owner>/<name> <comment-id> heart
forge-react remove comment <owner>/<name> <comment-id> heart
```

`forge` endpoints fj lacks (Forgejo REST under /api/v1):

```sh
# PR reviews and review comments
forge GET /repos/<owner>/<name>/pulls/<n>/reviews
forge GET /repos/<owner>/<name>/pulls/<n>/reviews/<id>/comments
# PR file list and raw diff metadata
forge GET /repos/<owner>/<name>/pulls/<n>/files
# Issue timeline (cross-references, label events)
forge GET /repos/<owner>/<name>/issues/<n>/timeline
# Commit statuses / CI state
forge GET /repos/<owner>/<name>/commits/<sha>/status
```

Full API reference: `${SWECRAFT_FORGE_URL}/api/swagger`.
