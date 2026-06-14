"""
execute.py 리팩터링 안전망 테스트.
리팩터링 전후 동작이 동일한지 검증한다.
"""

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import execute as ex


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path):
    """phases/, CLAUDE.md, docs/ 를 갖춘 임시 프로젝트 구조."""
    phases_dir = tmp_path / "phases"
    phases_dir.mkdir()

    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Rules\n- rule one\n- rule two")

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "arch.md").write_text("# Architecture\nSome content")
    (docs_dir / "guide.md").write_text("# Guide\nAnother doc")

    return tmp_path


@pytest.fixture
def phase_dir(tmp_project):
    """step 3개를 가진 phase 디렉토리."""
    d = tmp_project / "phases" / "0-mvp"
    d.mkdir()

    index = {
        "project": "TestProject",
        "phase": "mvp",
        "steps": [
            {"step": 0, "name": "setup", "status": "completed", "summary": "프로젝트 초기화 완료"},
            {"step": 1, "name": "core", "status": "completed", "summary": "핵심 로직 구현"},
            {"step": 2, "name": "ui", "status": "pending"},
        ],
    }
    (d / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
    (d / "step2.md").write_text("# Step 2: UI\n\nUI를 구현하세요.")

    return d


@pytest.fixture
def top_index(tmp_project):
    """phases/index.json (top-level)."""
    top = {
        "phases": [
            {"dir": "0-mvp", "status": "pending"},
            {"dir": "1-polish", "status": "pending"},
        ]
    }
    p = tmp_project / "phases" / "index.json"
    p.write_text(json.dumps(top, indent=2))
    return p


@pytest.fixture
def executor(tmp_project, phase_dir):
    """테스트용 StepExecutor 인스턴스. git 호출은 별도 mock 필요."""
    with patch.object(ex, "ROOT", tmp_project):
        inst = ex.StepExecutor("0-mvp")
    # 내부 경로를 tmp_project 기준으로 재설정
    inst._root = str(tmp_project)
    inst._phases_dir = tmp_project / "phases"
    inst._phase_dir = phase_dir
    inst._phase_dir_name = "0-mvp"
    inst._index_file = phase_dir / "index.json"
    inst._top_index_file = tmp_project / "phases" / "index.json"
    return inst


# ---------------------------------------------------------------------------
# _stamp (= 이전 now_iso)
# ---------------------------------------------------------------------------

class TestStamp:
    def test_returns_kst_timestamp(self, executor):
        result = executor._stamp()
        assert "+0900" in result

    def test_format_is_iso(self, executor):
        result = executor._stamp()
        dt = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert dt.tzinfo is not None

    def test_is_current_time(self, executor):
        before = datetime.now(ex.StepExecutor.TZ).replace(microsecond=0)
        result = executor._stamp()
        after = datetime.now(ex.StepExecutor.TZ).replace(microsecond=0) + timedelta(seconds=1)
        parsed = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert before <= parsed <= after


# ---------------------------------------------------------------------------
# _read_json / _write_json
# ---------------------------------------------------------------------------

class TestJsonHelpers:
    def test_roundtrip(self, tmp_path):
        data = {"key": "값", "nested": [1, 2, 3]}
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, data)
        loaded = ex.StepExecutor._read_json(p)
        assert loaded == data

    def test_save_ensures_ascii_false(self, tmp_path):
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"한글": "테스트"})
        raw = p.read_text()
        assert "한글" in raw
        assert "\\u" not in raw

    def test_save_indented(self, tmp_path):
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"a": 1})
        raw = p.read_text()
        assert "\n" in raw

    def test_load_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ex.StepExecutor._read_json(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# _load_guardrails
# ---------------------------------------------------------------------------

class TestLoadGuardrails:
    def test_loads_claude_md_and_docs(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "# Rules" in result
        assert "rule one" in result
        assert "# Architecture" in result
        assert "# Guide" in result

    def test_sections_separated_by_divider(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "---" in result

    def test_docs_sorted_alphabetically(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        arch_pos = result.index("arch")
        guide_pos = result.index("guide")
        assert arch_pos < guide_pos

    def test_no_claude_md(self, executor, tmp_project):
        (tmp_project / "CLAUDE.md").unlink()
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "CLAUDE.md" not in result
        assert "Architecture" in result

    def test_no_docs_dir(self, executor, tmp_project):
        import shutil
        shutil.rmtree(tmp_project / "docs")
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "Rules" in result
        assert "Architecture" not in result

    def test_empty_project(self, tmp_path):
        with patch.object(ex, "ROOT", tmp_path):
            # executor가 필요 없는 static-like 동작이므로 임시 인스턴스
            phases_dir = tmp_path / "phases" / "dummy"
            phases_dir.mkdir(parents=True)
            idx = {"project": "T", "phase": "t", "steps": []}
            (phases_dir / "index.json").write_text(json.dumps(idx))
            inst = ex.StepExecutor.__new__(ex.StepExecutor)
            result = inst._load_guardrails()
        assert result == ""


# ---------------------------------------------------------------------------
# _build_step_context
# ---------------------------------------------------------------------------

class TestBuildStepContext:
    def test_includes_completed_with_summary(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert "Step 0 (setup): 프로젝트 초기화 완료" in result
        assert "Step 1 (core): 핵심 로직 구현" in result

    def test_excludes_pending(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert "ui" not in result

    def test_excludes_completed_without_summary(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        del index["steps"][0]["summary"]
        result = ex.StepExecutor._build_step_context(index)
        assert "setup" not in result
        assert "core" in result

    def test_empty_when_no_completed(self):
        index = {"steps": [{"step": 0, "name": "a", "status": "pending"}]}
        result = ex.StepExecutor._build_step_context(index)
        assert result == ""

    def test_has_header(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert result.startswith("## 이전 Step 산출물")


# ---------------------------------------------------------------------------
# _build_preamble
# ---------------------------------------------------------------------------

class TestBuildPreamble:
    def test_includes_project_name(self, executor):
        result = executor._build_preamble("", "")
        assert "TestProject" in result

    def test_includes_guardrails(self, executor):
        result = executor._build_preamble("GUARD_CONTENT", "")
        assert "GUARD_CONTENT" in result

    def test_includes_step_context(self, executor):
        ctx = "## 이전 Step 산출물\n\n- Step 0: done"
        result = executor._build_preamble("", ctx)
        assert "이전 Step 산출물" in result

    def test_includes_commit_example(self, executor):
        result = executor._build_preamble("", "")
        assert "feat(mvp):" in result

    def test_includes_rules(self, executor):
        result = executor._build_preamble("", "")
        assert "작업 규칙" in result
        assert "AC" in result

    def test_no_retry_section_by_default(self, executor):
        result = executor._build_preamble("", "")
        assert "이전 시도 실패" not in result

    def test_retry_section_with_prev_error(self, executor):
        result = executor._build_preamble("", "", prev_error="타입 에러 발생")
        assert "이전 시도 실패" in result
        assert "타입 에러 발생" in result

    def test_includes_max_retries(self, executor):
        result = executor._build_preamble("", "")
        assert str(ex.StepExecutor.MAX_RETRIES) in result

    def test_includes_index_path(self, executor):
        result = executor._build_preamble("", "")
        assert "/phases/0-mvp/index.json" in result


# ---------------------------------------------------------------------------
# _update_top_index
# ---------------------------------------------------------------------------

class TestUpdateTopIndex:
    def test_completed(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text())
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "completed"
        assert "completed_at" in mvp

    def test_error(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("error")
        data = json.loads(top_index.read_text())
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "error"
        assert "failed_at" in mvp

    def test_blocked(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("blocked")
        data = json.loads(top_index.read_text())
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "blocked"
        assert "blocked_at" in mvp

    def test_other_phases_unchanged(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text())
        polish = next(p for p in data["phases"] if p["dir"] == "1-polish")
        assert polish["status"] == "pending"

    def test_nonexistent_dir_is_noop(self, executor, top_index):
        executor._top_index_file = top_index
        executor._phase_dir_name = "no-such-dir"
        original = json.loads(top_index.read_text())
        executor._update_top_index("completed")
        after = json.loads(top_index.read_text())
        for p_before, p_after in zip(original["phases"], after["phases"]):
            assert p_before["status"] == p_after["status"]

    def test_no_top_index_file(self, executor, tmp_path):
        executor._top_index_file = tmp_path / "nonexistent.json"
        executor._update_top_index("completed")  # should not raise


# ---------------------------------------------------------------------------
# _checkout_branch (mocked)
# ---------------------------------------------------------------------------

class TestCheckoutBranch:
    def _mock_git(self, executor, responses):
        call_idx = {"i": 0}
        def fake_git(*args):
            idx = call_idx["i"]
            call_idx["i"] += 1
            if idx < len(responses):
                return responses[idx]
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

    def test_already_on_branch(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="feat-mvp\n", stderr=""),
        ])
        executor._checkout_branch()  # should return without checkout

    def test_branch_exists_checkout(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()

    def test_branch_not_exists_create(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="not found"),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()

    def test_checkout_fails_exits(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="dirty tree"),
        ])
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1

    def test_no_git_exits(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=1, stdout="", stderr="not a git repo"),
        ])
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _commit_step (mocked)
# ---------------------------------------------------------------------------

class TestCommitStep:
    def test_two_phase_commit(self, executor):
        calls = []
        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_calls = [c for c in calls if c[0] == "commit"]
        assert len(commit_calls) == 2
        assert "feat(mvp):" in commit_calls[0][2]
        assert "chore(mvp):" in commit_calls[1][2]

    def test_no_code_changes_skips_feat_commit(self, executor):
        call_count = {"diff": 0}
        calls = []
        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                call_count["diff"] += 1
                if call_count["diff"] == 1:
                    return MagicMock(returncode=0)
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_msgs = [c[2] for c in calls if c[0] == "commit"]
        assert len(commit_msgs) == 1
        assert "chore" in commit_msgs[0]


# ---------------------------------------------------------------------------
# _invoke_claude (mocked)
# ---------------------------------------------------------------------------

class TestInvokeClaude:
    def test_invokes_claude_with_correct_args(self, executor):
        mock_result = MagicMock(returncode=0, stdout='{"result": "ok"}', stderr="")
        step = {"step": 2, "name": "ui"}
        preamble = "PREAMBLE\n"

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            output = executor._invoke_claude(step, preamble)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--output-format" in cmd
        assert "PREAMBLE" in cmd[-1]
        assert "UI를 구현하세요" in cmd[-1]

    def test_saves_output_json(self, executor):
        mock_result = MagicMock(returncode=0, stdout='{"ok": true}', stderr="")
        step = {"step": 2, "name": "ui"}

        with patch("subprocess.run", return_value=mock_result):
            executor._invoke_claude(step, "preamble")

        output_file = executor._phase_dir / "step2-output.json"
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert data["step"] == 2
        assert data["name"] == "ui"
        assert data["exitCode"] == 0

    def test_nonexistent_step_file_exits(self, executor):
        step = {"step": 99, "name": "nonexistent"}
        with pytest.raises(SystemExit) as exc_info:
            executor._invoke_claude(step, "preamble")
        assert exc_info.value.code == 1

    def test_timeout_is_1800(self, executor):
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        step = {"step": 2, "name": "ui"}

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            executor._invoke_claude(step, "preamble")

        assert mock_run.call_args[1]["timeout"] == 1800


# ---------------------------------------------------------------------------
# progress_indicator (= 이전 Spinner)
# ---------------------------------------------------------------------------

class TestProgressIndicator:
    def test_context_manager(self):
        import time
        with ex.progress_indicator("test") as pi:
            time.sleep(0.15)
        assert pi.elapsed >= 0.1

    def test_elapsed_increases(self):
        import time
        with ex.progress_indicator("test") as pi:
            time.sleep(0.2)
        assert pi.elapsed > 0


# ---------------------------------------------------------------------------
# main() CLI 파싱 (mocked)
# ---------------------------------------------------------------------------

class TestMainCli:
    def test_no_args_exits(self):
        with patch("sys.argv", ["execute.py"]):
            with pytest.raises(SystemExit) as exc_info:
                ex.main()
            assert exc_info.value.code == 2  # argparse exits with 2

    def test_invalid_phase_dir_exits(self):
        with patch("sys.argv", ["execute.py", "nonexistent"]):
            with patch.object(ex, "ROOT", Path("/tmp/fake_nonexistent")):
                with pytest.raises(SystemExit) as exc_info:
                    ex.main()
                assert exc_info.value.code == 1

    def test_missing_index_exits(self, tmp_project):
        (tmp_project / "phases" / "empty").mkdir()
        with patch("sys.argv", ["execute.py", "empty"]):
            with patch.object(ex, "ROOT", tmp_project):
                with pytest.raises(SystemExit) as exc_info:
                    ex.main()
                assert exc_info.value.code == 1

    def test_step_and_from_mutually_exclusive(self):
        with patch("sys.argv", ["execute.py", "0-mvp", "--step", "1", "--from", "2"]):
            with pytest.raises(SystemExit) as exc_info:
                ex.main()
            assert exc_info.value.code == 2  # argparse mutually-exclusive 위반


# ---------------------------------------------------------------------------
# _check_blockers (= 이전 main() error/blocked 체크)
# ---------------------------------------------------------------------------

class TestCheckBlockers:
    def _make_executor_with_steps(self, tmp_project, steps):
        d = tmp_project / "phases" / "test-phase"
        d.mkdir(exist_ok=True)
        index = {"project": "T", "phase": "test", "steps": steps}
        (d / "index.json").write_text(json.dumps(index))

        with patch.object(ex, "ROOT", tmp_project):
            inst = ex.StepExecutor.__new__(ex.StepExecutor)
        inst._root = str(tmp_project)
        inst._phases_dir = tmp_project / "phases"
        inst._phase_dir = d
        inst._phase_dir_name = "test-phase"
        inst._index_file = d / "index.json"
        inst._top_index_file = tmp_project / "phases" / "index.json"
        inst._phase_name = "test"
        inst._total = len(steps)
        return inst

    def test_error_step_exits_1(self, tmp_project):
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "bad", "status": "error", "error_message": "fail"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 1

    def test_blocked_step_exits_2(self, tmp_project):
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "stuck", "status": "blocked", "blocked_reason": "API key"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# _invoke_claude 실패 신호 (타임아웃 / is_error 파싱)
# ---------------------------------------------------------------------------

class TestInvokeClaudeSignals:
    def test_timeout_returns_timed_out_signal(self, executor):
        step = {"step": 2, "name": "ui"}
        exc = subprocess.TimeoutExpired(cmd="claude", timeout=1800)
        with patch("subprocess.run", side_effect=exc):
            out = executor._invoke_claude(step, "preamble")
        assert out["timed_out"] is True
        assert out["exitCode"] == -1
        # 타임아웃이어도 하네스가 죽지 않고 output 파일을 남겨야 한다
        assert (executor._phase_dir / "step2-output.json").exists()

    def test_is_error_parsed_from_cli_json(self, executor):
        mock_result = MagicMock(
            returncode=0,
            stdout='{"is_error": true, "result": "API 과부하"}',
            stderr="",
        )
        step = {"step": 2, "name": "ui"}
        with patch("subprocess.run", return_value=mock_result):
            out = executor._invoke_claude(step, "preamble")
        assert out["is_error"] is True
        assert "API 과부하" in out["cli_error"]

    def test_clean_run_has_no_error_signals(self, executor):
        mock_result = MagicMock(
            returncode=0,
            stdout='{"is_error": false, "result": "ok"}',
            stderr="",
        )
        step = {"step": 2, "name": "ui"}
        with patch("subprocess.run", return_value=mock_result):
            out = executor._invoke_claude(step, "preamble")
        assert out["is_error"] is False
        assert out["timed_out"] is False

    def test_malformed_stdout_does_not_crash(self, executor):
        mock_result = MagicMock(returncode=0, stdout="not json at all", stderr="")
        step = {"step": 2, "name": "ui"}
        with patch("subprocess.run", return_value=mock_result):
            out = executor._invoke_claude(step, "preamble")
        assert out["is_error"] is False  # 파싱 실패는 조용히 무시


# ---------------------------------------------------------------------------
# _execute_single_step 상태 머신 (완료 / 재시도 / 에러 / blocked / 타임아웃)
# ---------------------------------------------------------------------------

class TestExecuteSingleStep:
    @staticmethod
    def _signal(**kw):
        base = {"timed_out": False, "is_error": False, "exitCode": 0, "cli_error": ""}
        base.update(kw)
        return base

    def _set_step2(self, phase_dir, **fields):
        idx = json.loads((phase_dir / "index.json").read_text())
        for s in idx["steps"]:
            if s["step"] == 2:
                s.update(fields)
        (phase_dir / "index.json").write_text(json.dumps(idx, ensure_ascii=False))

    def _get_step2(self, phase_dir):
        idx = json.loads((phase_dir / "index.json").read_text())
        return next(s for s in idx["steps"] if s["step"] == 2)

    def test_completed_on_first_attempt(self, executor, phase_dir):
        def fake_invoke(step, preamble):
            self._set_step2(phase_dir, status="completed", summary="UI 완료")
            return self._signal()
        executor._invoke_claude = fake_invoke
        executor._commit_step = lambda *a: None

        result = executor._execute_single_step({"step": 2, "name": "ui"}, "")

        assert result is True
        s2 = self._get_step2(phase_dir)
        assert s2["status"] == "completed"
        assert "completed_at" in s2

    def test_retry_then_success(self, executor, phase_dir):
        calls = {"n": 0}
        def fake_invoke(step, preamble):
            calls["n"] += 1
            if calls["n"] < 2:
                self._set_step2(phase_dir, status="error", error_message="타입 에러")
            else:
                self._set_step2(phase_dir, status="completed", summary="ok")
            return self._signal()
        executor._invoke_claude = fake_invoke
        executor._commit_step = lambda *a: None

        result = executor._execute_single_step({"step": 2, "name": "ui"}, "")

        assert result is True
        assert calls["n"] == 2  # 1회 실패 후 2회차에 성공

    def test_retry_feeds_prev_error_into_preamble(self, executor, phase_dir):
        seen = {"preambles": []}
        calls = {"n": 0}
        def fake_invoke(step, preamble):
            seen["preambles"].append(preamble)
            calls["n"] += 1
            if calls["n"] < 2:
                self._set_step2(phase_dir, status="error", error_message="UNIQUE_ERR_X")
            else:
                self._set_step2(phase_dir, status="completed", summary="ok")
            return self._signal()
        executor._invoke_claude = fake_invoke
        executor._commit_step = lambda *a: None

        executor._execute_single_step({"step": 2, "name": "ui"}, "")

        # 2회차 preamble에는 1회차 에러가 피드백되어야 한다
        assert "UNIQUE_ERR_X" in seen["preambles"][1]
        assert "이전 시도 실패" in seen["preambles"][1]

    def test_error_after_max_retries_exits_1(self, executor, phase_dir):
        calls = {"n": 0}
        def fake_invoke(step, preamble):
            calls["n"] += 1
            self._set_step2(phase_dir, status="error", error_message="언제나 실패")
            return self._signal()
        executor._invoke_claude = fake_invoke
        executor._commit_step = lambda *a: None
        executor._update_top_index = lambda *a: None

        with pytest.raises(SystemExit) as exc_info:
            executor._execute_single_step({"step": 2, "name": "ui"}, "")

        assert exc_info.value.code == 1
        assert calls["n"] == ex.StepExecutor.MAX_RETRIES
        s2 = self._get_step2(phase_dir)
        assert s2["status"] == "error"
        assert "failed_at" in s2
        assert "언제나 실패" in s2["error_message"]

    def test_blocked_exits_2(self, executor, phase_dir):
        def fake_invoke(step, preamble):
            self._set_step2(phase_dir, status="blocked", blocked_reason="API 키 필요")
            return self._signal()
        executor._invoke_claude = fake_invoke
        executor._update_top_index = lambda *a: None

        with pytest.raises(SystemExit) as exc_info:
            executor._execute_single_step({"step": 2, "name": "ui"}, "")

        assert exc_info.value.code == 2
        s2 = self._get_step2(phase_dir)
        assert "blocked_at" in s2

    def test_timeout_signal_surfaces_in_error_message(self, executor, phase_dir):
        # 에이전트가 status를 갱신하지 못한 채 타임아웃된 경우
        def fake_invoke(step, preamble):
            return self._signal(timed_out=True, exitCode=-1)
        executor._invoke_claude = fake_invoke
        executor._commit_step = lambda *a: None
        executor._update_top_index = lambda *a: None

        with pytest.raises(SystemExit) as exc_info:
            executor._execute_single_step({"step": 2, "name": "ui"}, "")

        assert exc_info.value.code == 1
        s2 = self._get_step2(phase_dir)
        # "Step did not update status"로 뭉뚱그리지 않고 타임아웃을 명시해야 한다
        assert "타임아웃" in s2["error_message"]


# ---------------------------------------------------------------------------
# resume (--step / --from): _reset_for_resume
# ---------------------------------------------------------------------------

class TestResumeReset:
    def _steps(self, phase_dir):
        idx = json.loads((phase_dir / "index.json").read_text())
        return {s["step"]: s for s in idx["steps"]}

    def test_only_step_resets_just_target(self, executor, phase_dir):
        executor._only_step = 0
        executor._from_step = None
        executor._reset_for_resume()
        steps = self._steps(phase_dir)
        assert steps[0]["status"] == "pending"
        assert "summary" not in steps[0]      # 잔여 메타데이터 제거
        assert steps[1]["status"] == "completed"  # 다른 step은 그대로
        assert steps[2]["status"] == "pending"    # 원래 pending

    def test_from_step_resets_tail_only(self, executor, phase_dir):
        executor._only_step = None
        executor._from_step = 1
        executor._reset_for_resume()
        steps = self._steps(phase_dir)
        assert steps[0]["status"] == "completed"  # < from → 보존
        assert steps[1]["status"] == "pending"    # >= from → 초기화
        assert "summary" not in steps[1]
        assert steps[2]["status"] == "pending"

    def test_invalid_target_exits_1(self, executor):
        executor._only_step = 99
        executor._from_step = None
        with pytest.raises(SystemExit) as exc_info:
            executor._reset_for_resume()
        assert exc_info.value.code == 1

    def test_noop_without_resume_flags(self, executor, phase_dir):
        executor._only_step = None
        executor._from_step = None
        before = (phase_dir / "index.json").read_text()
        executor._reset_for_resume()
        after = (phase_dir / "index.json").read_text()
        assert before == after


# ---------------------------------------------------------------------------
# _finalize: 부분 실행은 phase를 completed로 마킹하지 않는다
# ---------------------------------------------------------------------------

class TestFinalize:
    def test_partial_run_does_not_mark_completed(self, executor, phase_dir):
        # 기본 fixture: step 2가 pending → 미완료
        called = {"top": False}
        executor._update_top_index = lambda *a: called.__setitem__("top", True)
        executor._run_git = lambda *a: MagicMock(returncode=0, stdout="", stderr="")
        executor._finalize()
        assert called["top"] is False
        # completed_at 도 찍히면 안 된다
        idx = json.loads((phase_dir / "index.json").read_text())
        assert "completed_at" not in idx

    def test_full_completion_marks_completed(self, executor, phase_dir):
        idx = json.loads((phase_dir / "index.json").read_text())
        for s in idx["steps"]:
            s["status"] = "completed"
        (phase_dir / "index.json").write_text(json.dumps(idx, ensure_ascii=False))

        called = {"top": False}
        executor._update_top_index = lambda *a: called.__setitem__("top", True)
        executor._run_git = lambda *a: MagicMock(returncode=0, stdout="", stderr="")
        executor._auto_push = False
        executor._finalize()

        assert called["top"] is True
        idx = json.loads((phase_dir / "index.json").read_text())
        assert "completed_at" in idx


# ---------------------------------------------------------------------------
# 브랜치명 단일 출처 (_branch) & phase/dir 불일치 감지
# ---------------------------------------------------------------------------

class TestBranchNaming:
    def test_branch_derives_from_phase_field(self, executor):
        # fixture: dir="0-mvp", phase="mvp" → branch="feat-mvp"
        assert executor._branch == "feat-mvp"

    def test_phase_field_mismatch_detected(self, executor):
        # phase("mvp") != dir("0-mvp") → 불일치 플래그 True
        assert executor._phase_field_differs is True

    def test_checkout_uses_branch_attr(self, executor):
        seen = {}
        def fake_git(*args):
            if args[:1] == ("checkout",):
                seen["branch"] = args[-1]
            if args == ("rev-parse", "--abbrev-ref", "HEAD"):
                return MagicMock(returncode=0, stdout="main\n", stderr="")
            if args[:2] == ("rev-parse", "--verify"):
                return MagicMock(returncode=1, stdout="", stderr="")  # 없음 → -b 생성
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git
        executor._checkout_branch()
        assert seen["branch"] == "feat-mvp"


# ---------------------------------------------------------------------------
# run() 통합: --step 으로 단일 step 재실행 → 전체 완료 시 finalize
# ---------------------------------------------------------------------------

class TestRunResumeIntegration:
    def test_run_with_only_step_completes_and_finalizes(self, tmp_project, phase_dir):
        with patch.object(ex, "ROOT", tmp_project):
            inst = ex.StepExecutor("0-mvp", only_step=2)
        inst._root = str(tmp_project)
        inst._phases_dir = tmp_project / "phases"
        inst._phase_dir = phase_dir
        inst._phase_dir_name = "0-mvp"
        inst._index_file = phase_dir / "index.json"
        inst._top_index_file = tmp_project / "phases" / "index.json"

        def fake_invoke(step, preamble):
            idx = json.loads((phase_dir / "index.json").read_text())
            for s in idx["steps"]:
                if s["step"] == 2:
                    s["status"] = "completed"
                    s["summary"] = "done"
            (phase_dir / "index.json").write_text(json.dumps(idx, ensure_ascii=False))
            return {"timed_out": False, "is_error": False, "exitCode": 0, "cli_error": ""}

        inst._invoke_claude = fake_invoke
        inst._run_git = lambda *a: MagicMock(
            returncode=0,
            stdout="main\n" if a[:1] == ("rev-parse",) else "",
            stderr="",
        )
        inst._update_top_index = lambda *a: None
        inst._load_guardrails = lambda: ""
        inst._ensure_claude_available = lambda: None  # 환경 비의존

        inst.run()

        idx = json.loads((phase_dir / "index.json").read_text())
        assert all(s["status"] == "completed" for s in idx["steps"])
        assert "completed_at" in idx  # 전체 완료되었으므로 phase 마킹됨


# ---------------------------------------------------------------------------
# _ensure_claude_available: claude CLI 부재 시 깔끔한 종료
# ---------------------------------------------------------------------------

class TestEnsureClaudeAvailable:
    def test_missing_claude_exits_1(self, executor):
        with patch.object(ex.shutil, "which", return_value=None):
            with pytest.raises(SystemExit) as exc_info:
                executor._ensure_claude_available()
        assert exc_info.value.code == 1

    def test_present_claude_passes(self, executor):
        with patch.object(ex.shutil, "which", return_value="/usr/bin/claude"):
            executor._ensure_claude_available()  # should not raise


# ---------------------------------------------------------------------------
# _validate_phase: 실행 전 정합성 검증 (step 파일/번호/phase 고유성)
# ---------------------------------------------------------------------------

class TestValidatePhase:
    def test_valid_phase_passes(self, executor):
        # 기본 fixture: step0,1 completed / step2 pending, step2.md 존재 → 통과
        executor._validate_phase()

    def test_missing_step_file_exits_1(self, executor):
        # step1을 재실행 대상으로 지정하지만 step1.md는 fixture에 없음
        executor._only_step = 1
        with pytest.raises(SystemExit) as exc_info:
            executor._validate_phase()
        assert exc_info.value.code == 1

    def test_completed_step_file_not_required(self, executor, phase_dir):
        # step0,1은 completed → 전체 실행 시 검증 범위에서 제외 (파일 없어도 통과)
        executor._validate_phase()  # step0.md/step1.md 없음에도 통과해야 함

    def test_duplicate_step_numbers_exit_1(self, executor, phase_dir):
        idx = json.loads((phase_dir / "index.json").read_text())
        idx["steps"].append({"step": 2, "name": "dup", "status": "pending"})
        (phase_dir / "index.json").write_text(json.dumps(idx, ensure_ascii=False))
        with pytest.raises(SystemExit) as exc_info:
            executor._validate_phase()
        assert exc_info.value.code == 1

    def test_empty_steps_exit_1(self, executor, phase_dir):
        idx = json.loads((phase_dir / "index.json").read_text())
        idx["steps"] = []
        (phase_dir / "index.json").write_text(json.dumps(idx, ensure_ascii=False))
        with pytest.raises(SystemExit) as exc_info:
            executor._validate_phase()
        assert exc_info.value.code == 1

    def test_from_step_scopes_file_check(self, executor, phase_dir):
        # --from 2 → step2만 검증 범위 (step2.md 존재) → 통과
        executor._from_step = 2
        executor._validate_phase()


# ---------------------------------------------------------------------------
# _check_phase_uniqueness: phase 값 충돌 감지
# ---------------------------------------------------------------------------

class TestCheckPhaseUniqueness:
    def _make_other_phase(self, tmp_project, dir_name, phase_value):
        d = tmp_project / "phases" / dir_name
        d.mkdir(exist_ok=True)
        idx = {"project": "T", "phase": phase_value,
               "steps": [{"step": 0, "name": "x", "status": "pending"}]}
        (d / "index.json").write_text(json.dumps(idx, ensure_ascii=False))

    def test_no_top_index_is_noop(self, executor):
        executor._check_phase_uniqueness()  # top index 없음 → 통과

    def test_unique_phase_passes(self, executor, tmp_project, top_index):
        # top index에 0-mvp(phase=mvp), 1-polish 존재. 1-polish는 다른 phase
        self._make_other_phase(tmp_project, "1-polish", "polish")
        executor._check_phase_uniqueness()  # 충돌 없음

    def test_colliding_phase_exits_1(self, executor, tmp_project, top_index):
        # 1-polish가 phase="mvp"로 0-mvp와 같은 브랜치/커밋 scope를 공유 → 충돌
        self._make_other_phase(tmp_project, "1-polish", "mvp")
        with pytest.raises(SystemExit) as exc_info:
            executor._check_phase_uniqueness()
        assert exc_info.value.code == 1

    def test_missing_other_index_skipped(self, executor, top_index):
        # 1-polish 디렉토리에 index.json이 없으면 조용히 건너뛴다
        executor._check_phase_uniqueness()  # should not raise
