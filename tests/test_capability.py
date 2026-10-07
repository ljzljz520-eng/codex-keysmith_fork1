"""Capability descriptor, layered config model, and fail-closed install tests."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "codex-instruct.py"
spec = importlib.util.spec_from_file_location("codex_instruct_capability", MODULE_PATH)
codex_instruct = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = codex_instruct
spec.loader.exec_module(codex_instruct)

ci = codex_instruct


def _run(*args):
    arguments = list(map(str, args))
    if not any(a == "--lang" or a.startswith("--lang=") for a in arguments):
        arguments.extend(("--lang", "en"))
    return subprocess.run(
        [sys.executable, str(MODULE_PATH), *arguments],
        text=True,
        capture_output=True,
    )


def _make_codex_dir(tmp_path, name=".codex", config='model = "gpt-5.6"\n'):
    codex_dir = tmp_path / name
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(config, encoding="utf-8")
    return codex_dir


# ── 版本解析 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("codex-cli 0.160.1", (0, 160, 1)),
        ("codex 0.90.0\n", (0, 90, 0)),
        ("rust-v0.10.0-x86_64", (0, 10, 0)),
        ("codex 0.162.0-alpha.1+build.7", (0, 162, 0)),
        ("Codex CLI v0.2.0", (0, 2, 0)),
        ("no version here", None),
        # A date-shaped token still parses structurally; resolution then
        # classifies 2024.x as unknown-newer and fails closed.
        ("2024.10.01 release", (2024, 10, 1)),
    ],
)
def test_parse_codex_version(text, expected):
    parsed = ci.parse_codex_version(text)
    if expected is None:
        assert parsed is None
    else:
        assert parsed.tuple == expected


def test_parse_codex_version_prerelease():
    parsed = ci.parse_codex_version("codex 0.162.0-rc.1")
    assert parsed is not None
    assert parsed.prerelease == "rc.1"
    assert str(parsed) == "0.162.0-rc.1"


@pytest.mark.parametrize(
    "text,ok",
    [
        ("0.160.1", True),
        ("v0.90.0", True),
        ("0.162.0-alpha.1", True),
        ("0.16", False),
        ("v0.16", False),
        ("0.160.1.2", False),
        ("latest", False),
        ("", False),
        ("0.90.0+build", True),
    ],
)
def test_parse_pinned_codex_version(text, ok):
    if ok:
        parsed = ci.parse_pinned_codex_version(text)
        assert parsed.tuple[0] == 0
    else:
        with pytest.raises(ValueError):
            ci.parse_pinned_codex_version(text)


# ── descriptor 区间边界 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "version,descriptor_id,usable_key",
    [
        ((0, 2, 0), "legacy-inline-only", None),
        ((0, 5, 3), "legacy-inline-only", None),
        ((0, 9, 9), "legacy-inline-only", None),
        ((0, 10, 0), "experimental-instructions-file", "experimental_instructions_file"),
        ((0, 50, 0), "experimental-instructions-file", "experimental_instructions_file"),
        ((0, 89, 7), "experimental-instructions-file", "experimental_instructions_file"),
        ((0, 90, 0), "model-instructions-file", "model_instructions_file"),
        ((0, 100, 0), "model-instructions-file", "model_instructions_file"),
        ((0, 140, 0), "model-instructions-file", "model_instructions_file"),
        ((0, 160, 1), "model-instructions-file", "model_instructions_file"),
    ],
)
def test_descriptor_boundaries(version, descriptor_id, usable_key):
    descriptor = ci.descriptor_for_version(version)
    assert descriptor is not None
    assert descriptor.descriptor_id == descriptor_id
    assert descriptor.install_key == usable_key


@pytest.mark.parametrize("version", [(0, 1, 0), (0, 0, 1), (0, 161, 0), (1, 0, 0), (9, 9, 9)])
def test_unknown_versions_have_no_descriptor(version):
    assert ci.descriptor_for_version(version) is None


# ── probe / resolve ─────────────────────────────────────────────────────────


def test_probe_missing_binary_is_unavailable():
    evidence = ci.probe_codex_version("/nonexistent/codex-keysmith-bin")
    assert evidence.status == "unavailable"
    assert evidence.version is None


def test_probe_uses_fake_binary(fake_codex_factory):
    binary = fake_codex_factory("codex-cli 0.160.1")
    evidence = ci.probe_codex_version(str(binary))
    assert evidence.status == "probed"
    assert evidence.version.tuple == (0, 160, 1)
    assert evidence.executable == str(binary)


def test_probe_unparseable_output(fake_codex_factory):
    binary = fake_codex_factory("codex: versionless binary")
    evidence = ci.probe_codex_version(str(binary))
    assert evidence.status == "unparseable"
    assert evidence.version is None


def test_resolve_reasons(fake_codex_factory):
    cap = ci.resolve_capability(None, "/nonexistent/codex-keysmith-bin")
    assert cap.reason == "unavailable"
    assert cap.usable is False

    cap = ci.resolve_capability("0.5.0", None)
    assert cap.reason == "file-install-unsupported"
    assert cap.usable is False
    assert cap.descriptor.descriptor_id == "legacy-inline-only"

    cap = ci.resolve_capability("0.1.5", None)
    assert cap.reason == "unknown-older"
    assert cap.usable is False

    cap = ci.resolve_capability("0.161.0", None)
    assert cap.reason == "unknown-newer"
    assert cap.usable is False

    binary = fake_codex_factory("weird")
    cap = ci.resolve_capability(None, str(binary))
    assert cap.reason == "unparseable"
    assert cap.usable is False


def test_resolve_usable_generations():
    cap = ci.resolve_capability("0.50.0", None)
    assert cap.usable
    assert cap.install_key == "experimental_instructions_file"
    assert cap.reference_style == "absolute"

    cap = ci.resolve_capability("0.160.1", None)
    assert cap.usable
    assert cap.install_key == "model_instructions_file"
    assert cap.reference_style == "config-relative"


def test_planned_reference_styles(tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    modern = ci.descriptor_for_version((0, 160, 0))
    assert ci.planned_install_reference(codex_dir, "gpt-overlay.md", modern) == (
        "./gpt-overlay.md"
    )
    legacy = ci.descriptor_for_version((0, 50, 0))
    reference = ci.planned_install_reference(codex_dir, "gpt-overlay.md", legacy)
    assert os.path.isabs(reference)
    assert reference == str((codex_dir / "gpt-overlay.md").resolve())


# ── 分层模型：运行配置层 ────────────────────────────────────────────────────


def _runtime_layer(config_text, capability, codex_dir, md="gpt-overlay.md"):
    analysis = ci._analyze_toml_root(
        config_text, target_key=capability.install_key or "model_instructions_file"
    )
    return ci.analyze_runtime_config_layer(codex_dir, analysis, capability, md)


def test_runtime_layer_preserves_unknown_keys_and_tables(tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    config = (
        'model = "gpt-5.6"\n'
        'model_provider = "openai"\n'
        "model_instructions_file = \"./gpt-overlay.md\"\n"
        "[mcp_servers.server_one]\n"
        'command = "x"\n'
    )
    cap = ci.resolve_capability("0.160.1", None)
    layer = _runtime_layer(config, cap, codex_dir)
    assert layer.managed_key == "model_instructions_file"
    assert layer.managed_present is True
    assert layer.managed_value == "./gpt-overlay.md"
    assert layer.planned_reference == "./gpt-overlay.md"
    assert layer.reference_base == "config-dir"
    preserved_names = {item.name for item in layer.preserved_keys}
    assert preserved_names == {"model", "model_provider"}
    table_names = [item.name for item in layer.preserved_tables]
    assert table_names == ["[mcp_servers.server_one]"]
    # Rendering must preserve every unknown field byte-for-byte.
    updated, changed = ci.render_model_instructions(
        config,
        "gpt-overlay.md",
        analysis=ci._analyze_toml_root(config),
    )
    assert changed is False
    assert 'model_provider = "openai"' in updated
    assert "[mcp_servers.server_one]" in updated


def test_runtime_layer_flags_ineffective_keys_per_generation(tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    config = (
        'experimental_instructions_file = "/old/gpt-overlay.md"\n'
        'model_instructions_file = "./gpt-overlay.md"\n'
        'instructions = "inline"\n'
        'developer_instructions = "dev"\n'
    )
    modern = ci.resolve_capability("0.160.1", None)
    layer = _runtime_layer(config, modern, codex_dir)
    ineffective = {item.key for item in layer.ineffective}
    assert ineffective == {"experimental_instructions_file"}
    assert layer.inline_instructions_present
    assert layer.developer_instructions_present

    experimental = ci.resolve_capability("0.50.0", None)
    analysis = ci._analyze_toml_root(config, target_key="experimental_instructions_file")
    layer = ci.analyze_runtime_config_layer(
        codex_dir, analysis, experimental, "gpt-overlay.md"
    )
    assert layer.managed_key == "experimental_instructions_file"
    assert layer.reference_base == "cwd"
    assert {item.key for item in layer.ineffective} == {"model_instructions_file"}


def test_runtime_layer_lists_profile_files(tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "work.config.toml").write_text('model = "x"\n', encoding="utf-8")
    (codex_dir / "notes.txt").write_text("x", encoding="utf-8")
    cap = ci.resolve_capability("0.160.1", None)
    layer = _runtime_layer('model = "x"\n', cap, codex_dir)
    assert layer.profile_files == ("work.config.toml",)


# ── 分层模型：AGENTS.md 指令发现层（独立层） ────────────────────────────────


def test_discovery_layer_independent_from_config_layer(tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    cap = ci.resolve_capability("0.160.1", None)
    layer = ci.analyze_instruction_discovery_layer(codex_dir, cap)
    assert layer.state == "missing"
    assert layer.supported is True

    (codex_dir / "AGENTS.md").write_text("# global rules\n", encoding="utf-8")
    layer = ci.analyze_instruction_discovery_layer(codex_dir, cap)
    assert layer.state == "present-nonempty"
    assert layer.supported is True

    (codex_dir / "AGENTS.md").write_text("  \n", encoding="utf-8")
    layer = ci.analyze_instruction_discovery_layer(codex_dir, cap)
    assert layer.state == "present-empty"

    cap_unknown = ci.resolve_capability("9.9.9", None)
    layer = ci.analyze_instruction_discovery_layer(codex_dir, cap_unknown)
    # Unknown version -> support unverified, but the discovery node is still
    # modeled as its own layer rather than as a config-layer override.
    assert layer.supported is None


# ── fail-closed：未知版本零写入 ─────────────────────────────────────────────


def test_dry_run_blocked_writes_nothing(tmp_path):
    codex_dir = _make_codex_dir(tmp_path)
    before = (codex_dir / "config.toml").read_text(encoding="utf-8")
    result = _run(
        "--codex-dir",
        codex_dir,
        "--dry-run",
        "--codex-bin",
        "/nonexistent/codex-keysmith-bin",
    )
    assert result.returncode == 1
    assert "Compatibility blocked" in result.stdout
    assert "no instruction config key will be written" in result.stdout
    assert (codex_dir / "config.toml").read_text(encoding="utf-8") == before
    assert not (codex_dir / "gpt-overlay.md").exists()
    assert not (codex_dir / ci.MANIFEST_FILENAME).exists()


def test_deploy_blocked_writes_nothing(tmp_path):
    codex_dir = _make_codex_dir(tmp_path)
    before = (codex_dir / "config.toml").read_text(encoding="utf-8")
    result = _run(
        "--codex-dir",
        codex_dir,
        "--yes",
        "--codex-version",
        "9.9.9",
    )
    assert result.returncode == 1
    assert "Compatibility blocked" in result.stdout
    assert "newer than the highest verified" in result.stdout
    assert (codex_dir / "config.toml").read_text(encoding="utf-8") == before
    assert not (codex_dir / "gpt-overlay.md").exists()
    assert not (codex_dir / ci.MANIFEST_FILENAME).exists()


def test_legacy_inline_generation_blocked(tmp_path):
    codex_dir = _make_codex_dir(tmp_path)
    result = _run(
        "--codex-dir", codex_dir, "--dry-run", "--codex-version", "0.5.0"
    )
    assert result.returncode == 1
    assert "no file-based instruction key" in result.stdout
    assert "legacy-inline-only" in result.stdout


def test_invalid_pinned_version_is_usage_error(tmp_path):
    codex_dir = _make_codex_dir(tmp_path)
    result = _run(
        "--codex-dir", codex_dir, "--dry-run", "--codex-version", "0.160"
    )
    assert result.returncode == 2
    assert "X.Y.Z" in (result.stderr + result.stdout)


# ── 现代代际部署 + 预览分层展示 ─────────────────────────────────────────────


def test_modern_deploy_uses_model_key(tmp_path):
    codex_dir = _make_codex_dir(tmp_path)
    dry = _run(
        "--codex-dir",
        codex_dir,
        "--dry-run",
        "--codex-version",
        "0.160.1",
    )
    assert dry.returncode == 0
    assert 'Config entry: model_instructions_file = "./gpt-overlay.md"' in dry.stdout
    assert "Compatibility:" in dry.stdout
    assert "model-instructions-file" in dry.stdout
    assert "Runtime config layer:" in dry.stdout
    assert "Instruction discovery layer:" in dry.stdout
    assert "preserved unmanaged scalar keys: model" in dry.stdout

    result = _run(
        "--codex-dir", codex_dir, "--yes", "--codex-version", "0.160.1"
    )
    assert result.returncode == 0, result.stdout
    config_text = (codex_dir / "config.toml").read_text(encoding="utf-8")
    assert 'model_instructions_file = "./gpt-overlay.md"' in config_text
    assert "experimental_instructions_file" not in config_text

    manifest = json.loads((codex_dir / ci.MANIFEST_FILENAME).read_text())
    assert manifest["config"]["key"] == "model_instructions_file"
    assert manifest["config"]["reference"] == "./gpt-overlay.md"
    assert manifest["capability"]["descriptor_id"] == "model-instructions-file"
    assert manifest["capability"]["codex_version"] == "0.160.1"
    assert manifest["capability"]["evidence_status"] == "pinned"
    assert (
        manifest["capability"]["descriptor_schema_version"]
        == ci.CAPABILITY_DESCRIPTOR_SCHEMA_VERSION
    )


def test_unknown_fields_preserved_through_modern_deploy(tmp_path):
    config = (
        'model = "gpt-5.6"\n'
        'model_provider = "custom"\n'
        "[mcp_servers.local]\n"
        'command = "run-local"\n'
    )
    codex_dir = _make_codex_dir(tmp_path, config=config)
    result = _run(
        "--codex-dir", codex_dir, "--yes", "--codex-version", "0.160.1"
    )
    assert result.returncode == 0, result.stdout
    updated = (codex_dir / "config.toml").read_text(encoding="utf-8")
    assert 'model_provider = "custom"' in updated
    assert "[mcp_servers.local]" in updated
    assert 'command = "run-local"' in updated


# ── experimental 代际部署：绝对路径 + experimental 键 ───────────────────────


def test_experimental_deploy_uses_absolute_reference(tmp_path):
    codex_dir = _make_codex_dir(tmp_path)
    expected_ref = str((codex_dir / "gpt-overlay.md").resolve())
    dry = _run(
        "--codex-dir", codex_dir, "--dry-run", "--codex-version", "0.50.0"
    )
    assert dry.returncode == 0
    assert (
        f"Config entry: experimental_instructions_file = "
        f'"{expected_ref}"'
    ) in dry.stdout
    assert "experimental-instructions-file" in dry.stdout
    assert "launch working directory" in dry.stdout

    result = _run(
        "--codex-dir", codex_dir, "--yes", "--codex-version", "0.50.0"
    )
    assert result.returncode == 0, result.stdout
    config_text = (codex_dir / "config.toml").read_text(encoding="utf-8")
    assert f'experimental_instructions_file = "{expected_ref}"' in config_text
    assert "model_instructions_file" not in config_text

    manifest = json.loads((codex_dir / ci.MANIFEST_FILENAME).read_text())
    assert manifest["config"]["key"] == "experimental_instructions_file"
    assert manifest["config"]["reference"] == expected_ref
    assert manifest["capability"]["descriptor_id"] == "experimental-instructions-file"
    assert manifest["capability"]["codex_version"] == "0.50.0"


def test_experimental_manifest_uninstall_restores_config(tmp_path):
    codex_dir = _make_codex_dir(tmp_path)
    original = (codex_dir / "config.toml").read_text(encoding="utf-8")
    deploy = _run(
        "--codex-dir", codex_dir, "--yes", "--codex-version", "0.50.0"
    )
    assert deploy.returncode == 0, deploy.stdout
    uninstall = _run("--codex-dir", codex_dir, "--uninstall", "--yes")
    assert uninstall.returncode == 0, uninstall.stdout
    assert (codex_dir / "config.toml").read_text(encoding="utf-8") == original
    assert not (codex_dir / "gpt-overlay.md").exists()
    assert not (codex_dir / ci.MANIFEST_FILENAME).exists()


@pytest.mark.parametrize(
    "first_version,second_version,first_key,second_key",
    [
        ("0.160.1", "0.50.0", "model_instructions_file", "experimental_instructions_file"),
        ("0.50.0", "0.160.1", "experimental_instructions_file", "model_instructions_file"),
    ],
)
def test_deploy_blocks_cross_generation_redeploy(
    tmp_path, first_version, second_version, first_key, second_key
):
    codex_dir = _make_codex_dir(tmp_path)
    first = _run(
        "--codex-dir", codex_dir, "--yes", "--codex-version", first_version
    )
    assert first.returncode == 0, first.stdout
    config_path = codex_dir / "config.toml"
    deployed_config = config_path.read_text(encoding="utf-8")
    assert first_key in deployed_config
    assert second_key not in deployed_config

    dry = _run(
        "--codex-dir", codex_dir, "--dry-run", "--codex-version", second_version
    )
    assert dry.returncode == 1
    assert "deploy across generations" in dry.stdout
    # Preview blocker means the on-disk state stays byte-identical.
    assert config_path.read_text(encoding="utf-8") == deployed_config

    second = _run(
        "--codex-dir", codex_dir, "--yes", "--codex-version", second_version
    )
    assert second.returncode == 1
    assert "deploy across generations" in second.stdout
    assert config_path.read_text(encoding="utf-8") == deployed_config

    # The original generation is still owned by the manifest and uninstalls
    # cleanly, leaving no stale key behind.
    uninstall = _run("--codex-dir", codex_dir, "--uninstall", "--yes")
    assert uninstall.returncode == 0, uninstall.stdout
    restored = config_path.read_text(encoding="utf-8")
    assert first_key not in restored
    assert second_key not in restored
    assert restored == 'model = "gpt-5.6"\n'


def test_redeploy_same_generation_is_allowed(tmp_path):
    codex_dir = _make_codex_dir(tmp_path)
    first = _run(
        "--codex-dir", codex_dir, "--yes", "--codex-version", "0.160.1"
    )
    assert first.returncode == 0, first.stdout
    second = _run(
        "--codex-dir", codex_dir, "--yes", "--codex-version", "0.160.1"
    )
    assert second.returncode == 0, second.stdout
    config_text = (codex_dir / "config.toml").read_text(encoding="utf-8")
    assert 'model_instructions_file = "./gpt-overlay.md"' in config_text
    assert "experimental_instructions_file" not in config_text


def test_reactivate_blocks_cross_generation(tmp_path):
    codex_dir = _make_codex_dir(tmp_path)
    deploy = _run(
        "--codex-dir", codex_dir, "--yes", "--codex-version", "0.50.0"
    )
    assert deploy.returncode == 0, deploy.stdout
    # Simulate CCSwitch switching the experimental key away (inactive).
    config_path = codex_dir / "config.toml"
    config_path.write_text('model = "gpt-5.6"\n', encoding="utf-8")

    blocked = _run(
        "--codex-dir",
        codex_dir,
        "--reactivate",
        "--yes",
        "--codex-version",
        "0.160.1",
    )
    assert blocked.returncode == 1
    assert "different Codex generation" in blocked.stdout
    assert config_path.read_text(encoding="utf-8") == 'model = "gpt-5.6"\n'

    same_generation = _run(
        "--codex-dir",
        codex_dir,
        "--reactivate",
        "--yes",
        "--codex-version",
        "0.50.0",
    )
    assert same_generation.returncode == 0, same_generation.stdout
    expected_ref = str((codex_dir / "gpt-overlay.md").resolve())
    assert (
        f'experimental_instructions_file = "{expected_ref}"'
        in config_path.read_text(encoding="utf-8")
    )


def test_reactivate_without_evidence_is_blocked(tmp_path):
    codex_dir = _make_codex_dir(tmp_path)
    deploy = _run(
        "--codex-dir", codex_dir, "--yes", "--codex-version", "0.160.1"
    )
    assert deploy.returncode == 0, deploy.stdout
    (codex_dir / "config.toml").write_text('model = "gpt-5.6"\n', encoding="utf-8")
    result = _run(
        "--codex-dir",
        codex_dir,
        "--reactivate",
        "--yes",
        "--codex-bin",
        "/nonexistent/codex-keysmith-bin",
    )
    assert result.returncode == 1
    assert "Compatibility blocked" in result.stdout
    assert (codex_dir / "config.toml").read_text(encoding="utf-8") == (
        'model = "gpt-5.6"\n'
    )


# ── status 只读：未知版本不改变退出码 ───────────────────────────────────────


def test_status_unknown_version_stays_read_only_zero(tmp_path):
    codex_dir = _make_codex_dir(tmp_path)
    result = _run(
        "--codex-dir", codex_dir, "--status", "--codex-version", "9.9.9"
    )
    assert result.returncode == 0
    assert "Compatibility:" in result.stdout
    assert "fail-closed" in result.stdout
    assert "Runtime config layer:" in result.stdout
    assert "Instruction discovery layer:" in result.stdout
    assert "Deployability: ready" in result.stdout
    assert "[Done]" in result.stdout


def test_status_shows_discovery_layer_with_agents_md(tmp_path):
    codex_dir = _make_codex_dir(tmp_path)
    (codex_dir / "AGENTS.md").write_text("# house rules\n", encoding="utf-8")
    result = _run(
        "--codex-dir", codex_dir, "--status", "--codex-version", "0.160.1"
    )
    assert result.returncode == 0
    assert "Instruction discovery layer:" in result.stdout
    assert "present and non-empty" in result.stdout
    assert "does not override it" in result.stdout
    # Stable status contract line is preserved for the GUI parser.
    assert "model_instructions_file:" in result.stdout


# ── manifest 往返与校验 ─────────────────────────────────────────────────────


def test_manifest_capability_section_validation():
    def build(**overrides):
        record = {
            "descriptor_id": "model-instructions-file",
            "codex_version": "0.160.1",
            "evidence_status": "probed",
            "descriptor_schema_version": ci.CAPABILITY_DESCRIPTOR_SCHEMA_VERSION,
        }
        record.update(overrides)
        return record

    # Unknown descriptor id.
    with pytest.raises(ValueError):
        ci._validate_manifest(
            _manifest_skeleton(capability=build(descriptor_id="nope"))
        )
    # Version outside descriptor range.
    with pytest.raises(ValueError):
        ci._validate_manifest(
            _manifest_skeleton(capability=build(codex_version="0.50.0"))
        )
    # Coherence: descriptor generation vs config.key.
    with pytest.raises(ValueError):
        ci._validate_manifest(
            _manifest_skeleton(
                capability=build(descriptor_id="experimental-instructions-file"),
                config_key="model_instructions_file",
                config_ref="./gpt-overlay.md",
            )
        )
    # Bad schema version / evidence status.
    with pytest.raises(ValueError):
        ci._validate_manifest(
            _manifest_skeleton(capability=build(descriptor_schema_version=999))
        )
    with pytest.raises(ValueError):
        ci._validate_manifest(
            _manifest_skeleton(capability=build(evidence_status="guessed"))
        )


def test_manifest_old_shape_defaults_to_model_strategy():
    # A capability-less modern manifest (pre-feature shape) must validate and
    # resolve to the model_instructions_file + ./<md.path> defaults.
    manifest = _manifest_skeleton()
    manifest.pop("capability", None)
    validated = ci._validate_manifest(manifest)
    key, reference = ci._manifest_config_strategy(validated)
    assert key == "model_instructions_file"
    assert reference == "./gpt-overlay.md"


def test_manifest_rejects_relative_experimental_reference():
    with pytest.raises(ValueError):
        ci._validate_manifest(
            _manifest_skeleton(
                config_key="experimental_instructions_file",
                config_ref="./gpt-overlay.md",
            )
        )


def test_manifest_accepts_experimental_strategy():
    manifest = _manifest_skeleton(
        config_key="experimental_instructions_file",
        config_ref="/abs/path/gpt-overlay.md",
        capability={
            "descriptor_id": "experimental-instructions-file",
            "codex_version": "0.50.0",
            "evidence_status": "pinned",
            "descriptor_schema_version": ci.CAPABILITY_DESCRIPTOR_SCHEMA_VERSION,
        },
    )
    validated = ci._validate_manifest(manifest)
    assert ci._manifest_config_strategy(validated) == (
        "experimental_instructions_file",
        "/abs/path/gpt-overlay.md",
    )


def _fingerprint(size=1, sha256=None):
    import hashlib

    return {
        "size": size,
        "mtime_ns": 1,
        "sha256": sha256 or hashlib.sha256(b"x").hexdigest(),
    }


def _manifest_skeleton(
    capability=None,
    config_key="model_instructions_file",
    config_ref="./gpt-overlay.md",
):
    fp = _fingerprint()
    data = {
        "schema_version": ci.MANIFEST_SCHEMA_VERSION,
        "tool_version": ci.VERSION,
        "deployment_id": "a" * 32,
        "created_at": "2026-01-01T00:00:00Z",
        "md": {
            "path": "gpt-overlay.md",
            "before": None,
            "after": fp,
            "backup": None,
            "preset": "overlay",
        },
        "config": {
            "path": "config.toml",
            "before": fp,
            "after": fp,
            "changed": False,
            "backup": None,
        },
        "hooks": {
            "isolated": False,
            "active_before": None,
            "disabled_before": None,
            "active_after": None,
            "disabled_after": None,
            "backup": None,
            "previous_disabled_backup": None,
        },
        "legacy": {
            "path": ci.LEGACY_MD_FILENAME,
            "action": "none",
            "before": None,
            "after": None,
            "archive": None,
        },
        "previous_manifest": {"before": None, "backup": None},
    }
    data["config"]["key"] = config_key
    data["config"]["reference"] = config_ref
    if capability is not None:
        data["capability"] = capability
    return data


# ── 探测到的证据进入描述符报告 ─────────────────────────────────────────────


def test_capability_report_mentions_probed_evidence(fake_codex_factory, capsys):
    binary = fake_codex_factory("codex-cli 0.100.2")
    cap = ci.resolve_capability(None, str(binary))
    ci._set_output_language("en")
    ci.print_capability_report(cap)
    out = capsys.readouterr().out
    assert "probed via" in out
    assert "0.100.2" in out
    assert "model_instructions_file is recognized" in out


def test_capability_blocker_message_covers_reasons():
    for version, fragment in (
        ("0.5.0", "inline `instructions` only"),
        ("0.1.0", "older than the oldest verified"),
        ("0.200.0", "newer than the highest verified"),
    ):
        cap = ci.resolve_capability(version, None)
        message = ci.capability_blocker_message(cap)
        assert "Compatibility blocked" in message
        assert fragment in message
    cap = ci.resolve_capability(None, "/nonexistent/codex-keysmith-bin")
    assert "could not be probed" in ci.capability_blocker_message(cap)
