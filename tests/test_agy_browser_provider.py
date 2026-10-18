import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.intake import SearchScope
from procurement_bot.providers.agy import AgyCliStatusError
from procurement_bot.providers.agy_browser import AgyBrowserMcpExecutor
from procurement_bot.providers.browser import (
    PreparedBrowserCall,
    StructuredBrowserProvider,
)
from procurement_bot.providers.staged_research import prepare_discovery
from procurement_bot.research import (
    LocalityConstraint,
    LocalityResearchTask,
    ResearchPurpose,
    ResearchSource,
    SourcePlan,
)

MCP_SERVER = {
    "command": "/usr/bin/npx",
    "args": ["-y", "@browsermcp/mcp@1.2.3"],
}


def _safe_settings(server_name: str = "browsermcp") -> dict:
    return {
        "toolPermission": "request-review",
        "enableTerminalSandbox": True,
        "allowNonWorkspaceAccess": False,
        "permissions": {
            "allow": [f"mcp({server_name}/*)"],
            "deny": [
                "command(*)",
                "unsandboxed(*)",
                "read_file(*)",
                "write_file(*)",
                "read_url(*)",
                "execute_url(*)",
            ],
            "ask": [],
        },
    }


def _write_executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/python3\n" + body, encoding="utf-8")
    path.chmod(0o700)
    return path


def _write_profile(home: Path, *, config: dict | None = None) -> None:
    profile = home / ".gemini" / "antigravity-cli"
    config_dir = home / ".gemini" / "config"
    profile.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    (profile / "antigravity-oauth-token").write_text("dedicated-token", encoding="utf-8")
    (profile / "settings.json").write_text(
        json.dumps(_safe_settings()),
        encoding="utf-8",
    )
    (config_dir / "mcp_config.json").write_text(
        json.dumps(config or {"mcpServers": {"browsermcp": MCP_SERVER}}),
        encoding="utf-8",
    )


def _call() -> PreparedBrowserCall:
    return StructuredBrowserProvider(StubExecutor()).prepare_locality(
        LocalityResearchTask(city="Алматы", phrase="первая Алматы")
    )


class StubExecutor:
    async def execute(self, call):  # pragma: no cover - only satisfies the preparation helper
        raise AssertionError(call)


def _executor(tmp_path: Path, executable: Path, **overrides) -> AgyBrowserMcpExecutor:
    home = tmp_path / "dedicated-home"
    if not home.exists():
        _write_profile(home)
    options = {
        "executable": executable,
        "home_dir": home,
        "mcp_server_name": "browsermcp",
        "expected_mcp_server": MCP_SERVER,
        "timeout_seconds": 5,
    }
    options.update(overrides)
    return AgyBrowserMcpExecutor(**options)


def test_300_second_batch_timeout_is_supported(tmp_path):
    executor = _executor(
        tmp_path,
        Path("/bin/true"),
        batch_timeout_seconds=300,
        timeout_seconds=360,
    )
    assert executor.batch_timeout_seconds == 300


def test_constructor_requires_explicit_absolute_paths(tmp_path):
    with pytest.raises(ValueError, match="executable path must be absolute"):
        AgyBrowserMcpExecutor(
            executable=Path("agy"),
            home_dir=tmp_path,
            mcp_server_name="browsermcp",
            expected_mcp_server=MCP_SERVER,
        )
    with pytest.raises(ValueError, match="HOME path must be absolute"):
        AgyBrowserMcpExecutor(
            executable=tmp_path / "agy",
            home_dir=Path("profile"),
            mcp_server_name="browsermcp",
            expected_mcp_server=MCP_SERVER,
        )


@pytest.mark.parametrize(
    ("envelope", "diagnostic_code"),
    [
        (
            {"status": "ERROR", "message": "authentication failed; private=DO_NOT_LEAK"},
            "cli_auth_failed",
        ),
        ({"status": "ERROR", "error_code": "usage_limit"}, "cli_usage_limit"),
        (
            {"status": "ERROR", "error": "account is not eligible in this region"},
            "cli_account_ineligible",
        ),
        (
            {"status": "ERROR", "message": "private arbitrary vendor response"},
            "cli_status_error",
        ),
    ],
)
def test_cli_error_envelopes_use_typed_allowlisted_diagnostics(
    envelope: dict, diagnostic_code: str
) -> None:
    with pytest.raises(AgyCliStatusError) as caught:
        AgyBrowserMcpExecutor._parse_stdout(json.dumps(envelope).encode())

    assert caught.value.diagnostic_code == diagnostic_code
    assert str(caught.value) == f"AGY CLI returned an error ({diagnostic_code})"
    assert "DO_NOT_LEAK" not in str(caught.value)
    assert "private arbitrary vendor response" not in str(caught.value)


