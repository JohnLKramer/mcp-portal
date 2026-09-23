import json
from pathlib import Path

from mcp_portal.cli import main

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "openapi"


def _new_config_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(f"""
version: "1"
mode: introspect-safe
server:
  name: billing-sidecar
  transport: stdio
upstreams:
  billing:
    introspection:
      openapi:
        file: "{(FIXTURE_DIR / "billing-3.1.yaml").as_posix()}"
""")
    return path


def test_configure_on_a_fresh_config_writes_operations_and_policy(tmp_path: Path, monkeypatch):
    config_path = _new_config_yaml(tmp_path)
    policy_path = tmp_path / "policy.yaml"

    # billing-3.1.yaml has 4 exposable operations (list_invoices,
    # create_invoice, get_invoice, cancel_invoice) all tagged 'billing'
    # (get_invoice_audit is x-mcp-exclude'd, list_invoices_legacy is
    # deprecated-by-default). run_survey asks a single group-level
    # "expose all 4 operation(s) in 'billing'?" confirm; accepting it takes
    # the accept-all path, which never descends into per-operation prompts.
    # Then write_with_confirmation asks once for the config file and once
    # for the policy file: 3 prompts total.
    answers = iter([True, True, True])
    monkeypatch.setattr("builtins.input", lambda *_args: "y" if next(answers) else "n")

    code = main(
        [
            "configure",
            "--config",
            str(config_path),
            "--policy",
            str(policy_path),
        ]
    )

    assert code == 0
    written = json.loads(json.dumps(_load_yaml(config_path)))
    ids = {op["id"] for op in written["operations"]}
    assert ids == {"list_invoices", "create_invoice", "get_invoice", "cancel_invoice"}
    assert written["policy"]["file"] == str(policy_path)

    policy = _load_yaml(policy_path)
    assert policy["version"] == "1"

    # The written config must load cleanly through the real loader.
    from mcp_portal.config.loader import load_config

    load_config(config_path)


def test_configure_does_not_leave_a_config_referencing_a_missing_policy_file(
    tmp_path: Path, monkeypatch
):
    config_path = _new_config_yaml(tmp_path)
    policy_path = tmp_path / "policy.yaml"

    # Accept the group-level survey, but decline the policy write when asked.
    # Policy is now written before config, so the sequence is: survey
    # accept-all (True), policy write confirm (False -> declined). The config
    # write confirm is never reached because the function returns early.
    answers = iter([True, False])
    monkeypatch.setattr("builtins.input", lambda *_args: "y" if next(answers) else "n")

    code = main(
        [
            "configure",
            "--config",
            str(config_path),
            "--policy",
            str(policy_path),
        ]
    )

    assert code == 0
    assert not policy_path.exists()

    # The config must NOT have been written either, since it would reference
    # a policy file that doesn't exist.
    from mcp_portal.config.loader import load_config as real_load_config

    reloaded = real_load_config(config_path)
    assert reloaded.config.operations == []


def test_configure_preserves_the_introspection_pointer_across_a_write(tmp_path: Path, monkeypatch):
    config_path = _new_config_yaml(tmp_path)
    policy_path = tmp_path / "policy.yaml"

    answers = iter([True, True, True])
    monkeypatch.setattr("builtins.input", lambda *_args: "y" if next(answers) else "n")
    assert main(["configure", "--config", str(config_path), "--policy", str(policy_path)]) == 0

    reloaded = _load_yaml(config_path)
    assert reloaded["upstreams"]["billing"]["introspection"]["openapi"]["file"]


def _load_yaml(path: Path):
    from ruamel.yaml import YAML

    return YAML(typ="safe").load(path.read_text())


def test_configure_requires_a_config_path():
    import pytest

    with pytest.raises(SystemExit):
        main(["configure"])
