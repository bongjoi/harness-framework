#!/usr/bin/env python3
"""
Harness Step Executor — phase 내 step을 순차 실행하고 자가 교정한다.

Usage:
    python3 scripts/execute.py <phase-dir> [--push]
"""

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import types
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def progress_indicator(label: str):
    """터미널 진행 표시기. with 문으로 사용하며 .elapsed 로 경과 시간을 읽는다."""
    frames = "◐◓◑◒"
    stop = threading.Event()
    t0 = time.monotonic()

    def _animate():
        idx = 0
        while not stop.wait(0.12):
            sec = int(time.monotonic() - t0)
            sys.stderr.write(f"\r{frames[idx % len(frames)]} {label} [{sec}s]")
            sys.stderr.flush()
            idx += 1
        sys.stderr.write("\r" + " " * (len(label) + 20) + "\r")
        sys.stderr.flush()

    # 비-TTY(CI/파이프)에서는 \r 스피너가 로그를 더럽히므로 애니메이션을 끈다.
    th = None
    if sys.stderr.isatty():
        th = threading.Thread(target=_animate, daemon=True)
        th.start()
    info = types.SimpleNamespace(elapsed=0.0)
    try:
        yield info
    finally:
        stop.set()
        if th is not None:
            th.join()
        info.elapsed = time.monotonic() - t0