def test_success_response_auth_text_is_untrusted_not_an_auth_diagnostic() -> None:
    envelope = {
        "status": "SUCCESS",
        "response": "authentication failed; this is untrusted model text",
        "structured_output": {
            "result_json": json.dumps({"observation": "authentication failed"})
        },
    }

    result = AgyBrowserMcpExecutor._parse_stdout(json.dumps(envelope).encode())

    assert result == {"observation": "authentication failed"}


def test_bounded_search_prompt_stops_at_batch_cap(tmp_path):
    plan = SourcePlan(
        item_index=0,
        source=ResearchSource.TWO_GIS,
        purpose=ResearchPurpose.BUSINESS_DISCOVERY,
        query_terms=["сантехника"],
        rendered_query="сантехника Алматы",
        locality=LocalityConstraint(city="Алматы", scope=SearchScope.CITYWIDE),
        minimum_unique_candidates=20,
        result_ttl_hours=24,
        empty_page_limit=1,
    ).model_copy(update={"batch_candidate_limit": 2})
    call = StructuredBrowserProvider(StubExecutor()).prepare_supplier_search(plan)
    prompt = _executor(tmp_path, Path("/bin/true"))._build_prompt(call)

    assert "at most 2 distinct candidate card(s)" in prompt
    assert "Do not browse further to reach the overall minimum_unique_candidates target" in prompt
    assert "Do not start another batch or search here" in prompt
    assert "return that exact HTTPS URL as next_cursor" in prompt
    assert "stop once the requested minimum_unique_candidates are found" not in prompt
    assert (
        "Do not continue to another batch or search until the overall target is met"
        not in prompt
    )


def test_search_cursor_prompt_continues_from_saved_page(tmp_path):
    plan = SourcePlan(
        item_index=0,
        source=ResearchSource.TWO_GIS,
        purpose=ResearchPurpose.BUSINESS_DISCOVERY,
        query_terms=["сантехника"],
        rendered_query="сантехника Алматы",
        locality=LocalityConstraint(city="Алматы", scope=SearchScope.CITYWIDE),
        minimum_unique_candidates=20,
        result_ttl_hours=24,
        empty_page_limit=1,
    ).model_copy(update={
        "batch_candidate_limit": 2,
        "search_cursor": "https://2gis.kz/almaty/search/page/2",
    })
    call = StructuredBrowserProvider(StubExecutor()).prepare_supplier_search(plan)
    prompt = _executor(tmp_path, Path("/bin/true"))._build_prompt(call)

    assert "Continue this source search from the supplied search_cursor URL first" in prompt
    assert "do not restart at the first search page" in prompt
    assert '"search_cursor":"https://2gis.kz/almaty/search/page/2"' in prompt


def test_discovery_prompt_overrides_legacy_page_and_contact_guidance(tmp_path):
    plan = SourcePlan(
        item_index=0,
        source=ResearchSource.TWO_GIS,
        purpose=ResearchPurpose.BUSINESS_DISCOVERY,
        query_terms=["медицинские товары"],
        rendered_query="медицинские товары Алматы",
        locality=LocalityConstraint(city="Алматы", scope=SearchScope.CITYWIDE),
        minimum_unique_candidates=20,
        result_ttl_hours=24,
        empty_page_limit=1,
    ).model_copy(update={
        "batch_candidate_limit": 2,
        "search_cursor": "https://2gis.kz/almaty/search/page/2",
    })
    call = prepare_discovery(plan)
    prompt = _executor(tmp_path, Path("/bin/true"))._build_prompt(call)
    trusted = prompt.split("UNTRUSTED_BROWSER_REQUEST_JSON:\n", maxsplit=1)[0]

    assert "inspect exactly one results page" in prompt
    assert "up to 20 readable card descriptors" in prompt
    assert "there is no minimum card target" in prompt
    assert "Do not open cards" in prompt
    assert "Do not scroll or issue coordinate gestures" in prompt
    assert "Continue this source search from the supplied search_cursor URL first" in prompt
    assert "return that exact HTTPS URL as next_cursor" in prompt
    assert "inspect at most five pages" not in trusted
    assert "minimum_unique_candidates" not in trusted
    assert "click its public Show phone control" not in trusted
    assert "TRUSTED_TARGET_RESULT_SCHEMA_JSON" in prompt
    assert '"cards"' in prompt


