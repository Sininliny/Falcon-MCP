"""Sound-design guidance tools: recipes, blueprint checks, perceptual vocabulary and recipe verification."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .audio import read_audio
from .builder import LayerSpec
from .listen import feature_vector, listen
from .sound_design import (
    check_blueprint,
    guided_steps,
    knowledge,
    list_recipes,
    native_edit_plan,
    recipe,
    targets_for,
    verify_renders,
    vocabulary_targets,
)

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
Note = Annotated[int, Field(ge=0, le=127)]
Seconds = Annotated[float, Field(ge=0, le=30)]


class RenderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    path: str = Field(min_length=1, max_length=1024)
    note: Note
    velocity: int = Field(ge=1, le=127)
    note_on_seconds: Seconds | None = None
    note_off_seconds: Seconds | None = None


def register_design(mcp, config):
    def analyze(spec: RenderSpec) -> dict:
        audio, rate = read_audio(config.audio_path(spec.path))
        return listen(audio, rate, spec.note, spec.note_on_seconds, spec.note_off_seconds, include_ascii=False)

    @mcp.tool(annotations=READ)
    async def falcon_sound_recipe(
        action: Literal["list", "get", "principles"] = "list", recipe_id: str | None = None,
        note: Note | None = None,
    ) -> dict[str, Any]:
        """Sound-design recipes for analog-v1 builds: blueprint, native follow-ups, targets and guided steps.

        list names recipes (sub/reese bass, pluck, keys, pad, strings, lead, brass, organ) with role and
        register. get returns one recipe: a falcon_build_instrument layers blueprint, native_edits with
        selectors relative to the Program (Waveform is chosen by sweep plus harmonic signature because the
        offline index has no enum names), beyond_builder elements (filters/LFOs/effects), measurable targets
        for falcon_match_target keytracked to note, velocity range, pitfalls and a step-by-step plan.
        principles returns the acoustics/psychoacoustics rules the checks use. Conventions, not verdicts.
        """
        if action == "list":
            return {"recipes": list_recipes(), "template": knowledge()["template_id"], "scope": knowledge()["scope"]}
        if action == "principles":
            return {"principles": knowledge()["principles"], "waveform_signatures": knowledge()["waveform_signatures"]}
        if recipe_id is None:
            raise ValueError("get requires recipe_id")
        spec = recipe(recipe_id)
        return {**spec, "targets": targets_for(recipe_id, note), "targets_note": note or spec["register"]["reference_note"],
                "native_edit_plan": native_edit_plan(recipe_id), "steps": guided_steps(recipe_id),
                "blueprint_check": check_blueprint([LayerSpec.model_validate(x) for x in spec["blueprint"]],
                                                   recipe_id=recipe_id)}

    @mcp.tool(annotations=READ)
    async def falcon_design_check(
        layers: Annotated[list[LayerSpec], Field(min_length=1, max_length=16)] | None = None,
        recipe_id: str | None = None, role: Literal["Lead", "Keys", "Bass", "Kit"] = "Keys",
        low_key: Note | None = None, high_key: Note | None = None,
    ) -> dict[str, Any]:
        """Static sound-design sanity check of a falcon_build_instrument blueprint before building.

        Finds dead key/velocity zones, stacked zones, estimated gain staging and chord headroom, zone level
        seams, onset/release click risk (release vs one period of the lowest note), blips, lead sustain and
        bass release mud, detune beat rates (chorus vs roughness at the top key), fixed dyads, low-register
        detune and recipe envelope conventions. layers omitted checks the recipe's own blueprint.
        Estimates assume oscillator peak 1.0 x gain; render and listen to confirm.
        """
        if layers is None:
            if recipe_id is None:
                raise ValueError("Provide layers, recipe_id or both")
            layers = [LayerSpec.model_validate(x) for x in recipe(recipe_id)["blueprint"]]
        return check_blueprint(layers, role, recipe_id, low_key, high_key)

    @mcp.tool(annotations=READ)
    async def falcon_timbre_vocabulary(
        terms: Annotated[list[str], Field(max_length=6)] | None = None,
        baseline: RenderSpec | None = None,
    ) -> dict[str, Any]:
        """Translate words such as brighter, warmer, less_muddy, punchier, wider into features and moves.

        terms omitted lists the vocabulary. With baseline (a render of the current patch), returns a numeric
        target for falcon_match_target/falcon_design_verify: requested directions use about one noticeable
        step, and hold features (level, attack) guard against side effects. Render the edit with identical
        notes and controls. Words map to measurable proxies, not to how a listener will judge the sound.
        """
        vocabulary = knowledge()["vocabulary"]
        if not terms:
            return {"terms": {k: {"opposite": v.get("opposite"), "moves": v["moves"]} for k, v in vocabulary.items()}}
        vector = None
        if baseline is not None:
            report = await asyncio.to_thread(analyze, baseline)
            if report["level"]["silent"]:
                raise ValueError("Baseline render is silent")
            vector = feature_vector(report)
        return vocabulary_targets(terms, vector)

    @mcp.tool(annotations=READ)
    async def falcon_design_verify(
        recipe_id: str, renders: Annotated[list[RenderSpec], Field(min_length=1, max_length=24)],
    ) -> dict[str, Any]:
        """Verify single-note renders (audio root) against a recipe's targets and cross-render rules.

        Each render is one note at one velocity; pass note on/off seconds for reliable ADSR. Per render:
        keytracked target evaluation with directions and parameter hints, and the closest waveform
        signature for single-layer recipes. Across renders: velocity level range and monotonicity,
        velocity brightness ratio, register balance and decay keytracking. Conventions, not quality scores.
        """
        recipe(recipe_id)
        reports = await asyncio.gather(*(asyncio.to_thread(analyze, spec) for spec in renders))
        return verify_renders(recipe_id, [{"path": spec.path, "note": spec.note, "velocity": spec.velocity,
                                           "report": report} for spec, report in zip(renders, reports)])