class StepExecutor:
    """Phase 디렉토리 안의 step들을 순차 실행하는 하네스."""

    MAX_RETRIES = 3
    FEAT_MSG = "feat({phase}): step {num} — {name}"
    CHORE_MSG = "chore({phase}): step {num} output"
    TZ = timezone(timedelta(hours=9))

    def __init__(self, phase_dir_name: str, *, auto_push: bool = False,
                 only_step: Optional[int] = None, from_step: Optional[int] = None):
        self._root = str(ROOT)
        self._phases_dir = ROOT / "phases"
        self._phase_dir = self._phases_dir / phase_dir_name
        self._phase_dir_name = phase_dir_name
        self._top_index_file = self._phases_dir / "index.json"
        self._auto_push = auto_push
        self._only_step = only_step
        self._from_step = from_step

        if not self._phase_dir.is_dir():
            print(f"ERROR: {self._phase_dir} not found")
            sys.exit(1)

        self._index_file = self._phase_dir / "index.json"
        if not self._index_file.exists():
            print(f"ERROR: {self._index_file} not found")
            sys.exit(1)

        idx = self._read_json(self._index_file)
        self._project = idx.get("project", "project")
        self._phase_name = idx.get("phase", phase_dir_name)
        # 브랜치/커밋 scope의 단일 출처. phase 필드(없으면 디렉토리명)를 따른다.
        self._branch = f"feat-{self._phase_name}"
        self._phase_field_differs = "phase" in idx and idx["phase"] != phase_dir_name
        self._total = len(idx["steps"])

    def run(self):
        self._print_header()
        self._ensure_claude_available()
        self._validate_phase()
        resume = self._only_step is not None or self._from_step is not None
        if not resume:
            # resume 모드에서는 대상 step을 직접 초기화하므로 전역 blocker 체크를 건너뛴다.
            self._check_blockers()
        self._checkout_branch()
        guardrails = self._load_guardrails()
        self._ensure_created_at()
        self._reset_for_resume()
        if self._only_step is not None:
            self._execute_only_step(guardrails)
        else:
            self._execute_all_steps(guardrails)
        self._finalize()

    # --- timestamps ---

    def _stamp(self) -> str:
        return datetime.now(self.TZ).strftime("%Y-%m-%dT%H:%M:%S%z")

    # --- JSON I/O ---

    @staticmethod
    def _read_json(p: Path) -> dict:
        return json.loads(p.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(p: Path, data: dict):
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- git ---

    def _run_git(self, *args) -> subprocess.CompletedProcess:
        cmd = ["git"] + list(args)
        return subprocess.run(cmd, cwd=self._root, capture_output=True, text=True)

    def _checkout_branch(self):
        branch = self._branch

        r = self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        if r.returncode != 0:
            print(f"  ERROR: git을 사용할 수 없거나 git repo가 아닙니다.")
            print(f"  {r.stderr.strip()}")
            sys.exit(1)

        if r.stdout.strip() == branch:
            return

        r = self._run_git("rev-parse", "--verify", branch)
        r = self._run_git("checkout", branch) if r.returncode == 0 else self._run_git("checkout", "-b", branch)

        if r.returncode != 0:
            print(f"  ERROR: 브랜치 '{branch}' checkout 실패.")
            print(f"  {r.stderr.strip()}")
            print(f"  Hint: 변경사항을 stash하거나 commit한 후 다시 시도하세요.")
            sys.exit(1)

        print(f"  Branch: {branch}")

    def _commit_step(self, step_num: int, step_name: str):
        output_rel = f"phases/{self._phase_dir_name}/step{step_num}-output.json"
        index_rel = f"phases/{self._phase_dir_name}/index.json"

        self._run_git("add", "-A")
        self._run_git("reset", "HEAD", "--", output_rel)
        self._run_git("reset", "HEAD", "--", index_rel)

        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self.FEAT_MSG.format(phase=self._phase_name, num=step_num, name=step_name)
            r = self._run_git("commit", "-m", msg)
            if r.returncode == 0:
                print(f"  Commit: {msg}")
            else:
                print(f"  WARN: 코드 커밋 실패: {r.stderr.strip()}")

        self._run_git("add", "-A")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self.CHORE_MSG.format(phase=self._phase_name, num=step_num)
            r = self._run_git("commit", "-m", msg)
            if r.returncode != 0:
                print(f"  WARN: housekeeping 커밋 실패: {r.stderr.strip()}")

    # --- top-level index ---

    def _update_top_index(self, status: str):
        if not self._top_index_file.exists():
            return
        top = self._read_json(self._top_index_file)
        ts = self._stamp()
        for phase in top.get("phases", []):
            if phase.get("dir") == self._phase_dir_name:
                phase["status"] = status
                ts_key = {"completed": "completed_at", "error": "failed_at", "blocked": "blocked_at"}.get(status)
                if ts_key:
                    phase[ts_key] = ts
                break
        self._write_json(self._top_index_file, top)

    # --- guardrails & context ---

    def _load_guardrails(self) -> str:
        sections = []
        claude_md = ROOT / "CLAUDE.md"
        if claude_md.exists():
            sections.append(f"## 프로젝트 규칙 (CLAUDE.md)\n\n{claude_md.read_text()}")
        docs_dir = ROOT / "docs"
        if docs_dir.is_dir():
            for doc in sorted(docs_dir.glob("*.md")):
                sections.append(f"## {doc.stem}\n\n{doc.read_text()}")
        return "\n\n---\n\n".join(sections) if sections else ""

    @staticmethod
    def _build_step_context(index: dict) -> str:
        lines = [
            f"- Step {s['step']} ({s['name']}): {s['summary']}"
            for s in index["steps"]
            if s["status"] == "completed" and s.get("summary")
        ]
        if not lines:
            return ""
        return "## 이전 Step 산출물\n\n" + "\n".join(lines) + "\n\n"

    def _build_preamble(self, guardrails: str, step_context: str,
                        prev_error: Optional[str] = None) -> str:
        commit_example = self.FEAT_MSG.format(
            phase=self._phase_name, num="N", name="<step-name>"
        )
        retry_section = ""
        if prev_error:
            retry_section = (
                f"\n## ⚠ 이전 시도 실패 — 아래 에러를 반드시 참고하여 수정하라\n\n"
                f"{prev_error}\n\n---\n\n"
            )
        return (
            f"당신은 {self._project} 프로젝트의 개발자입니다. 아래 step을 수행하세요.\n\n"
            f"{guardrails}\n\n---\n\n"
            f"{step_context}{retry_section}"
            f"## 작업 규칙\n\n"
            f"1. 이전 step에서 작성된 코드를 확인하고 일관성을 유지하라.\n"
            f"2. 이 step에 명시된 작업만 수행하라. 추가 기능이나 파일을 만들지 마라.\n"
            f"3. 기존 테스트를 깨뜨리지 마라.\n"
            f"4. AC(Acceptance Criteria) 검증을 직접 실행하라.\n"
            f"5. /phases/{self._phase_dir_name}/index.json의 해당 step status를 업데이트하라:\n"
            f"   - AC 통과 → \"completed\" + \"summary\" 필드에 이 step의 산출물을 한 줄로 요약\n"
            f"   - AC를 만족시키지 못하면 → \"error\" + \"error_message\"에 구체적 실패 내용 기록 "
            f"(자체 재시도 루프를 돌리지 마라. 실패하면 하네스가 자동으로 재시도한다)\n"
            f"   - 사용자 개입이 필요한 경우 (API 키, 인증, 수동 설정 등) → \"blocked\" + \"blocked_reason\" 기록 후 즉시 중단\n"
            f"6. 모든 변경사항을 커밋하라:\n"
            f"   {commit_example}\n\n---\n\n"
        )

    # --- Claude 호출 ---

    def _invoke_claude(self, step: dict, preamble: str) -> dict:
        step_num, step_name = step["step"], step["name"]
        step_file = self._phase_dir / f"step{step_num}.md"

        if not step_file.exists():
            print(f"  ERROR: {step_file} not found")
            sys.exit(1)

        prompt = preamble + step_file.read_text()

        timed_out = False
        try:
            result = subprocess.run(
                ["claude", "-p", "--dangerously-skip-permissions", "--output-format", "json", prompt],
                cwd=self._root, capture_output=True, text=True, timeout=1800,
            )
            returncode, stdout, stderr = result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired as e:
            # 30분 초과로 강제 종료. 하네스 전체가 죽지 않도록 실패 신호로 변환한다.
            timed_out = True
            returncode = -1
            stdout = e.stdout if isinstance(e.stdout, str) else ""
            stderr = e.stderr if isinstance(e.stderr, str) else ""
            print(f"\n  WARN: Claude가 타임아웃(30분)으로 강제 종료됨")

        if returncode != 0 and not timed_out:
            print(f"\n  WARN: Claude가 비정상 종료됨 (code {returncode})")
            if stderr:
                print(f"  stderr: {stderr[:500]}")

        # --output-format json 출력의 is_error를 신뢰 신호로 파싱한다.
        # (서브세션이 index.json status 갱신을 누락해도 세션 실패를 감지하기 위함)
        is_error = False
        cli_error = ""
        if stdout:
            try:
                payload = json.loads(stdout)
            except (json.JSONDecodeError, ValueError):
                payload = None
            if isinstance(payload, dict) and payload.get("is_error"):
                is_error = True
                cli_error = str(payload.get("result", ""))[:500]

        output = {
            "step": step_num, "name": step_name,
            "exitCode": returncode,
            "timed_out": timed_out, "is_error": is_error, "cli_error": cli_error,
            "stdout": stdout, "stderr": stderr,
        }
        self._write_json(self._phase_dir / f"step{step_num}-output.json", output)

        return output

    # --- 헤더 & 검증 ---

    def _print_header(self):
        print(f"\n{'='*60}")
        print(f"  Harness Step Executor")
        print(f"  Phase: {self._phase_name} | Steps: {self._total} | Branch: {self._branch}")
        if self._phase_field_differs:
            print(f"  Note: phase 필드('{self._phase_name}')가 디렉토리명('{self._phase_dir_name}')과 다릅니다.")
            print(f"        브랜치/커밋 scope는 phase 필드를 따릅니다.")
        if self._only_step is not None:
            print(f"  Mode: step {self._only_step}만 재실행 (상태 초기화)")
        elif self._from_step is not None:
            print(f"  Mode: step {self._from_step}부터 재실행 (상태 초기화)")
        if self._auto_push:
            print(f"  Auto-push: enabled")
        print(f"{'='*60}")

    def _ensure_claude_available(self):
        """claude CLI가 PATH에 있는지 미리 확인한다. 없으면 traceback 대신 깔끔히 종료."""
        if shutil.which("claude") is None:
            print("  ERROR: 'claude' CLI를 PATH에서 찾을 수 없습니다.")
            print("  Claude Code CLI 설치 여부와 PATH 설정을 확인한 뒤 다시 실행하세요.")
            sys.exit(1)

    def _steps_in_scope(self, steps: list) -> list:
        """이번 실행에서 실제로 돌 가능성이 있는 step만 추린다 (검증 범위 결정용)."""
        if self._only_step is not None:
            return [s for s in steps if s["step"] == self._only_step]
        if self._from_step is not None:
            return [s for s in steps if s["step"] >= self._from_step]
        # 전체 실행: 아직 완료되지 않은(=앞으로 돌릴) step들이 대상
        return [s for s in steps if s["status"] != "completed"]

    def _validate_phase(self):
        """실행 전 phase 정의의 정합성을 검사해 늦은 실패(fail-late)를 막는다."""
        index = self._read_json(self._index_file)
        steps = index.get("steps", [])
        if not steps:
            print(f"  ERROR: {self._index_file} 에 step이 정의되어 있지 않습니다.")
            sys.exit(1)

        nums = [s["step"] for s in steps]
        dupes = sorted({n for n in nums if nums.count(n) > 1})
        if dupes:
            print(f"  ERROR: step 번호가 중복되었습니다: {dupes}")
            print(f"  index.json의 steps[].step 값을 고유하게 맞추세요.")
            sys.exit(1)

        # 실행 대상 step의 step{N}.md 파일이 모두 존재하는지 미리 확인한다.
        missing = sorted(
            s["step"] for s in self._steps_in_scope(steps)
            if not (self._phase_dir / f"step{s['step']}.md").exists()
        )
        if missing:
            files = ", ".join(f"step{n}.md" for n in missing)
            print(f"  ERROR: 실행 대상 step 파일이 없습니다: {files}")
            print(f"  {self._phase_dir} 에 해당 파일을 만든 뒤 다시 실행하세요.")
            sys.exit(1)

        self._check_phase_uniqueness()

    def _check_phase_uniqueness(self):
        """top index의 다른 phase와 phase 값(=브랜치/커밋 scope)이 겹치는지 검사한다."""
        if not self._top_index_file.exists():
            return
        try:
            top = self._read_json(self._top_index_file)
        except (json.JSONDecodeError, ValueError):
            return
        for entry in top.get("phases", []):
            other_dir = entry.get("dir")
            if not other_dir or other_dir == self._phase_dir_name:
                continue
            other_index = self._phases_dir / other_dir / "index.json"
            if not other_index.exists():
                continue
            try:
                other = self._read_json(other_index)
            except (json.JSONDecodeError, ValueError):
                continue
            if other.get("phase", other_dir) == self._phase_name:
                print(f"  ERROR: phase 값 '{self._phase_name}'이(가) '{other_dir}'와 겹칩니다.")
                print(f"  두 phase가 같은 브랜치(feat-{self._phase_name})·커밋 scope를 "
                      f"공유해 충돌합니다. phase 값을 고유하게 바꾸세요.")
                sys.exit(1)

    def _check_blockers(self):
        index = self._read_json(self._index_file)
        for s in reversed(index["steps"]):
            if s["status"] == "error":
                print(f"\n  ✗ Step {s['step']} ({s['name']}) failed.")
                print(f"  Error: {s.get('error_message', 'unknown')}")
                print(f"  Fix and reset status to 'pending' to retry.")
                sys.exit(1)
            if s["status"] == "blocked":
                print(f"\n  ⏸ Step {s['step']} ({s['name']}) blocked.")
                print(f"  Reason: {s.get('blocked_reason', 'unknown')}")
                print(f"  Resolve and reset status to 'pending' to retry.")
                sys.exit(2)
            if s["status"] != "pending":
                break

    def _ensure_created_at(self):
        index = self._read_json(self._index_file)
        if "created_at" not in index:
            index["created_at"] = self._stamp()
            self._write_json(self._index_file, index)

    # --- 실행 루프 ---

    def _execute_single_step(self, step: dict, guardrails: str) -> bool:
        """단일 step 실행 (재시도 포함). 완료되면 True, 실패/차단이면 False."""
        step_num, step_name = step["step"], step["name"]
        done = sum(1 for s in self._read_json(self._index_file)["steps"] if s["status"] == "completed")
        prev_error = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            index = self._read_json(self._index_file)
            step_context = self._build_step_context(index)
            preamble = self._build_preamble(guardrails, step_context, prev_error)

            tag = f"Step {step_num}/{self._total - 1} ({done} done): {step_name}"
            if attempt > 1:
                tag += f" [retry {attempt}/{self.MAX_RETRIES}]"

            with progress_indicator(tag) as pi:
                signal = self._invoke_claude(step, preamble)
                elapsed = int(pi.elapsed)

            index = self._read_json(self._index_file)
            status = next((s.get("status", "pending") for s in index["steps"] if s["step"] == step_num), "pending")
            ts = self._stamp()

            if status == "completed":
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["completed_at"] = ts
                self._write_json(self._index_file, index)
                self._commit_step(step_num, step_name)
                print(f"  ✓ Step {step_num}: {step_name} [{elapsed}s]")
                return True

            if status == "blocked":
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["blocked_at"] = ts
                self._write_json(self._index_file, index)
                reason = next((s.get("blocked_reason", "") for s in index["steps"] if s["step"] == step_num), "")
                print(f"  ⏸ Step {step_num}: {step_name} blocked [{elapsed}s]")
                print(f"    Reason: {reason}")
                self._update_top_index("blocked")
                sys.exit(2)

            err_msg = next(
                (s.get("error_message", "Step did not update status") for s in index["steps"] if s["step"] == step_num),
                "Step did not update status",
            )

            # 세션 자체 실패 신호를 에러 메시지에 반영해 "status 미갱신"으로 뭉뚱그리지 않는다.
            if signal.get("timed_out"):
                err_msg = f"세션이 30분 타임아웃으로 강제 종료됨 — {err_msg}"
            elif signal.get("is_error"):
                err_msg = f"Claude CLI 오류(is_error): {signal.get('cli_error', '')} — {err_msg}"
            elif signal.get("exitCode", 0) != 0:
                err_msg = f"Claude 비정상 종료(code {signal.get('exitCode')}) — {err_msg}"

            if attempt < self.MAX_RETRIES:
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["status"] = "pending"
                        s.pop("error_message", None)
                self._write_json(self._index_file, index)
                prev_error = err_msg
                print(f"  ↻ Step {step_num}: retry {attempt}/{self.MAX_RETRIES} — {err_msg}")
            else:
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["status"] = "error"
                        s["error_message"] = f"[{self.MAX_RETRIES}회 시도 후 실패] {err_msg}"
                        s["failed_at"] = ts
                self._write_json(self._index_file, index)
                self._commit_step(step_num, step_name)
                print(f"  ✗ Step {step_num}: {step_name} failed after {self.MAX_RETRIES} attempts [{elapsed}s]")
                print(f"    Error: {err_msg}")
                self._update_top_index("error")
                sys.exit(1)

        return False  # unreachable

    def _mark_started(self, step_num: int):
        index = self._read_json(self._index_file)
        for s in index["steps"]:
            if s["step"] == step_num and "started_at" not in s:
                s["started_at"] = self._stamp()
                self._write_json(self._index_file, index)
                return

    # --- resume (--step / --from) ---

    _RESUME_CLEARED = (
        "error_message", "failed_at", "blocked_reason", "blocked_at",
        "completed_at", "started_at", "summary",
    )

    def _reset_for_resume(self):
        """--step / --from 대상 step들을 pending으로 되돌리고 잔여 메타데이터를 비운다."""
        if self._only_step is None and self._from_step is None:
            return

        index = self._read_json(self._index_file)
        valid = {s["step"] for s in index["steps"]}
        target = self._only_step if self._only_step is not None else self._from_step
        if target not in valid:
            print(f"  ERROR: step {target} 가 {self._index_file} 에 없습니다.")
            sys.exit(1)

        for s in index["steps"]:
            hit = (s["step"] == self._only_step) if self._only_step is not None \
                else (s["step"] >= self._from_step)
            if hit:
                s["status"] = "pending"
                for k in self._RESUME_CLEARED:
                    s.pop(k, None)
        self._write_json(self._index_file, index)

    def _execute_only_step(self, guardrails: str):
        index = self._read_json(self._index_file)
        step = next((s for s in index["steps"] if s["step"] == self._only_step), None)
        if step is None:  # _reset_for_resume 에서 검증되지만 방어적으로 둔다.
            print(f"  ERROR: step {self._only_step} not found")
            sys.exit(1)
        self._mark_started(self._only_step)
        self._execute_single_step(step, guardrails)

    def _execute_all_steps(self, guardrails: str):
        while True:
            index = self._read_json(self._index_file)
            pending = next((s for s in index["steps"] if s["status"] == "pending"), None)
            if pending is None:
                print("\n  All steps completed!")
                return

            self._mark_started(pending["step"])
            self._execute_single_step(pending, guardrails)

    def _finalize(self):
        index = self._read_json(self._index_file)

        # 부분 실행(--step/--from 등)으로 미완료 step이 남았으면 phase를 completed로 마킹하지 않는다.
        if not all(s["status"] == "completed" for s in index["steps"]):
            remaining = [s["step"] for s in index["steps"] if s["status"] != "completed"]
            print(f"\n  일부 step만 실행됨. 남은 step: {remaining}")
            print(f"  전체 실행하려면: python3 scripts/execute.py {self._phase_dir_name}")
            return

        index["completed_at"] = self._stamp()
        self._write_json(self._index_file, index)
        self._update_top_index("completed")

        self._run_git("add", "-A")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = f"chore({self._phase_name}): mark phase completed"
            r = self._run_git("commit", "-m", msg)
            if r.returncode == 0:
                print(f"  ✓ {msg}")

        if self._auto_push:
            branch = self._branch
            r = self._run_git("push", "-u", "origin", branch)
            if r.returncode != 0:
                print(f"\n  ERROR: git push 실패: {r.stderr.strip()}")
                sys.exit(1)
            print(f"  ✓ Pushed to origin/{branch}")

        print(f"\n{'='*60}")
        print(f"  Phase '{self._phase_name}' completed!")
        print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="Harness Step Executor")
    parser.add_argument("phase_dir", help="Phase directory name (e.g. 0-mvp)")
    parser.add_argument("--push", action="store_true", help="Push branch after completion")
    resume = parser.add_mutually_exclusive_group()
    resume.add_argument("--step", type=int, metavar="N",
                        help="step N 하나만 (상태 초기화 후) 재실행")
    resume.add_argument("--from", dest="from_step", type=int, metavar="N",
                        help="step N부터 끝까지 (상태 초기화 후) 재실행")
    args = parser.parse_args()

    StepExecutor(
        args.phase_dir,
        auto_push=args.push,
        only_step=args.step,
        from_step=args.from_step,
    ).run()


if __name__ == "__main__":
    main()