@pytest.mark.parametrize("stage", ["read_card", "read_contacts"])
def test_card_stage_prompt_uses_exact_url_and_one_candidate(tmp_path, stage: str):
    plan = SourcePlan(
        item_index=0,
        source=ResearchSource.TWO_GIS,
        purpose=ResearchPurpose.BUSINESS_DISCOVERY,
        query_terms=["медицинские товары"],
        rendered_query="https://2gis.kz/almaty/firm/12345",
        locality=LocalityConstraint(city="Алматы", scope=SearchScope.CITYWIDE),
        minimum_unique_candidates=20,
        result_ttl_hours=24,
        empty_page_limit=1,
    ).model_copy(update={"batch_candidate_limit": 2})
    base = StructuredBrowserProvider(StubExecutor()).prepare_supplier_search(plan)
    payload = json.loads(base.untrusted_request_json)
    payload["execution_stage"] = stage
    request = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    call = base.model_copy(update={
        "untrusted_request_json": request,
        "request_sha256": hashlib.sha256(request.encode()).hexdigest(),
    })
    prompt = _executor(tmp_path, Path("/bin/true"))._build_prompt(call)
    trusted = prompt.split("UNTRUSTED_BROWSER_REQUEST_JSON:\n", maxsplit=1)[0]

    assert f"{stage} stage: open only the exact rendered_query URL" in prompt
    assert "return exactly one candidate for that card" in prompt
    assert "Do not search, browse result pages, or open any other card" in prompt
    assert "inspect at most five pages" not in trusted
    assert "minimum_unique_candidates" not in trusted
    if stage == "read_card":
        assert "Do not click contact controls" in prompt
    else:
        assert "concatenate the digits without spaces" in prompt
        assert "Set contact_discovery_reason precisely" in prompt
        assert "provider_timeout" in prompt


def test_constructor_rejects_mcp_environment_block(tmp_path):
    with pytest.raises(ValueError, match="environment block"):
        AgyBrowserMcpExecutor(
            executable=tmp_path / "agy",
            home_dir=tmp_path,
            mcp_server_name="browsermcp",
            expected_mcp_server={"command": "/bin/true", "env": {"TOKEN": "secret"}},
        )


