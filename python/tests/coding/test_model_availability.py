from errorta_council.coding.model_availability import (
    available_route_ids,
    effective_family_allowlist,
    project_availability,
)


def test_family_allowlist_is_hard_intersection() -> None:
    assert effective_family_allowlist({"local", "openai"}, ["openai", "missing"]) == {"openai"}
    projection = project_availability(
        ["local.ollama.qwen:7b", "openai.gpt-5"],
        configured_families={"local", "openai"}, enabled_families={"local"},
        local_models={"qwen:7b"}, ollama_reachable=True,
    )
    assert available_route_ids(projection) == {"local.ollama.qwen:7b"}
    assert projection["openai.gpt-5"].reason == "family_disabled"


def test_cli_unknown_connection_fails_closed() -> None:
    projection = project_availability(
        ["claude_cli.opus"], configured_families={"claude_cli"},
        enabled_families={"claude_cli"}, cli_connected={"claude_cli": None},
    )
    assert projection["claude_cli.opus"].available is False
    assert projection["claude_cli.opus"].reason == "cli_not_verified"
