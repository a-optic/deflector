# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""`claude -p` must never see ANTHROPIC_API_KEY.

The operator authenticates Claude Code through claude.ai on a Pro subscription --
a FLAT-RATE credential. An API key is a separate METERED product. Same model, same
output, per-token bill.

ANTHROPIC_API_KEY lives in the deflector's LaunchDaemon environment (added
2026-09-10), and `create_subprocess_exec` inherits the parent environment by
default. Claude Code prefers an env key over a stored OAuth credential, so an
unscrubbed spawn moves every CLI request off the subscription and onto per-token
billing with no error, no log line, and byte-identical behaviour. The only symptom
is the invoice, arriving weeks later.

The test asserts on the ACTUAL spawn kwargs rather than on `_cli_env()`. A correct
helper that nothing calls would pass a helper-only test and still bill.

Offline. Run: .venv/bin/python -m pytest providers/test_claude_cli_env.py -q
"""

import asyncio

import pytest

from providers import claude_cli


class _FakeProc:
    """Enough of an asyncio subprocess to let stream_claude reach its first read."""

    returncode = 0

    def __init__(self):
        self.stdin = self
        self.stdout = self

    def write(self, _data): pass
    async def drain(self): pass
    def close(self): pass
    async def wait(self): return 0
    def kill(self): pass

    def __aiter__(self):                        # stream_claude iterates proc.stdout
        return self

    async def __anext__(self):
        raise StopAsyncIteration                # EOF immediately


@pytest.fixture
def spawn_kwargs(monkeypatch):
    """Capture what stream_claude actually passes to create_subprocess_exec."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-be-inherited")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def drive():
        async for _ in claude_cli.stream_claude(
                {"model": "claude-sonnet-5",
                 "messages": [{"role": "user", "content": "hi"}]},
                "claude-sonnet-5"):
            pass

    asyncio.run(drive())
    assert captured, "create_subprocess_exec was never called"
    return captured


class TestTheKeyIsScrubbed:
    def test_spawn_passes_an_explicit_env(self, spawn_kwargs):
        # Without env=, the child inherits os.environ and the key comes with it.
        assert "env" in spawn_kwargs, "no env= passed; the child inherits the key"

    def test_the_key_is_not_in_it(self, spawn_kwargs):
        # THE test. Mutation-check: drop `env=_cli_env()` from the spawn and this fails.
        assert "ANTHROPIC_API_KEY" not in spawn_kwargs["env"]

    def test_the_rest_of_the_environment_survives(self, spawn_kwargs):
        # env={} would also pass the assertion above, and would break Claude Code
        # outright -- no HOME means no ~/.claude credential, so OAuth fails and the
        # CLI path dies. Scrub one variable, not the environment.
        env = spawn_kwargs["env"]
        assert "PATH" in env
        assert "HOME" in env


class TestTheHelperItself:
    def test_removes_the_key_without_mutating_os_environ(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        import os
        env = claude_cli._cli_env()
        assert "ANTHROPIC_API_KEY" not in env
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-x", "scrub leaked into the process"

    def test_absent_key_is_not_an_error(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert "ANTHROPIC_API_KEY" not in claude_cli._cli_env()
