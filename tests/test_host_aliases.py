"""WI-4560: a host answers to its aliases (hostname, tailnet name, LAN or tailnet IP).

`lookup` and `search` find a host by any alias, and the write gate refuses a
catalog in which two hosts claim the same alias: an address that means two
machines is a lookup that cannot be trusted.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from cadastre.cli.lookup import lookup
from cadastre.cli.search import search
from cadastre.cli.session import Session
from cadastre.core import model
from cadastre.core.errors import AmbiguousEntityError, MissingEntityError
from cadastre.core.rules import check_catalog
from tests.conftest import DECLARED_AS_OF, EXAMPLE_CATALOG, NOW

LAN_IP = "192.168.1.10"


def _aliased_estate(tmp_path: Path, hosts: dict[str, list[str]]) -> Session:
    """A copy of the example catalog with `aliases` set on the named hosts."""
    root = tmp_path / "aliased"
    shutil.copytree(EXAMPLE_CATALOG, root)
    path = root / "declared" / "hosts" / "hosts.yaml"
    entries = yaml.safe_load(path.read_text(encoding="utf-8"))
    for entry in entries:
        if entry["id"] in hosts:
            entry["aliases"] = hosts[entry["id"]]
    path.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")
    return Session.open(root, now=NOW, as_of=DECLARED_AS_OF)


def test_an_alias_is_loaded_onto_the_host(tmp_path: Path) -> None:
    session = _aliased_estate(tmp_path, {"app-01": ["app-01.lan", LAN_IP]})
    host = session.catalog.get("host", "app-01")
    assert isinstance(host, model.Host)
    assert host.aliases == ("app-01.lan", LAN_IP)


def test_lookup_finds_a_host_by_its_address(tmp_path: Path) -> None:
    session = _aliased_estate(tmp_path, {"app-01": ["app-01.lan", LAN_IP]})
    data = json.dumps(lookup(session, LAN_IP).data)
    assert '"id": "app-01"' in data
    assert "app-01" in data and "resolution" in data


def test_alias_lookup_ignores_case(tmp_path: Path) -> None:
    session = _aliased_estate(tmp_path, {"app-01": ["App-01.LAN"]})
    assert '"id": "app-01"' in json.dumps(lookup(session, "app-01.lan").data)


def test_an_id_is_still_found_before_an_alias(tmp_path: Path) -> None:
    """An alias must never shadow a real id: the id is the more specific claim."""
    session = _aliased_estate(tmp_path, {"app-02": ["app-01"]})
    data = json.dumps(lookup(session, "app-01").data)
    assert '"id": "app-01"' in data
    assert '"id": "app-02"' not in data


def test_search_finds_a_host_by_its_alias(tmp_path: Path) -> None:
    session = _aliased_estate(tmp_path, {"app-01": ["app-01.lan"]})
    data = json.dumps(search(session, "app-01 lan", kind="host").data)
    assert '"id": "app-01"' in data


def test_an_unknown_name_still_reports_missing(tmp_path: Path) -> None:
    session = _aliased_estate(tmp_path, {"app-01": [LAN_IP]})
    with pytest.raises(MissingEntityError):
        lookup(session, "10.9.9.9")


def test_an_alias_claimed_by_two_hosts_cannot_be_looked_up(tmp_path: Path) -> None:
    session = _aliased_estate(
        tmp_path, {"app-01": [LAN_IP], "app-02": [LAN_IP.upper()]}
    )
    with pytest.raises(AmbiguousEntityError) as raised:
        lookup(session, LAN_IP)
    assert "app-01" in str(raised.value) and "app-02" in str(raised.value)


def test_two_hosts_claiming_one_alias_is_a_write_gate_error(tmp_path: Path) -> None:
    session = _aliased_estate(tmp_path, {"app-01": [LAN_IP], "app-02": [LAN_IP]})
    findings = [
        f for f in check_catalog(session.catalog) if f.code == "host-alias-collision"
    ]
    assert len(findings) == 1
    assert findings[0].level == "error"
    assert findings[0].subject == LAN_IP
    why = findings[0].why or ""
    assert "app-01" in why and "app-02" in why


def test_a_catalog_without_shared_aliases_passes_the_gate(tmp_path: Path) -> None:
    session = _aliased_estate(tmp_path, {"app-01": ["app-01.lan"], "app-02": [LAN_IP]})
    assert not [
        f for f in check_catalog(session.catalog) if f.code == "host-alias-collision"
    ]
