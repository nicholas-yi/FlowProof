from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from flowproof.guards import FrozenTests, TamperReport, fingerprint
from flowproof.guards.signals import signals
from flowproof.guards.tamper import (
    ADDED_TEST_FILE,
    CONFIG_OVERRIDE,
    DELETED_TEST,
    EARLY_EXIT,
    MODIFIED_TEST,
    SKIP_MARKER,
    TRIVIAL_ASSERT,
)

PACKAGE_JSON = json.dumps({"name": "demo", "scripts": {"test": "jest", "build": "tsc"}}, indent=2)

PROJECT: dict[str, str] = {
    "calc.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": (
        "import sys\n\nimport pytest\n\nfrom calc import add\n\n\n"
        "@pytest.mark.skipif(sys.platform == 'nope', reason='never true')\n"
        "def test_add():\n    assert add(2, 3) == 5\n\n\n"
        "@pytest.mark.skip(reason='pre-existing skip')\n"
        "def test_legacy():\n    assert add(1, 1) == 2\n"
    ),
    "tests/test_other.py": "def test_other():\n    assert 1 + 1 == 2\n",
    "other/test_extra.py": "import sys\nimport unittest\n\nimport pytest\n\n\ndef test_extra():\n"
    "    assert True\n",
    "tools/cli.py": "import sys\n\n\ndef main():\n    return 0\n",
    "conftest.py": "",
    "pytest.ini": "[pytest]\ntestpaths = tests\n",
    "tox.ini": "[tox]\nenvlist = py312\n",
    "setup.cfg": "[metadata]\nname = demo\n\n[tool:pytest]\naddopts = -q\n",
    "pyproject.toml": (
        '[project]\nname = "demo"\ndependencies = []\n\n[tool.pytest.ini_options]\naddopts = "-q"\n'
    ),
    "package.json": PACKAGE_JSON + "\n",
    "jest.config.js": "module.exports = {};\n",
    "vitest.config.ts": "export default {};\n",
}


def _write(root: Path, rel: str, text: str | bytes) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
    target.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return target


def _append(root: Path, rel: str, text: str) -> None:
    _write(root, rel, (root / rel).read_text(encoding="utf-8") + text)


@pytest.fixture
def base(tmp_path: Path) -> Path:
    root = tmp_path / "base"
    for rel, text in PROJECT.items():
        _write(root, rel, text)
    return root


@pytest.fixture
def lease(base: Path, tmp_path: Path) -> Path:
    return Path(shutil.copytree(base, tmp_path / "lease"))


@pytest.fixture
def frozen(base: Path) -> FrozenTests:
    return FrozenTests.capture(base, ["tests/**"])


def _only(report: TamperReport, kind: str, path: str) -> None:
    assert [(f.kind, f.path) for f in report.findings] == [(kind, path)], report.findings


def test_an_unchanged_tree_has_no_findings(frozen: FrozenTests, base: Path, lease: Path) -> None:
    assert frozen.inspect(lease).findings == ()
    assert frozen.inspect(base).findings == ()
    assert not frozen.inspect(lease).tampered


def test_capture_freezes_the_patterns_and_test_configuration(frozen: FrozenTests) -> None:
    assert frozen.captured
    assert frozen.paths == (
        "conftest.py",
        "jest.config.js",
        "package.json",
        "pyproject.toml",
        "pytest.ini",
        "setup.cfg",
        "tests/test_calc.py",
        "tests/test_other.py",
        "tox.ini",
        "vitest.config.ts",
    )
    assert len(frozen.manifest()["tests/test_calc.py"]) == 64


def test_caches_under_the_patterns_are_not_findings(frozen: FrozenTests, lease: Path) -> None:
    _write(lease, "tests/__pycache__/test_calc.cpython-312.pyc", b"\x00cache")
    _write(lease, ".pytest_cache/v/cache/nodeids", "[]")
    assert frozen.inspect(lease).findings == ()


def test_a_modified_frozen_test_is_definitive(frozen: FrozenTests, lease: Path) -> None:
    text = (lease / "tests/test_calc.py").read_text(encoding="utf-8")
    _write(lease, "tests/test_calc.py", text.replace("== 5", "== 6"))
    report = frozen.inspect(lease)
    _only(report, MODIFIED_TEST, "tests/test_calc.py")
    finding = report.findings[0]
    assert finding.definitive
    assert "-assert add(2, 3) == 5" in finding.evidence
    assert "+assert add(2, 3) == 6" in finding.evidence
    assert report.tampered


def test_a_deleted_frozen_test_is_definitive(frozen: FrozenTests, lease: Path) -> None:
    (lease / "tests/test_other.py").unlink()
    report = frozen.inspect(lease)
    _only(report, DELETED_TEST, "tests/test_other.py")
    assert report.tampered


