from __future__ import annotations

import json
from pathlib import Path

from app.recipes import RecipeCatalog, has_unresolved_placeholders, inspect_placeholders


ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE = ROOT / "knowledge"


def test_recipe_bundle_is_traceable_and_loadable() -> None:
    bundle = json.loads((KNOWLEDGE / "recipes.json").read_text(encoding="utf-8"))
    catalog = RecipeCatalog(KNOWLEDGE)
    recipes = catalog.load()

    assert bundle["schema_version"] == 1
    assert bundle["recipe_count"] == len(recipes) > 0
    assert bundle["recipe_revision"]
    assert len({recipe.recipe_id for recipe in recipes}) == len(recipes)
    assert all(recipe.source_revision for recipe in recipes)
    assert all(recipe.provenance for recipe in recipes)
    assert all(record.source_id and record.revision for recipe in recipes for record in recipe.provenance)

    manifest = json.loads((KNOWLEDGE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["recipe_count"] == len(recipes)
    assert manifest["recipe_revision"] == bundle["recipe_revision"]
    assert {item["recipe_id"] for item in manifest["recipes"]} == {
        recipe.recipe_id for recipe in recipes
    }

    source_lock = json.loads((KNOWLEDGE / "sources.lock.json").read_text(encoding="utf-8"))
    assert source_lock["recipe_revision"] == bundle["recipe_revision"]
    assert {item["recipe_id"] for item in source_lock["recipes"]} == {
        recipe.recipe_id for recipe in recipes
    }


def test_virtualenv_recipe_has_sourced_platform_variants() -> None:
    catalog = RecipeCatalog(KNOWLEDGE)
    recipe = catalog.load()[0]
    # The bundle is sorted by recipe id; use the explicit lookup to keep this
    # assertion independent of future recipe additions/order.
    recipe = catalog.by_id("python.virtualenv.create_activate")
    assert recipe is not None
    assert {slot.name for slot in recipe.required_slots} == {"target_path"}
    assert {slot.name for slot in recipe.optional_slots} >= {"environment_name", "shell"}
    assert any(slot.prompt_if_absent for slot in recipe.optional_slots)
    assert {"powershell", "cmd", "posix"}.issubset(recipe.shells)
    assert len(recipe.steps) == 2

    create = recipe.steps[0].templates or {}
    activate = recipe.steps[1].templates or {}
    assert "python -m venv" in create["powershell"]
    assert "Activate.ps1" in activate["powershell"]
    assert "activate.bat" in activate["cmd"]
    assert "/bin/activate" in activate["posix"]
    assert any(record.source_id == "python.venv.official" for record in recipe.provenance)
    assert any(record.role == "topic_basis" for record in recipe.provenance)


def test_generic_placeholder_inspection_is_conservative() -> None:
    command = "Get-NetTCPConnection -LocalPort <port> | Select-Object <pid>"
    placeholders = inspect_placeholders(command)
    assert [item["name"] for item in placeholders] == ["port", "pid"]
    assert placeholders[0]["type"] == "port"
    assert placeholders[1]["type"] == "value"
    assert has_unresolved_placeholders(command)
    assert not has_unresolved_placeholders("git revert HEAD")


def test_generic_placeholder_inspection_does_not_treat_redirection_as_slot() -> None:
    placeholders = inspect_placeholders("mysql app < <backup.sql>")
    assert [item["name"] for item in placeholders] == ["backup.sql"]
    assert placeholders[0]["type"] == "path"