@pytest.mark.asyncio
async def test_execute_uses_ephemeral_home_and_does_not_inherit_environment(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PROCUREMENT_MUST_NOT_LEAK", "top-secret")
    script = _write_executable(
        tmp_path / "fake-agy",
        """
import json, os, pathlib, sys
prompt = sys.stdin.read()
home = pathlib.Path(os.environ["HOME"])
config = json.loads((home / ".gemini/config/mcp_config.json").read_text())
safe = (
    "PROCUREMENT_MUST_NOT_LEAK" not in os.environ
    and home.name == "home"
    and list(config["mcpServers"]) == ["browsermcp"]
    and "UNTRUSTED_BROWSER_REQUEST_JSON" in prompt
)
result = {"safe": safe, "argv": sys.argv[1:]}
print(json.dumps({"status": "SUCCESS", "structured_output": {"result_json": json.dumps(result)}}))
""",
    )
    executor = _executor(tmp_path, script)

    result = await executor.execute(_call())

    assert result["safe"] is True
    assert "--sandbox" in result["argv"]
    assert "--disable-slash-commands" in result["argv"]
    assert not (tmp_path / "dedicated-home" / ".cache").exists()


@pytest.mark.asyncio
async def test_execute_refuses_missing_profile_before_starting_process(tmp_path):
    marker = tmp_path / "started"
    script = _write_executable(
        tmp_path / "fake-agy",
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
    )
    home = tmp_path / "missing-home"
    executor = AgyBrowserMcpExecutor(
        executable=script,
        home_dir=home,
        mcp_server_name="browsermcp",
        expected_mcp_server=MCP_SERVER,
    )

    with pytest.raises(PermanentProviderError, match="missing"):
        await executor.execute(_call())
    assert not marker.exists()


@pytest.mark.asyncio
async def test_execute_refuses_extra_or_drifted_mcp_server(tmp_path):
    script = _write_executable(tmp_path / "fake-agy", "raise SystemExit(99)\n")
    home = tmp_path / "dedicated-home"
    _write_profile(
        home,
        config={
            "mcpServers": {
                "browsermcp": MCP_SERVER,
                "unexpected": {"command": "/bin/true", "args": []},
            }
        },
    )
    executor = _executor(tmp_path, script)

    with pytest.raises(PermanentProviderError, match="allowlist"):
        await executor.execute(_call())


@pytest.mark.asyncio
async def test_execute_rejects_settings_that_define_another_mcp_path(tmp_path):
    script = _write_executable(tmp_path / "fake-agy", "raise SystemExit(99)\n")
    executor = _executor(tmp_path, script)
    settings = tmp_path / "dedicated-home/.gemini/antigravity-cli/settings.json"
    settings.write_text('{"nested":{"mcp_servers":{}}}', encoding="utf-8")

    with pytest.raises(PermanentProviderError, match="settings.json"):
        await executor.execute(_call())


@pytest.mark.asyncio
async def test_execute_rejects_global_auto_approval_policy(tmp_path):
    script = _write_executable(tmp_path / "fake-agy", "raise SystemExit(99)\n")
    executor = _executor(tmp_path, script)
    settings = tmp_path / "dedicated-home/.gemini/antigravity-cli/settings.json"
    settings.write_text('{"toolPermission":"always-proceed"}', encoding="utf-8")

    with pytest.raises(PermanentProviderError, match="fail-closed"):
        await executor.execute(_call())


@pytest.mark.asyncio
async def test_execute_rejects_non_json_stdout_without_markdown_fallback(tmp_path):
    script = _write_executable(
        tmp_path / "fake-agy",
        'print(\'```json\\n{"status":"SUCCESS"}\\n```\')\n',
    )
    executor = _executor(tmp_path, script)

    with pytest.raises(RetryableProviderError, match="one JSON value"):
        await executor.execute(_call())


@pytest.mark.asyncio
async def test_execute_validates_call_hash_before_starting_process(tmp_path):
    script = _write_executable(tmp_path / "fake-agy", "raise SystemExit(99)\n")
    executor = _executor(tmp_path, script)
    call = _call().model_copy(update={"request_sha256": "0" * 64})

    with pytest.raises(PermanentProviderError, match="request hash"):
        await executor.execute(call)


@pytest.mark.asyncio
async def test_concurrency_is_bounded_by_semaphore(tmp_path, monkeypatch):
    script = _write_executable(tmp_path / "fake-agy", "raise SystemExit(99)\n")
    executor = _executor(tmp_path, script, concurrency=2)
    active = 0
    maximum = 0

    async def invoke(prompt):
        nonlocal active, maximum
        assert prompt
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {}

    monkeypatch.setattr(executor, "_invoke", invoke)
    await asyncio.gather(*(executor.execute(_call()) for _ in range(8)))

    assert maximum == 2


@pytest.mark.asyncio
async def test_timeout_terminates_process_group(tmp_path):
    script = _write_executable(
        tmp_path / "fake-agy",
        "import sys, time\nsys.stdin.read()\ntime.sleep(30)\n",
    )
    executor = _executor(tmp_path, script, timeout_seconds=1)

    with pytest.raises(RetryableProviderError, match="timeout"):
        await executor.execute(_call())


@pytest.mark.asyncio
async def test_attempt_receipt_is_durable_and_contains_only_safe_metadata(tmp_path, monkeypatch):
    executable = _write_executable(tmp_path / "fake-agy", "pass\n")
    diagnostics = tmp_path / "provider-attempts"
    executor = _executor(tmp_path, executable, diagnostics_root=diagnostics)

    async def invoke(prompt):
        assert "UNTRUSTED_BROWSER_REQUEST_JSON" in prompt
        return {"candidates": [], "pages_examined": 1, "access_issue": None}

    monkeypatch.setattr(executor, "_invoke", invoke)
    await executor.execute(_call())

    receipts = list(diagnostics.glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["operation"] == "resolve_locality"
    assert receipt["source"] == "2gis"
    assert receipt["status"] == "succeeded"
    assert receipt["page_count"] == 1
    assert receipt["request_sha256"]
    assert "prompt" not in receipt and "token" not in receipts[0].read_text().lower()
    assert receipts[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_repeated_captcha_uses_cooldown_without_reporting_absence(
    tmp_path, monkeypatch,
):
    executable = _write_executable(tmp_path / "fake-agy", "pass\n")
    executor = _executor(
        tmp_path,
        executable,
        captcha_cooldown_seconds=900,
    )
    calls = 0

    async def invoke(_prompt):
        nonlocal calls
        calls += 1
        return {"candidates": [], "pages_examined": 0, "access_issue": "captcha"}

    monkeypatch.setattr(executor, "_invoke", invoke)
    first = await executor.execute(_call())
    second = await executor.execute(_call())
    third = await executor.execute(_call())

    assert executor.captcha_cooldown_seconds == 900
    assert calls == 2
    assert first["access_issue"] == second["access_issue"] == third["access_issue"] == "captcha"
    assert third["pages_examined"] == 0


@pytest.mark.asyncio
async def test_execute_accepts_only_prepared_browser_call(tmp_path):
    script = _write_executable(tmp_path / "fake-agy", "raise SystemExit(99)\n")
    executor = _executor(tmp_path, script)

    with pytest.raises(TypeError, match="PreparedBrowserCall"):
        await executor.execute({})  # type: ignore[arg-type]