def test_a_file_added_under_the_patterns_is_definitive(frozen: FrozenTests, lease: Path) -> None:
    _write(lease, "tests/sub/test_new.py", "\ndef test_new():\n    pass\n")
    report = frozen.inspect(lease)
    _only(report, ADDED_TEST_FILE, "tests/sub/test_new.py")
    assert "def test_new():" in report.findings[0].evidence


def test_a_new_skip_in_a_frozen_test_is_also_flagged_but_old_ones_are_not(
    frozen: FrozenTests, lease: Path
) -> None:
    text = (lease / "tests/test_calc.py").read_text(encoding="utf-8")
    _write(
        lease, "tests/test_calc.py", text.replace("def test_add", "@pytest.mark.skip\ndef test_add")
    )
    report = frozen.inspect(lease)
    assert [(f.kind, f.path) for f in report.findings] == [
        (MODIFIED_TEST, "tests/test_calc.py"),
        (SKIP_MARKER, "tests/test_calc.py"),
    ]
    assert report.heuristic[0].evidence == "line 9: @pytest.mark.skip"
    assert not report.heuristic[0].definitive


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ("    pytest.skip('agent')", SKIP_MARKER),
        ("@pytest.mark.skip", SKIP_MARKER),
        ("@pytest.mark.skipif(True, reason='agent')", SKIP_MARKER),
        ("@unittest.skip('agent')", SKIP_MARKER),
        ("@pytest.mark.xfail", SKIP_MARKER),
        ("    assert True", TRIVIAL_ASSERT),
        ("    self.assertTrue(True)", TRIVIAL_ASSERT),
        ("    assert 1 == 1", TRIVIAL_ASSERT),
        ("sys.exit(0)", EARLY_EXIT),
        ("os._exit(0)", EARLY_EXIT),
        ("raise SystemExit", EARLY_EXIT),
    ],
)
def test_heuristics_in_a_test_file_outside_the_patterns(
    frozen: FrozenTests, lease: Path, line: str, kind: str
) -> None:
    _append(lease, "other/test_extra.py", f"\n{line}\n")
    report = frozen.inspect(lease)
    _only(report, kind, "other/test_extra.py")
    assert report.findings[0].evidence.endswith(line.strip())
    assert report.findings[0].evidence.startswith("line 10: ")
    assert not report.tampered
    assert report.suspicious


def test_heuristics_in_a_new_javascript_test_file(frozen: FrozenTests, lease: Path) -> None:
    _write(lease, "web/app.test.js", "it.only('runs alone', () => {});\n")
    _only(frozen.inspect(lease), SKIP_MARKER, "web/app.test.js")


@pytest.mark.parametrize(
    ("rel", "text"),
    [
        ("calc.py", "def add(a, b):\n    return b + a\n"),
        ("tools/cli.py", "import sys\n\n\ndef main():\n    sys.exit(0)\n"),
        (
            "other/test_extra.py",
            PROJECT["other/test_extra.py"] + "\n\ndef test_more():\n"
            "    result = 2\n    assert result == 2\n",
        ),
        (
            "other/test_extra.py",
            PROJECT["other/test_extra.py"] + "\n\n"
            "@pytest.mark.skipif(sys.platform == 'win32', reason='posix only')\ndef test_posix():\n"
            "    pass\n",
        ),
        ("pyproject.toml", PROJECT["pyproject.toml"].replace("[]", '["requests"]')),
        ("setup.cfg", PROJECT["setup.cfg"].replace("name = demo", "name = renamed")),
        ("package.json", PACKAGE_JSON.replace('"tsc"', '"tsc -b"') + "\n"),
    ],
)
def test_ordinary_edits_raise_nothing_and_survive_restore(
    frozen: FrozenTests, lease: Path, rel: str, text: str
) -> None:
    _write(lease, rel, text)
    assert frozen.inspect(lease).findings == ()
    assert frozen.restore(lease).findings == ()
    assert (lease / rel).read_bytes() == text.encode("utf-8")


@pytest.mark.parametrize(
    ("rel", "text"),
    [
        ("conftest.py", "collect_ignore = ['tests']\n"),
        ("pytest.ini", "[pytest]\ntestpaths = other\n"),
        ("tox.ini", "[tox]\nenvlist = py311\n"),
        ("setup.cfg", PROJECT["setup.cfg"].replace("-q", "-k nothing")),
        ("pyproject.toml", PROJECT["pyproject.toml"].replace('"-q"', '"-k nothing"')),
        ("package.json", PACKAGE_JSON.replace('"jest"', '"exit 0"') + "\n"),
        ("jest.config.js", "module.exports = { testPathIgnorePatterns: ['.'] };\n"),
        ("vitest.config.ts", "export default { test: { exclude: ['**'] } };\n"),
    ],
)
def test_test_configuration_changes_are_flagged_and_restored(
    frozen: FrozenTests, base: Path, lease: Path, rel: str, text: str
) -> None:
    _write(lease, rel, text)
    report = frozen.inspect(lease)
    _only(report, CONFIG_OVERRIDE, rel)
    assert not report.tampered
    assert frozen.restore(lease) == report
    assert (lease / rel).read_bytes() == (base / rel).read_bytes()


