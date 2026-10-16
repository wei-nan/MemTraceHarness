from __future__ import annotations

from pathlib import Path

from memtrace_harness.adapters import (
    AntigravityCliAdapter,
    ClaudeCliAdapter,
    CodexCliAdapter,
    CursorCliAdapter,
    ModelAdapter,
)
from memtrace_harness.cli_process import CliProcessRunner
from memtrace_harness.config import HarnessConfig
from memtrace_harness.output_contracts import output_schema_path
from memtrace_harness.role_profiles import ModelCandidate, RoleProfile


def build_provider_adapter(
    *,
    provider: str,
    config: HarnessConfig,
    working_directory: Path,
    timeout_seconds: int,
    profile: RoleProfile | None = None,
    candidate: ModelCandidate | None = None,
    adapter_id: str | None = None,
    fallback_index: int = 0,
    cli_version: str | None = None,
    read_directories: tuple[Path, ...] = (),
    write_directories: tuple[Path, ...] = (),
) -> ModelAdapter:
    if candidate is not None and profile is None:
        raise ValueError("A model candidate requires a role profile")
    effective_provider = candidate.provider if candidate else provider
    commands = provider_commands(config)
    source = candidate.source if candidate else profile.source if profile else None
    binding = config.source_binding(effective_provider, source, require_allowed=False)
    effective_directory = working_directory
    if profile and profile.context_policy == "loop-snapshot":
        effective_directory = (config.trace_root / "controller-workspace").resolve()
        effective_directory.mkdir(parents=True, exist_ok=True)
    common = {
        "adapter_id": adapter_id or (profile.profile_id if profile else provider),
        "role": profile.role if profile else "Implementation Agent",
        "executable": binding.command if binding else commands[effective_provider],
        "execution_source": binding,
        "working_directory": effective_directory,
        "trace_root": config.trace_root,
        "timeout_seconds": (
            profile.timeout_seconds if profile and profile.timeout_seconds else timeout_seconds
        ),
        "role_profile_id": profile.profile_id if profile else None,
        "model": candidate.model if candidate else profile.model if profile else None,
        "reasoning_effort": (
            candidate.reasoning_effort
            if candidate
            else profile.reasoning_effort
            if profile
            else None
        ),
        "permission": profile.permission if profile else "workspace-write",
        "context_policy": (
            profile.context_policy if profile else "task-and-targeted-evidence"
        ),
        "cli_version": cli_version,
        "output_schema_path": output_schema_path(profile.profile_id) if profile else None,
        "quota_bucket": binding.quota_bucket if binding else (
            candidate.quota_bucket
            if candidate
            else profile.quota_bucket
            if profile
            else f"{effective_provider}-account"
        ),
        "fallback_index": fallback_index,
        "reply_language": config.reply_language,
        "memtrace_mcp_url": config.memtrace_mcp_url,
        "read_directories": read_directories if (profile is None or profile.permission == "read-only") else (),
        "write_directories": (
            write_directories
            if profile is None or profile.permission == "workspace-write"
            else ()
        ),
    }
    if effective_provider == "claude":
        return ClaudeCliAdapter(**common)
    if effective_provider == "codex":
        return CodexCliAdapter(**common)
    if effective_provider == "cursor":
        return CursorCliAdapter(**common)
    if effective_provider == "antigravity":
        return AntigravityCliAdapter(
            structured_output=antigravity_stream_json_enabled(
                config, working_directory=working_directory
            ),
            **common,
        )
    raise ValueError(f"Unknown provider: {effective_provider}")


def build_role_adapters(
    *,
    profiles: dict[str, RoleProfile],
    config: HarnessConfig,
    working_directory: Path,
    timeout_seconds: int,
) -> dict[str, ModelAdapter]:
    return {
        profile_id: candidates[0]
        for profile_id, candidates in build_role_adapter_candidates(
            profiles=profiles,
            config=config,
            working_directory=working_directory,
            timeout_seconds=timeout_seconds,
        ).items()
    }


def build_role_adapter_candidates(
    *,
    profiles: dict[str, RoleProfile],
    config: HarnessConfig,
    working_directory: Path,
    timeout_seconds: int,
    read_directories: tuple[Path, ...] = (),
    write_directories: tuple[Path, ...] = (),
) -> dict[str, list[ModelAdapter]]:
    versions = probe_cli_versions(
        config=config,
        working_directory=working_directory,
        providers={
            candidate.provider
            for profile in profiles.values()
            for candidate in profile.candidates
        },
    )
    return {
        profile_id: [
            build_provider_adapter(
                provider=candidate.provider,
                profile=profile,
                candidate=candidate,
                adapter_id=(
                    profile_id if index == 0 else f"{profile_id}-fallback-{index}"
                ),
                fallback_index=index,
                config=config,
                working_directory=working_directory,
                timeout_seconds=timeout_seconds,
                cli_version=versions.get(candidate.provider) if not candidate.source else None,
                read_directories=read_directories,
                write_directories=write_directories,
            )
            for index, candidate in enumerate(profile.candidates)
        ]
        for profile_id, profile in profiles.items()
    }


def probe_cli_versions(
    *,
    config: HarnessConfig,
    working_directory: Path,
    providers: set[str],
) -> dict[str, str | None]:
    commands = provider_commands(config)
    runner = CliProcessRunner()
    versions: dict[str, str | None] = {}
    for provider in providers:
        result = runner.probe(commands[provider], cwd=working_directory)
        output = (result.stdout or result.stderr).strip().splitlines()
        versions[provider] = output[0] if result.return_code == 0 and output else None
    return versions


def provider_commands(config: HarnessConfig) -> dict[str, str]:
    return {
        "claude": config.claude_command,
        "codex": config.codex_command,
        "antigravity": config.antigravity_command,
        "cursor": config.cursor_command,
    }


def antigravity_stream_json_enabled(
    config: HarnessConfig, *, working_directory: Path
) -> bool:
    if config.antigravity_output_mode == "stream-json":
        return True
    if config.antigravity_output_mode == "text":
        return False
    result = CliProcessRunner().run(
        [config.antigravity_command, "--help"],
        cwd=working_directory,
        timeout_seconds=10,
    )
    help_text = f"{result.stdout}\n{result.stderr}"
    return not result.unavailable and "--output-format" in help_text
