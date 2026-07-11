#!/usr/bin/env python3
"""固定三路角色合同的 cross-review 启动器与 manifest finalizer —— 参考实现（脱敏版）。

禁分叉检测覆盖已知直接形态；蓄意规避在威胁模型外，保证等级为 best-effort。
调用的是 Codex CLI（`codex exec --json`）；换成其他厂商 CLI 时替换 run_role() 里
的 argv 拼装和 trace_facts() 的轨迹解析规则即可，其余状态机/校验逻辑不变。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import review_ledger as ledger

MODEL = ledger.REVIEW_MODEL
EFFORT = ledger.REVIEW_EFFORT
ROLES = ledger.REVIEW_ROLES
RAW_MAX_AGE_DAYS = 90
RAW_MAX_FILES_PER_DIR = 1000
AGENT_CLI_RE = re.compile(r"(?i)(^|[\s/;&|'\"=(])(codex|claude)([\s'\")&|;.]|$)")
ALLOWED_TOP_TYPES = {"thread.started", "turn.started", "turn.completed", "item.started", "item.updated", "item.completed"}
ALLOWED_ITEM_TYPES = {"agent_message", "command_execution", "reasoning", "analysis", "tool_call",
                      "tool_output", "mcp_tool_call", "web_search", "todo_list"}


def template_path(root: Path, role: str) -> Path:
    # 角色合同模板固定放在本脚本旁边的 review-templates/，与调用者的仓库布局无关，
    # 这样 reference-impl/ 挪到别的仓库、或从子目录外执行都不会找错路径。
    return Path(__file__).resolve().parent / "review-templates" / f"{role}.md"


def build_base_prompt(template: str, request: str, material: str, questions: str) -> str:
    return (
        template.rstrip() + "\n\n## 用户原始需求\n" + request.rstrip()
        + "\n\n## 待审内容原文\n" + material.rstrip()
        + "\n\n## 固定三问\n"
        + "1. 有什么更优的做法或方案？\n2. 哪些内容过于冗余，应如何删改？\n"
          "3. 方案有哪些未覆盖的场景、风险或依赖？\n"
        + ("\n## 验收标准与定向问题\n" + questions.rstrip() + "\n" if questions else "")
    )


def load_receipt(root: Path, value: str) -> tuple[Path, dict[str, Any]]:
    path = ledger.resolve_repo_path(root, value)
    receipt = ledger.load_json(path)
    return path, receipt


def build_prompt(args: argparse.Namespace, root: Path) -> str:
    template = ledger.checked_bytes(template_path(root, args.role)).decode()
    request = ledger.checked_bytes(ledger.resolve_repo_path(root, args.user_request_file)).decode()
    material = ledger.checked_bytes(ledger.resolve_repo_path(root, args.material_file)).decode()
    questions = ""
    if args.questions_file:
        questions = ledger.checked_bytes(ledger.resolve_repo_path(root, args.questions_file)).decode()
    prompt = build_base_prompt(template, request, material, questions)
    if args.role == "aggregate":
        if not args.regular_receipt or not args.adversarial_receipt:
            raise ledger.LedgerError("aggregate 必须提供两路 receipt")
        additions = []
        for role, ref in (("regular", args.regular_receipt), ("adversarial", args.adversarial_receipt)):
            _, receipt = load_receipt(root, ref)
            if receipt.get("role") != role:
                raise ledger.LedgerError(f"{role} receipt 角色不匹配")
            output_file = receipt.get("output_file")
            output = ledger.checked_bytes(ledger.resolve_repo_path(root, output_file)).decode()
            digest = ledger.sha256_bytes(output.encode())
            if digest != receipt.get("output_sha256"):
                raise ledger.LedgerError(f"{role} 输出哈希不匹配")
            additions.append(f"## {role} 完整输出（sha256: {digest}）\n{output.rstrip()}\n")
        prompt += "\n" + "\n".join(additions)
    return prompt


def trace_facts(raw: bytes) -> tuple[str, str, list[dict[str, Any]]]:
    session_ids: set[str] = set()
    messages: list[str] = []
    records: list[dict[str, Any]] = []
    if not raw.strip():
        raise ledger.LedgerError("JSON 轨迹为空，禁分叉不可验证")
    for number, line in enumerate(raw.splitlines(), 1):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ledger.LedgerError(f"JSON 轨迹第 {number} 行不可解析") from exc
        if not isinstance(obj, dict):
            raise ledger.LedgerError("JSON 轨迹记录不是对象")
        records.append(obj)
        typ = obj.get("type")
        if typ not in ALLOWED_TOP_TYPES:
            raise ledger.LedgerError(f"轨迹顶层类型不在白名单：{typ}")
        for key in ("thread_id", "session_id"):
            if isinstance(obj.get(key), str):
                session_ids.add(obj[key])
        item = obj.get("item")
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type not in ALLOWED_ITEM_TYPES:
                raise ledger.LedgerError(f"轨迹 item 类型不在白名单：{item_type}")
            if item_type in ("tool_call", "mcp_tool_call"):
                identity = " ".join(str(item.get(k, "")) for k in ("name", "tool", "tool_name", "server"))
                function = item.get("function")
                if isinstance(function, dict):
                    identity += " " + str(function.get("name", ""))
                if re.search(r"(?i)(spawn|collab|multi.?agent|delegate|(?:^|[_.-])task(?:$|[_.-]))", identity):
                    raise ledger.LedgerError("轨迹发现 spawn/task/collab 分叉工具")
            if item_type == "command_execution":
                command = item.get("command")
                if not isinstance(command, str):
                    raise ledger.LedgerError("command_execution 缺少可核验命令文本")
                if AGENT_CLI_RE.search(command):
                    raise ledger.LedgerError("轨迹发现 shell 间接调用 Agent CLI")
            if item_type == "agent_message":
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    messages.append(text)
        flat_type = str(typ).lower()
        if any(word in flat_type for word in ("spawn", "collab", "delegate", "task.")):
            raise ledger.LedgerError("轨迹发现分叉类型")
    if len(session_ids) != 1:
        raise ledger.LedgerError("轨迹必须且只能解析出一个 session id")
    if not messages:
        raise ledger.LedgerError("轨迹缺少 agent_message 输出")
    return next(iter(session_ids)), "\n".join(messages).rstrip() + "\n", records


def prune_raw_files(raw_root: Path, *, now: float | None = None) -> None:
    """Launcher raw/stderr retain <=90 days and <=1000 files per directory."""
    cutoff = (now if now is not None else dt.datetime.now().timestamp()) - RAW_MAX_AGE_DAYS * 86400
    if not raw_root.is_dir():
        return
    directories = [path for path in raw_root.rglob("*") if path.is_dir()]
    directories.append(raw_root)
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        files: list[Path] = []
        for path in directory.iterdir():
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                else:
                    files.append(path)
            except OSError:
                continue
        files.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        for path in files[RAW_MAX_FILES_PER_DIR:]:
            try:
                path.unlink()
            except OSError:
                pass
    for path in sorted(raw_root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if path.is_dir():
                path.rmdir()
        except (OSError, FileNotFoundError):
            continue


def run_role(args: argparse.Namespace, root: Path) -> None:
    prompt = build_prompt(args, root)
    prompt_data = prompt.encode()
    if len(prompt_data) > ledger.MAX_FILE or any(p.search(prompt_data) for p in ledger.SECRET_PATTERNS):
        raise ledger.LedgerError("prompt 违反 1MiB 或 secret 约束")
    # 按你自己的安装方式改：CODEX_BIN 环境变量优先，否则退回 PATH 里的 `codex`。
    codex = os.environ.get("CODEX_BIN", "codex")
    argv = [codex, "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only",
            "-m", MODEL, "-c", f'model_reasoning_effort="{EFFORT}"', "--json", "-"]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    # raw 轨迹/stderr 含审查全文，且可能带本机路径，绝不能落进受版本控制的工作树；
    # 固定写到 git common-dir（.git/ 或其等价目录）之下——这个目录本身天然不受
    # 工作树的 git 追踪，写在这里就不会被误提交，不是写到它外面的意思。
    local_dir = ledger.common_dir(root) / "cross-review-raw" / args.review_id
    prune_raw_files(local_dir.parent)
    local_dir.mkdir(parents=True, exist_ok=True)
    raw_path = local_dir / f"{args.phase}-{args.role}-{stamp}.jsonl"
    stderr_path = local_dir / f"{args.phase}-{args.role}-{stamp}.stderr"
    timed_out = False
    try:
        proc = subprocess.run(argv, input=prompt_data, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=args.timeout, cwd=root)
        raw = proc.stdout
        stderr = proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or b""
        stderr = (exc.stderr or b"") + f"\n启动器超时：{args.timeout} 秒\n".encode()
        exit_code = 124
        timed_out = True
    raw_path.write_bytes(raw)
    stderr_path.write_bytes(stderr)
    if exit_code != 0:
        raise ledger.LedgerError(f"codex 退出码 {exit_code}；stderr：{stderr_path}")
    session_id, output, _ = trace_facts(raw)
    output_data = output.encode()
    if len(output_data) > ledger.MAX_FILE or any(p.search(output_data) for p in ledger.SECRET_PATTERNS):
        raise ledger.LedgerError("output 违反 1MiB 或 secret 约束")
    archive = root / "cross-review/archive" / args.review_id / "receipts"
    archive.mkdir(parents=True, exist_ok=True)
    contract_data = ledger.checked_bytes(template_path(root, args.role))
    contract_hash = ledger.sha256_bytes(contract_data)
    contracts = root / "cross-review/archive" / args.review_id / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    contract_path = contracts / f"{args.role}-{contract_hash[:12]}.md"
    if contract_path.exists():
        if ledger.file_sha(contract_path) != contract_hash:
            raise ledger.LedgerError("role contract 快照路径已存在但内容不匹配")
    else:
        contract_path.write_bytes(contract_data)
    prompt_path = archive / f"{args.phase}-{args.role}-{stamp}-prompt.md"
    output_path = archive / f"{args.phase}-{args.role}-{stamp}-output.md"
    prompt_path.write_bytes(prompt_data)
    output_path.write_bytes(output_data)
    ledger.checked_bytes(output_path)
    # 归档进 tracked receipt 的 argv 只留逻辑命令名（basename），不留 CODEX_BIN 可能
    # 指向的本机绝对路径；_argv_is_pinned() 只关心 -m/-c 钉参，不关心命令本身的路径。
    logged_argv = [os.path.basename(argv[0]), *argv[1:]]
    receipt = {
        "schema_version": 1, "review_id": args.review_id, "role": args.role, "phase": args.phase,
        "argv": logged_argv,
        "requested_model": MODEL, "requested_effort": EFFORT,
        "role_contract_hash": contract_hash,
        "role_contract_snapshot_file": ledger.relative(root, contract_path),
        "role_contract_snapshot_sha256": contract_hash,
        "session_id": session_id, "exit_code": exit_code, "timed_out": timed_out,
        "prompt_file": ledger.relative(root, prompt_path), "prompt_sha256": ledger.file_sha(prompt_path),
        "output_file": ledger.relative(root, output_path), "output_sha256": ledger.file_sha(output_path),
        "material_file": ledger.relative(root, ledger.resolve_repo_path(root, args.material_file)),
        "material_sha256": ledger.file_sha(ledger.resolve_repo_path(root, args.material_file)),
        "user_request_file": ledger.relative(root, ledger.resolve_repo_path(root, args.user_request_file)),
        "user_request_sha256": ledger.file_sha(ledger.resolve_repo_path(root, args.user_request_file)),
        "raw_trace_path": ledger.relative(root, raw_path), "raw_trace_sha256": ledger.sha256_bytes(raw),
        "raw_trace_size": len(raw), "stderr_path": ledger.relative(root, stderr_path),
        "stderr_sha256": ledger.sha256_bytes(stderr), "stderr_size": len(stderr),
        "fork_check": {"verified": True, "method": "NDJSON 顶层/item 白名单；spawn/task/collab 与 shell Agent CLI 拒绝",
                       "guarantee": "best-effort"},
        "created": ledger.utc_now(),
    }
    receipt_path = ledger.resolve_repo_path(root, args.receipt_out) if args.receipt_out else archive / f"{args.phase}-{args.role}-{stamp}-receipt.json"
    ledger.atomic_json(receipt_path, receipt)
    print(ledger.relative(root, receipt_path))


def dual_run(args: argparse.Namespace, root: Path) -> None:
    """用两个独立进程发起互不可见 lenses，再将完整输出送聚合位。"""
    base = [sys.executable, str(Path(__file__).resolve()), "run", "--review-id", args.review_id,
            "--phase", args.phase, "--user-request-file", args.user_request_file,
            "--material-file", args.material_file, "--timeout", str(args.timeout)]
    if args.questions_file:
        base += ["--questions-file", args.questions_file]

    def child(role: str) -> str:
        proc = subprocess.run(base + ["--role", role], cwd=root, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode:
            raise ledger.LedgerError(f"{role} 路失败：{proc.stderr.strip()}")
        return proc.stdout.strip()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future_regular = pool.submit(child, "regular")
        future_adversarial = pool.submit(child, "adversarial")
        regular = future_regular.result()
        adversarial = future_adversarial.result()
    aggregate_cmd = base + ["--role", "aggregate", "--regular-receipt", regular,
                            "--adversarial-receipt", adversarial]
    aggregate_proc = subprocess.run(aggregate_cmd, cwd=root, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if aggregate_proc.returncode:
        raise ledger.LedgerError(f"aggregate 路失败：{aggregate_proc.stderr.strip()}")
    print(json.dumps({"regular": regular, "adversarial": adversarial,
                      "aggregate": aggregate_proc.stdout.strip()}, ensure_ascii=False, sort_keys=True))


def parse_receipt_args(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ledger.LedgerError("--receipt 格式必须为 role=path")
        role, path = value.split("=", 1)
        if role not in ROLES or role in result:
            raise ledger.LedgerError("receipt 角色无效或重复")
        result[role] = path
    return result


def _expected_attempt(root: Path, review_id: str, phase: str) -> int:
    """在写任何文件之前，先问台账"这轮 finalize 到底该是第几个 attempt"。

    没有这一步的话，传错 --n（比如台账明明只有 1 个 attempt，却传 --n 5）会
    成功写出一份任何 attempt 命令都不认、永远没法登记进台账的孤儿 manifest——
    而这份孤儿 manifest 引用的 receipt，会被 _reject_reused_receipts() 当成
    "已经用过"，导致这批 receipt 永久报废，即便正确的 --n 也无法再用它们。
    """
    _, state = ledger.load_state(root, review_id)
    ledger.require_state(state, "open", "implementing")
    expected_phase = "pre" if state["status"] == "open" else "post"
    if phase != expected_phase:
        raise ledger.LedgerError(f"跨 phase：当前台账状态 {state['status']} 只接受 phase={expected_phase}")
    phase_attempts = [a for a in state["attempts"] if a["phase"] == expected_phase]
    return len(phase_attempts) + 1


def _reject_reused_receipts(root: Path, review_id: str, receipts: dict[str, Any]) -> None:
    """一次真实审查只能被记一次账：拒绝把同一份 receipt（按内容哈希）或同一个
    session_id 拼进这个 review 下的第二份 manifest——否则只改 --n 就能让同一次
    审查被登记成好几轮独立 attempt，破坏"一次审查对应一次 attempt"这个核心事实。
    """
    manifests_dir = root / "cross-review/archive" / review_id / "manifests"
    if not manifests_dir.is_dir():
        return
    new_hashes = {r["receipt_sha256"] for r in receipts.values()}
    new_sessions = {r.get("session_id") for r in receipts.values() if r.get("session_id")}
    for path in manifests_dir.glob("*.json"):
        try:
            existing = ledger.load_json(path)
        except ledger.LedgerError:
            continue
        for role_receipt in (existing.get("roles") or {}).values():
            if not isinstance(role_receipt, dict):
                continue
            if role_receipt.get("receipt_sha256") in new_hashes:
                raise ledger.LedgerError(f"receipt 已被 {path.name} 使用过，不能登记进新 attempt")
            if role_receipt.get("session_id") in new_sessions:
                raise ledger.LedgerError(f"session_id 已被 {path.name} 使用过，不能登记进新 attempt")


def finalize(args: argparse.Namespace, root: Path) -> None:
    if args.n < 1:
        raise ledger.LedgerError("attempt n 必须大于零")
    expected_n = _expected_attempt(root, args.review_id, args.phase)
    if args.n != expected_n:
        raise ledger.LedgerError(f"--n 与台账期望的下一个 attempt 序号不符：应为 {expected_n}，收到 {args.n}")
    refs = parse_receipt_args(args.receipt)
    if args.mode == "single":
        if len(refs) != 1:
            raise ledger.LedgerError("single 模式必须且只能有一个 role receipt")
    elif set(refs) != set(ROLES):
        raise ledger.LedgerError(f"dual 模式 receipt 角色必须为 {sorted(ROLES)}")
    if args.mode == "single":
        if not args.authorized_by_file or not args.single_reason:
            raise ledger.LedgerError("single 必须提供授权摘录文件与理由")
        auth_path = ledger.resolve_repo_path(root, args.authorized_by_file)
        auth_sha = ledger.file_sha(auth_path)
    else:
        if args.authorized_by_file or args.single_reason:
            raise ledger.LedgerError("dual 不接受 single 授权参数")
        auth_path = None
        auth_sha = None
    receipts: dict[str, Any] = {}
    for role, ref in refs.items():
        path, receipt = load_receipt(root, ref)
        receipts[role] = {"receipt_file": ledger.relative(root, path),
                          "receipt_sha256": ledger.file_sha(path), **receipt}
    _reject_reused_receipts(root, args.review_id, receipts)
    verdict_receipt = receipts["aggregate"] if args.mode == "dual" else next(iter(receipts.values()))
    output = ledger.checked_bytes(ledger.resolve_repo_path(root, verdict_receipt["output_file"])).decode()
    verdict = ledger.verdict_from_output(output)
    if args.verdict and args.verdict != verdict:
        raise ledger.LedgerError("--verdict 与聚合输出不一致")
    manifest: dict[str, Any] = {
        "schema_version": 1, "review_id": args.review_id, "phase": args.phase,
        "n": args.n, "mode": args.mode, "roles": receipts,
        "verdict": verdict, "created": ledger.utc_now(),
    }
    materials = []
    for value in args.material_file:
        path = ledger.resolve_repo_path(root, value)
        materials.append({"material_file": ledger.relative(root, path), "material_sha256": ledger.file_sha(path)})
    manifest["materials"] = materials
    if verdict == "approve":
        if not args.user_quote_file:
            raise ledger.LedgerError("approve 轮必须提供用户确认摘录文件")
        approval_quote = ledger.resolve_repo_path(root, args.user_quote_file)
        manifest["user_confirmation"] = {
            "user_quote_file": ledger.relative(root, approval_quote),
            "user_quote_sha256": ledger.file_sha(approval_quote),
        }
    elif args.user_quote_file:
        raise ledger.LedgerError("非 approve 轮不接受用户确认摘录")
    if auth_path:
        manifest["single_authorization"] = {
            "authorized_by_file": ledger.relative(root, auth_path), "authorized_by_sha256": auth_sha,
            "reason": args.single_reason,
        }
    ledger.validate_manifest_data(root, args.review_id, manifest, enforce_current_templates=True)
    data = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    if len(data) > ledger.MAX_FILE or any(p.search(data) for p in ledger.SECRET_PATTERNS):
        raise ledger.LedgerError("manifest 违反 1MiB 或 secret 约束")
    digest = ledger.sha256_bytes(data)
    directory = root / "cross-review/archive" / args.review_id / "manifests"
    path = directory / f"{args.phase}-attempt-{args.n}-{digest[:12]}.json"
    if path.exists():
        raise ledger.LedgerError("内容寻址 manifest 已存在，不覆盖")
    ledger.atomic_json(path, manifest)
    print(ledger.relative(root, path))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="固定角色 cross-review 启动器（参考实现）")
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("run")
    q.add_argument("--review-id", required=True); q.add_argument("--phase", choices=("pre", "post"), required=True)
    q.add_argument("--role", choices=ROLES, required=True); q.add_argument("--user-request-file", required=True)
    q.add_argument("--material-file", required=True); q.add_argument("--questions-file")
    q.add_argument("--regular-receipt"); q.add_argument("--adversarial-receipt")
    q.add_argument("--timeout", type=int, default=900); q.add_argument("--receipt-out"); q.set_defaults(fn=run_role)
    q = sub.add_parser("dual-run")
    q.add_argument("--review-id", required=True); q.add_argument("--phase", choices=("pre", "post"), required=True)
    q.add_argument("--user-request-file", required=True); q.add_argument("--material-file", required=True)
    q.add_argument("--questions-file"); q.add_argument("--timeout", type=int, default=900); q.set_defaults(fn=dual_run)
    q = sub.add_parser("finalize")
    q.add_argument("--review-id", required=True); q.add_argument("--phase", choices=("pre", "post"), required=True)
    q.add_argument("--n", type=int, required=True); q.add_argument("--mode", choices=("dual", "single"), required=True)
    q.add_argument("--receipt", action="append", default=[], required=True); q.add_argument("--material-file", action="append", default=[], required=True)
    q.add_argument("--verdict", choices=ledger.VERDICTS); q.add_argument("--authorized-by-file"); q.add_argument("--single-reason")
    q.add_argument("--user-quote-file")
    q.set_defaults(fn=finalize)
    return p


def main() -> int:
    args = parser().parse_args()
    try:
        root = ledger.repo_root()
        ledger.load_state(root, args.review_id)
        if hasattr(args, "timeout") and args.timeout < 1:
            raise ledger.LedgerError("timeout 必须大于零")
        # finalize 产生不可变 manifest，持 ledger 同一锁；两路审查进程需并发且各用唯一文件名。
        if args.command == "finalize":
            with ledger.writer_lock(root):
                args.fn(args, root)
        else:
            args.fn(args, root)
        return 0
    except (ledger.LedgerError, OSError, subprocess.SubprocessError, KeyError) as exc:
        print(f"拒绝：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