def test_added_and_deleted_test_configuration_is_flagged_and_undone(
    frozen: FrozenTests, base: Path, lease: Path
) -> None:
    _write(lease, "other/conftest.py", "import pytest\n")
    (lease / "pytest.ini").unlink()
    report = frozen.restore(lease)
    assert [(f.kind, f.path, f.evidence) for f in report.findings] == [
        (CONFIG_OVERRIDE, "other/conftest.py", "test configuration added: import pytest"),
        (CONFIG_OVERRIDE, "pytest.ini", "test configuration deleted"),
    ]
    assert not (lease / "other/conftest.py").exists()
    assert (lease / "pytest.ini").read_bytes() == (base / "pytest.ini").read_bytes()


def test_inspect_never_writes(frozen: FrozenTests, lease: Path) -> None:
    _write(lease, "tests/test_calc.py", "def test_add():\n    assert True\n")
    _write(lease, "tests/test_added.py", "x = 1\n")
    _write(lease, "pytest.ini", "[pytest]\n")
    before = fingerprint(lease, exclude=())
    frozen.inspect(lease)
    assert fingerprint(lease, exclude=()) == before


def test_restore_puts_back_frozen_bytes_and_removes_added_files(
    frozen: FrozenTests, base: Path, lease: Path
) -> None:
    calc = _write(lease, "tests/test_calc.py", "def test_add():\n    assert True\n")
    os.chmod(calc, stat.S_IREAD)
    (lease / "tests/test_other.py").unlink()
    _write(lease, "tests/test_added.py", "def test_added():\n    pass\n")
    _write(lease, "tests/deep/er/test_deeper.py", "x = 1\n")
    _write(lease, "pytest.ini", "[pytest]\ntestpaths = nowhere\n")
    _write(lease, "tests/__pycache__/test_calc.cpython-312-pytest-8.4.2.pyc", b"stale")
    _write(lease, "tests/__pycache__/test_calc.cpython-312.pyc", b"stale")
    _write(lease, "tests/__pycache__/unrelated.cpython-312.pyc", b"keep")
    expected = frozen.inspect(lease)

    report = frozen.restore(lease)

    assert report == expected
    assert {f.kind for f in report.definitive} == {MODIFIED_TEST, DELETED_TEST, ADDED_TEST_FILE}
    for rel in ("tests/test_calc.py", "tests/test_other.py", "pytest.ini"):
        assert (lease / rel).read_bytes() == (base / rel).read_bytes(), rel
    assert not (lease / "tests/test_added.py").exists()
    assert not (lease / "tests/deep/er/test_deeper.py").exists()
    assert sorted(p.name for p in (lease / "tests/__pycache__").iterdir()) == [
        "unrelated.cpython-312.pyc"
    ]
    assert frozen.inspect(lease).findings == ()


def test_restore_replaces_a_directory_planted_at_a_frozen_path(
    frozen: FrozenTests, base: Path, lease: Path
) -> None:
    (lease / "tests/test_other.py").unlink()
    _write(lease, "tests/test_other.py/inner.txt", "planted")
    frozen.restore(lease)
    assert (lease / "tests/test_other.py").read_bytes() == (
        base / "tests/test_other.py"
    ).read_bytes()


def test_inspect_requires_a_captured_base(lease: Path) -> None:
    with pytest.raises(ValueError, match="no captured base"):
        FrozenTests(["tests/**"]).inspect(lease)
    with pytest.raises(TypeError):
        FrozenTests("tests/**")
    with pytest.raises(ValueError):
        FrozenTests([])


@pytest.mark.parametrize(
    "line",
    [
        "@pytest.mark.skipif(sys.platform == 'win32', reason='x')",
        "atexit.register(cleanup)",
        "stack.exit()",
        "sys.exit(1)",
        "assert result == 5",
        "assert Truthy(x)",
        "# pytest.skip('commented out')",
        "self.assertTrue(value)",
    ],
)
def test_signal_patterns_ignore_ordinary_lines(line: str) -> None:
    assert signals("tests/test_x.py", f"{line}\n".encode(), test_file=True) == []
