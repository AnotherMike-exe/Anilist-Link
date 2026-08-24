"""Every settings field must be backed by SETTINGS_MAP.

The settings page builds its display values with `for key in SETTINGS_MAP` and
saves with the same loop, so a field listed only in FIELD_GROUPS renders in the
form and is completely inert — it never shows a stored value and never saves
one. That is invisible from the template alone, which is how library.import_path
shipped broken.
"""

from __future__ import annotations

import ast
from pathlib import Path


def _field_group_keys() -> list[str]:
    """Setting keys from Settings.FIELD_GROUPS, read without importing FastAPI."""
    tree = ast.parse(Path("src/Web/Routes/Settings.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == (
            "FIELD_GROUPS"
        ):
            groups = ast.literal_eval(node.value)
            return [field[0] for _group, fields in groups for field in fields]
    raise AssertionError("FIELD_GROUPS not found")


def _settings_map_keys() -> set[str]:
    from src.Utils.Config import SETTINGS_MAP

    return set(SETTINGS_MAP)


# These live in the `libraries` table rather than app_settings, and the POST
# handler reads them from the form explicitly before the SETTINGS_MAP loop —
# so they are genuinely exempt rather than broken.
HANDLED_SEPARATELY = {"library.name", "library.paths"}


def test_every_settings_field_is_in_settings_map() -> None:
    """A field absent from SETTINGS_MAP can neither display nor save."""
    known = _settings_map_keys() | HANDLED_SEPARATELY
    missing = [k for k in _field_group_keys() if k not in known]
    assert not missing, (
        "These settings fields render but can never be saved or displayed, "
        f"because SETTINGS_MAP drives both loops: {missing}"
    )


def test_import_path_is_configurable() -> None:
    """The import folder is settable in the UI and by env var, like the other paths."""
    from src.Utils.Config import SETTINGS_MAP

    assert "library.import_path" in SETTINGS_MAP
    env_var, default = SETTINGS_MAP["library.import_path"]
    assert env_var == "LIBRARY_IMPORT_PATH"
    assert default == ""
    assert "library.import_path" in _field_group_keys()


def test_settings_map_env_vars_are_unique() -> None:
    """Two settings sharing an env var would silently overwrite each other."""
    from src.Utils.Config import SETTINGS_MAP

    seen: dict[str, str] = {}
    clashes: list[str] = []
    for key, (env_var, _default) in SETTINGS_MAP.items():
        if env_var in seen:
            clashes.append(f"{env_var}: {seen[env_var]} and {key}")
        seen[env_var] = key
    assert not clashes, f"Duplicate env vars: {clashes}"
