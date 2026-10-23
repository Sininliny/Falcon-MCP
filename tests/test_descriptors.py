import json

import pytest

from falcon_mcp.descriptors import (
    builtin_bank,
    descriptor_deltas,
    resolve_facets,
    select_facets,
    validate_custom,
)


def test_banks_are_well_formed():
    bank = builtin_bank()
    for group in bank:
        assert group["banks"] and len(group["items"]) >= 2
        assert all(item["label"] and item["text"] for item in group["items"])
    keys = {group["facet"] for group in select_facets("keys")}
    assert {"Keyboard instrument", "Hammer and attack", "Mechanics and noise", "Electric piano movement"} <= keys
    labels = {item["label"] for group in select_facets("keys") for item in group["items"]}
    assert {"Felt piano", "Concert grand piano", "Wurlitzer electric piano", "Rhodes electric piano",
            "Tremolo", "Audible mechanics", "Intimate and close", "Reed bark", "Metallic tine ping"} <= labels
    assert "Brightness" in {g["facet"] for g in select_facets("synth")} and "Brightness" not in keys
    assert len(select_facets("all")) == len(bank)


def test_custom_descriptors():
    custom = [{"facet": "Felt depth", "items": ["A thin felt piano.", {"label": "Thick", "text": "A thick felt."}]}]
    facets = select_facets("keys", custom)
    assert facets[-1] == {"facet": "Felt depth", "banks": ["custom"], "items": [
        {"label": "A thin felt piano.", "text": "A thin felt piano."}, {"label": "Thick", "text": "A thick felt."}]}
    assert select_facets("custom", custom)[0]["facet"] == "Felt depth"
    with pytest.raises(ValueError, match="custom"):
        select_facets("custom")
    with pytest.raises(ValueError, match="2.."):
        validate_custom([{"facet": "x", "items": ["one"]}])
    with pytest.raises(ValueError):
        select_facets("organ")


def test_descriptor_file_is_confined(config):
    config.audio_root.mkdir()
    path = config.audio_root / "words.json"
    path.write_text(json.dumps({"facets": [{"facet": "Body", "items": ["A big body.", "A small body."]}]}))
    assert resolve_facets(config, "custom", None, "words.json")[0]["facet"] == "Body"
    with pytest.raises(ValueError, match="inside"):
        resolve_facets(config, "custom", None, str(config.audio_root.parent / "x.json"))


def test_descriptor_deltas_rank_by_size():
    before = {"scores": [{"facet": "A", "label": "x", "cosine_similarity": 0.2},
                         {"facet": "A", "label": "y", "cosine_similarity": 0.3}]}
    after = {"scores": [{"facet": "A", "label": "x", "cosine_similarity": 0.25},
                        {"facet": "A", "label": "y", "cosine_similarity": 0.1}]}
    rows = descriptor_deltas(before, after)
    assert rows[0]["label"] == "y" and rows[0]["delta"] == pytest.approx(-0.2)
    assert descriptor_deltas({}, after) == []
