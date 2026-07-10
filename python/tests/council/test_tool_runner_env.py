"""F043 runner environment allowlist tests."""
from __future__ import annotations

from errorta_tools.runner import EnvGrant, build_runner_env, is_secret_env_name


def test_runner_env_copies_only_small_allowlist_and_strips_common_secrets() -> None:
    source = {
        "PATH": "/usr/bin",
        "HOME": "/Users/tester",
        "LANG": "en_US.UTF-8",
        "OPENAI_API_KEY": "sk-openai",
        "ANTHROPIC_API_KEY": "sk-anthropic",
        "GITHUB_TOKEN": "ghp-token",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "NORMAL_UNLISTED": "nope",
    }

    env = build_runner_env(
        source_env=source,
        allowlist=("OPENAI_API_KEY", "GITHUB_TOKEN", "NORMAL_UNLISTED"),
    )

    assert env.values == {
        "PATH": "/usr/bin",
        "HOME": "/Users/tester",
        "LANG": "en_US.UTF-8",
        "NORMAL_UNLISTED": "nope",
    }
    assert "OPENAI_API_KEY" not in env.values
    assert "GITHUB_TOKEN" not in env.values
    assert "AWS_SECRET_ACCESS_KEY" not in env.values
    assert set(env.stripped_names) == {"OPENAI_API_KEY", "GITHUB_TOKEN"}


def test_explicit_env_grants_are_named_but_values_stay_out_of_safe_projection() -> None:
    env = build_runner_env(
        source_env={"PATH": "/bin", "OPENAI_API_KEY": "ambient-secret"},
        explicit_env=(EnvGrant(name="OPENAI_API_KEY", value="explicit-secret"),),
    )

    assert env.values["OPENAI_API_KEY"] == "explicit-secret"
    projection = env.safe_projection()
    assert "OPENAI_API_KEY" in projection["explicit_names"]
    assert "explicit-secret" not in str(projection)
    assert env.redaction_values == {"OPENAI_API_KEY": "explicit-secret"}


def test_secret_name_classifier_catches_common_secret_shapes() -> None:
    assert is_secret_env_name("OPENAI_API_KEY")
    assert is_secret_env_name("GITHUB_TOKEN")
    assert is_secret_env_name("AWS_SECRET_ACCESS_KEY")
    assert is_secret_env_name("DATABASE_PASSWORD")
    assert not is_secret_env_name("PATH")
    assert not is_secret_env_name("REQUESTS_CA_BUNDLE")
