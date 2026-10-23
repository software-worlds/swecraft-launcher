"""Canonical society checkpoint paths and the generated dataset card."""

from __future__ import annotations

from swecraft_society.models import CheckpointManifest

MANIFEST_NAME = "manifest.json"
CARD_NAME = "README.md"

APPARATUS_DIR = "apparatus"
WORLD_MANIFEST = "apparatus/world.yaml"
WORLD_LOCK = "apparatus/world.lock.json"
EVALUATION_MANIFEST = "apparatus/evaluation.yaml"
COORDINATOR_CONFIG = "apparatus/coordinator.yaml"
CHARTERS_DIR = "apparatus/charters"
IMAGES_DIR = "apparatus/images"

FORGE_VOLUME_TAR = "state/forge/forgejo-data.tar"
FORGE_HOST_DIR = "state/forge/host"
COORDINATOR_DB = "state/coordinator/coordinator.db"
CREDENTIALS_DIR = "state/coordinator/credentials"
API_TOKEN = "state/coordinator/api_token"
EVALUATOR_STATE = "state/evaluator/state.json"
RUNS_DIR = "runs"
EVALUATIONS_DIR = "evaluations"
REVIEWS_DIR = "reviews"


def render_card(manifest: CheckpointManifest) -> str:
    """Render the checkpoint's Hugging Face-compatible dataset card."""

    apparatus = "working tree"
    if manifest.apparatus.git_ref or manifest.apparatus.git_commit:
        apparatus = manifest.apparatus.git_ref or ""
        if manifest.apparatus.git_commit:
            apparatus = f"{apparatus} @ {manifest.apparatus.git_commit}".strip()
        if manifest.apparatus.dirty:
            apparatus += " (dirty)"
    lines = [
        f"# Society checkpoint {manifest.checkpoint_id}",
        "",
        f"- World: `{manifest.world_id}`",
        f"- Captured: {manifest.created_at.isoformat(timespec='seconds')}",
        f"- Hub commit: `{manifest.hub_commit}`",
        f"- Apparatus: `{apparatus}`",
        f"- Forge source: `{manifest.forge.source_url}`",
        f"- Agent image: `{manifest.agent_image.reference}` ",
        f"  (`{manifest.agent_image.image_id}`, `{manifest.agent_image.platform}`)",
        "",
        "## Repositories",
        "",
        "| Repository | Seed baseline | Head at checkpoint |",
        "| --- | --- | --- |",
    ]
    for head in manifest.heads:
        lines.append(f"| {head.name} | {head.baseline_commit} | {head.current_head} |")
    lines += [
        "",
        "## Contents",
        "",
        "| Contributor | Files | Bytes |",
        "| --- | ---: | ---: |",
    ]
    for contributor in manifest.contributors:
        total = sum(entry.size for entry in contributor.files)
        lines.append(f"| {contributor.name} | {len(contributor.files)} | {total} |")
    if manifest.omissions:
        lines += [
            "",
            "## Omissions",
            "",
            "This is a **partial** checkpoint: capture proceeded without the",
            "following expected files rather than lose recoverable state.",
            "",
        ]
        for omitted in manifest.omissions:
            lines.append(f"- `{omitted}`")
    lines += [
        "",
        "> This private checkpoint contains society-local Forge credentials,",
        "> including admin and agent tokens. It deliberately does not resolve or",
        "> capture external model, cloud, or Hugging Face credentials.",
        "",
    ]
    return "\n".join(lines)
