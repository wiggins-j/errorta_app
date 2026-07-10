"""F101-03 — common-ecosystem runtime detection + grounded resolution.

One detector per mainstream ecosystem (Deno, LÖVE, Ruby, PHP, .NET, Gradle),
each producing a real standard run command and grounded on a file that must
exist on the worktree head (grounded-or-refuse — never a guessed command).

Pure/read-only: builds a workspace dir + a ``RuntimeProfileStore`` directly (no
manager, no subprocess).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from errorta_council.coding.runtime import RuntimeProfileStore, detect
from errorta_council.coding.runtime_resolve import (
    LaunchPlan,
    ground_start,
    resolve_launch_plan,
)


def _store(tmp_path: Path) -> RuntimeProfileStore:
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    return RuntimeProfileStore(ledger_dir, threading.Lock())


def _resolve(tmp_path: Path) -> LaunchPlan:
    plan = resolve_launch_plan(tmp_path, "head1", _store(tmp_path), "p")
    assert isinstance(plan, LaunchPlan), plan
    return plan


# --------------------------------------------------------------------------- #
# Deno
# --------------------------------------------------------------------------- #
def test_deno_task_form(tmp_path: Path):
    (tmp_path / "deno.json").write_text(json.dumps({"tasks": {"start": "deno run main.ts"}}))
    props = detect(tmp_path, project_id="p")
    assert props[0].kind == "cli" and props[0].start == ["deno", "task", "start"]
    verified, missing = ground_start(props[0].start, tmp_path)
    assert verified == ["deno.json"] and missing == []


def test_deno_run_entry_form(tmp_path: Path):
    # A deno.json marker (no task) + a conventional entry -> `deno run <entry>`.
    (tmp_path / "deno.json").write_text(json.dumps({"imports": {}}))
    (tmp_path / "main.ts").write_text("console.log('hi')\n")
    props = detect(tmp_path, project_id="p")
    assert props[0].start == ["deno", "run", "-A", "main.ts"]
    # `deno run <entry>` grounds via the generic entrypoint scan (source ext).
    assert _resolve(tmp_path).verified_paths == ["main.ts"]


def test_deno_single_task_fallback(tmp_path: Path):
    # No start/dev/serve, but exactly one task defined -> run it (L1).
    (tmp_path / "deno.json").write_text(json.dumps({"tasks": {"run": "deno run main.ts"}}))
    props = detect(tmp_path, project_id="p")
    assert props[0].start == ["deno", "task", "run"]
    assert _resolve(tmp_path).verified_paths == ["deno.json"]


def test_deno_task_grounding_checks_task_exists(tmp_path: Path):
    # A stored/authored profile naming a task the config doesn't declare is not
    # grounded (parity with npm-script grounding) (L2).
    (tmp_path / "deno.json").write_text(json.dumps({"tasks": {"start": "x"}}))
    verified, missing = ground_start(["deno", "task", "ghost"], tmp_path)
    assert "deno.json" in verified and "deno.json#tasks.ghost" in missing


def test_bare_main_js_is_not_deno(tmp_path: Path):
    # No deno.json marker: a lone main.js must NOT be claimed as a Deno project
    # (it's just as likely browser/Node code — regression guard for the static
    # SPA false-positive).
    (tmp_path / "main.js").write_text("console.log('hi')\n")
    props = detect(tmp_path, project_id="p")
    assert all(p.start[:1] != ["deno"] for p in props)


# --------------------------------------------------------------------------- #
# LÖVE (love2d)
# --------------------------------------------------------------------------- #
def test_love_game_is_desktop(tmp_path: Path):
    (tmp_path / "main.lua").write_text("function love.draw() end\n")
    props = detect(tmp_path, project_id="p")
    assert props[0].kind == "desktop" and props[0].start == ["love", "."]
    assert props[0].demo.get("toolkit") == "love2d"
    assert _resolve(tmp_path).verified_paths == ["main.lua"]


def test_plain_lua_without_love_is_not_a_love_game(tmp_path: Path):
    (tmp_path / "main.lua").write_text("print('just lua')\n")
    props = detect(tmp_path, project_id="p")
    assert all(p.demo.get("toolkit") != "love2d" for p in props)


def test_love_conf_lua_is_enough(tmp_path: Path):
    # A conf.lua alongside main.lua is the strong LÖVE signal (no love. call needed).
    (tmp_path / "main.lua").write_text("-- entry\n")
    (tmp_path / "conf.lua").write_text("function love.conf(t) end\n")
    props = detect(tmp_path, project_id="p")
    assert props[0].demo.get("toolkit") == "love2d"


def test_love_substring_false_positives_rejected(tmp_path: Path):
    # None of these are LÖVE API usage: an identifier ending in ...love.,
    # prose in a comment, and a string literal (M1 regression).
    for src in ("local x = glove.left\n",
                "-- I love. this code\n",
                'print("I love. lua")\n'):
        (tmp_path / "main.lua").write_text(src)
        props = detect(tmp_path, project_id="p")
        assert all(p.demo.get("toolkit") != "love2d" for p in props), src


def test_love_grounding_absent(tmp_path: Path):
    verified, missing = ground_start(["love", "."], tmp_path)
    assert verified == [] and missing == ["main.lua"]


# --------------------------------------------------------------------------- #
# Ruby
# --------------------------------------------------------------------------- #
def test_ruby_rails_is_web(tmp_path: Path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "rails").write_text("#!/usr/bin/env ruby\n")
    (tmp_path / "Gemfile").write_text("gem 'rails'\n")
    props = detect(tmp_path, project_id="p")
    assert props[0].kind == "web"
    assert props[0].start[:2] == ["bin/rails", "server"]
    assert props[0].setup == [["bundle", "install"]]
    plan = _resolve(tmp_path)
    assert plan.modality == "server" and plan.verified_paths == ["bin/rails"]


def test_ruby_rack_is_web(tmp_path: Path):
    (tmp_path / "config.ru").write_text("run ->(env){[200,{},['ok']]}\n")
    props = detect(tmp_path, project_id="p")
    assert props[0].kind == "web" and props[0].start[0] == "rackup"
    assert _resolve(tmp_path).verified_paths == ["config.ru"]


def test_ruby_script_is_cli(tmp_path: Path):
    (tmp_path / "main.rb").write_text("puts 'hi'\n")
    props = detect(tmp_path, project_id="p")
    assert props[0].kind == "cli" and props[0].start == ["ruby", "main.rb"]
    assert _resolve(tmp_path).verified_paths == ["main.rb"]


# --------------------------------------------------------------------------- #
# PHP
# --------------------------------------------------------------------------- #
def test_php_plain_site_is_web(tmp_path: Path):
    (tmp_path / "index.php").write_text("<?php echo 'hi';\n")
    props = detect(tmp_path, project_id="p")
    assert props[0].kind == "web"
    assert props[0].start == ["php", "-S", "127.0.0.1:{port}"]
    assert _resolve(tmp_path).verified_paths == ["index.php"]


def test_php_laravel_is_web_via_artisan(tmp_path: Path):
    (tmp_path / "artisan").write_text("#!/usr/bin/env php\n")
    (tmp_path / "composer.json").write_text(json.dumps({"require": {"laravel/framework": "^11"}}))
    props = detect(tmp_path, project_id="p")
    assert props[0].start[:3] == ["php", "artisan", "serve"]
    assert props[0].setup == [["composer", "install"]]
    assert _resolve(tmp_path).verified_paths == ["artisan"]


def test_php_grounding_absent(tmp_path: Path):
    verified, missing = ground_start(["php", "-S", "127.0.0.1:{port}"], tmp_path)
    assert verified == [] and missing == ["index.php"]


# --------------------------------------------------------------------------- #
# .NET
# --------------------------------------------------------------------------- #
def test_dotnet_csproj_is_cli(tmp_path: Path):
    (tmp_path / "App.csproj").write_text("<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n")
    props = detect(tmp_path, project_id="p")
    assert props[0].kind == "cli"
    assert props[0].start == ["dotnet", "run", "--project", "App.csproj"]
    assert _resolve(tmp_path).verified_paths == ["App.csproj"]


def test_dotnet_solution_only_is_refused(tmp_path: Path):
    # A bare .sln has no single project to run: `dotnet run` would fail at spawn,
    # so refuse rather than propose it (M2).
    (tmp_path / "App.sln").write_text("Microsoft Visual Studio Solution File\n")
    props = detect(tmp_path, project_id="p")
    assert all(p.start[:1] != ["dotnet"] for p in props)


def test_dotnet_multiple_projects_is_refused(tmp_path: Path):
    (tmp_path / "A.csproj").write_text("<Project/>\n")
    (tmp_path / "B.csproj").write_text("<Project/>\n")
    props = detect(tmp_path, project_id="p")
    assert all(p.start[:1] != ["dotnet"] for p in props)


def test_dotnet_grounding_absent(tmp_path: Path):
    verified, missing = ground_start(["dotnet", "run"], tmp_path)
    assert verified == [] and missing == ["a .csproj / .fsproj / .sln"]


def test_dotnet_grounding_targets_the_named_project(tmp_path: Path):
    (tmp_path / "App.csproj").write_text("<Project/>\n")
    verified, missing = ground_start(
        ["dotnet", "run", "--project", "App.csproj"], tmp_path)
    assert verified == ["App.csproj"] and missing == []
    # A --project target that isn't on disk is not grounded.
    v2, m2 = ground_start(["dotnet", "run", "--project", "Ghost.csproj"], tmp_path)
    assert v2 == [] and m2 == ["Ghost.csproj"]


# --------------------------------------------------------------------------- #
# Gradle (Java / Kotlin)
# --------------------------------------------------------------------------- #
def test_gradle_application_runs_via_wrapper(tmp_path: Path):
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    (tmp_path / "build.gradle").write_text(
        "plugins { id 'application' }\napplication { mainClass = 'Main' }\n")
    props = detect(tmp_path, project_id="p")
    assert props[0].kind == "cli" and props[0].start == ["./gradlew", "run"]
    assert _resolve(tmp_path).verified_paths == ["./gradlew"]


def test_gradle_kotlin_dsl_bare_application_accessor(tmp_path: Path):
    # The idiomatic Kotlin-DSL form: `plugins { application }` (no quotes, no
    # mainClass in this file). Must still detect (H1 regression).
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    (tmp_path / "build.gradle.kts").write_text(
        "plugins {\n    kotlin(\"jvm\") version \"1.9.0\"\n    application\n}\n")
    props = detect(tmp_path, project_id="p")
    assert props[0].start == ["./gradlew", "run"]


def test_gradle_without_application_plugin_is_skipped(tmp_path: Path):
    # A library build (no application plugin) has no ``run`` task — don't guess it.
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    (tmp_path / "build.gradle").write_text(
        "plugins { id 'java-library' }\ndependencies { }\n")
    props = detect(tmp_path, project_id="p")
    assert all(p.start != ["./gradlew", "run"] for p in props)
