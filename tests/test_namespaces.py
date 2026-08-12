"""The core must reject un-namespaced plugin attributes."""

from __future__ import annotations

import json

from cadastre.core.errors import Located
from cadastre.core.observed import parse_source, source_to_dict
from cadastre.plugins import parse_plugin_info


def test_plugin_attributes_are_namespaced() -> None:
    try:
        parse_plugin_info(
            {
                "name": "demo",
                "version": "1",
                "capabilities": [],
                "entities": [
                    {
                        "kind": "host",
                        "authority": "source",
                        "attributes": {"type": "object", "properties": {"host": {}}},
                    }
                ],
            }
        )
    except ValueError as exc:
        assert "x-<plugin>" in str(exc)
    else:
        raise AssertionError("un-namespaced plugin attributes were accepted")


def test_x_plugin_attributes_are_accepted() -> None:
    info = parse_plugin_info(
        {
            "name": "demo",
            "version": "1",
            "capabilities": [],
            "entities": [
                {
                    "kind": "host",
                    "authority": "source",
                    "attributes": {
                        "type": "object",
                        "properties": {"x-demo-host": {"type": "string"}},
                    },
                }
            ],
        }
    )
    assert "x-demo-host" in info.entities[0].attributes["properties"]


def test_declared_x_plugin_attributes_are_retained_in_observed_entity() -> None:
    source = parse_source(
        {"entities": {"host": [{"id": "node-1", "x-demo-host": "upstream-42"}]}},
        Located("demo:inventory.list"),
        extensions={"host": {"x-demo-host"}},
    )
    entity = source.entities["host"][0]
    assert entity.extra == {"x-demo-host": "upstream-42"}
    assert source.entities["host"][0].id == "node-1"


def test_namespaced_attributes_survive_observed_snapshot_round_trip() -> None:
    original = parse_source(
        {"entities": {"host": [{"id": "node-1", "x-demo-host": "upstream-42"}]}},
        Located("demo:inventory.list"),
        extensions={"host": {"x-demo-host"}},
    )
    restored = parse_source(
        json.loads(json.dumps(source_to_dict(original))), Located("observed/demo.json")
    )
    assert restored.entities["host"][0].extra == {"x-demo-host": "upstream-42"}
    assert restored.extensions == {"host": ("x-demo-host",)}
