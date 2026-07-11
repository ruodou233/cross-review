#!/usr/bin/env python3
"""cross-review 生命周期台账 —— 参考实现（作者环境脱敏版，非 drop-in）。

用途：为「审查标识与生命周期」协议提供一份可读、可跑的参考实现，帮助你的 Agent
生成适配自己仓库的等价代码。已移除作者本机路径、具体模型/档位钉参和真实项目
review_id 样本；REVIEW_MODEL / REVIEW_EFFORT 请改成你自己订阅内可用的最强模型。

安装前提：Python 3.10+ 标准库；在一个 Git 仓库根目录下运行。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import tarfile
import warnings
from typing import Any, Iterator

MAX_FILE = 1024 * 1024
SECRET_PATTERNS = [
    re.compile(rb"(?i)authorization\s*:\s*(?:bearer|basic)\s+\S+"),
    re.compile(rb"(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*['\"]?[A-Za-z0-9_./+\-=]{12,}"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]
STATUSES = ("open", "plan_approved", "implementing", "post_approved", "closed")
VERDICTS = ("approve", "request-changes", "reject")
REVIEW_ROLES = ("regular", "adversarial", "aggregate")
# 按你自己的 CLI/订阅改：本机可用的最强第二厂商模型与推理档位。
REVIEW_MODEL = os.environ.get("REVIEW_MODEL", "<your-strongest-reviewer-model>")
REVIEW_EFFORT = os.environ.get("REVIEW_EFFORT", "high")
LEGACY_ALLOWLIST = "cross-review/ledger-legacy-allowlist.json"
HEADER = (
    "# cross-review 审查结果记录\n\n"
    "字段定义与记录时机见 reference-impl/README.md「审查结果记录（review-log）」节。\n\n"
    "| date | ref | topic | mode | source | issues (b/nb) | adopted/rejected/deferred | rounds | note |\n"
    "|---|---|---|---|---|---|---|---|---|\n"
)


class LedgerError(Exception):
    """可向操作者直接展示的拒绝原因。"""


def repo_root() -> Path:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LedgerError("当前目录不是 Git 工作树") from exc
    return Path(out).resolve()


def common_dir(root: Path) -> Path:
    out = subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], cwd=root, text=True
    ).strip()
    path = Path(out)
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


@contextlib.contextmanager
def writer_lock(root: Path) -> Iterator[None]:
    lock_path = common_dir(root) / "review-ledger.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checked_bytes(path: Path, *, tracked: bool = True) -> bytes:
    if not path.is_file():
        raise LedgerError(f"文件不存在：{path}")
    size = path.stat().st_size
    if tracked and size > MAX_FILE:
        raise LedgerError(f"文件超过 1MiB：{path}")
    data = path.read_bytes()
    if tracked:
        for pattern in SECRET_PATTERNS:
            if pattern.search(data):
                raise LedgerError(f"文件命中 secret 模式：{path}")
    return data


def file_sha(path: Path, *, tracked: bool = True) -> str:
    return sha256_bytes(checked_bytes(path, tracked=tracked))


def atomic_json(path: Path, value: Any) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    if len(data) > MAX_FILE:
        raise LedgerError(f"待写文件超过 1MiB：{path}")
    for pattern in SECRET_PATTERNS:
        if pattern.search(data):
            raise LedgerError(f"待写文件命中 secret 模式：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def resolve_repo_path(root: Path, value: str) -> Path:
    path = Path(value)
    path = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise LedgerError(f"引用越出工作树：{value}") from exc
    return path


def relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(checked_bytes(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerError(f"JSON 无法解析：{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LedgerError(f"JSON 顶层必须是对象：{path}")
    return value


def ledger_path(root: Path, review_id: str) -> Path:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*", review_id):
        raise LedgerError("review_id 必须为 YYYY-MM-DD-<小写 slug>")
    return root / "cross-review" / "ledger" / f"{review_id}.json"


def load_state(root: Path, review_id: str) -> tuple[Path, dict[str, Any]]:
    path = ledger_path(root, review_id)
    if not path.exists():
        raise LedgerError(f"review 不存在：{review_id}")
    state = load_json(path)
    if state.get("review_id") != review_id:
        raise LedgerError("状态文件 review_id 与文件名不一致")
    return path, state


def require_state(state: dict[str, Any], *allowed: str) -> None:
    current = state.get("status")
    if current == "closed":
        raise LedgerError("closed 为终态，不可重开或修改生命周期")
    if current not in allowed:
        raise LedgerError(f"非法状态转移：当前 {current}，要求 {'/'.join(allowed)}")


def manifest_from_ref(root: Path, review_id: str, ref: str) -> tuple[Path, dict[str, Any], str]:
    path = resolve_repo_path(root, ref)
    expected_parent = root / "cross-review" / "archive" / review_id / "manifests"
    if path.parent != expected_parent.resolve():
        raise LedgerError("manifest 必须位于该 review 的 archive/manifests 目录")
    manifest = load_json(path)
    digest = file_sha(path)
    expected = f"{manifest.get('phase')}-attempt-{manifest.get('n')}-{digest[:12]}.json"
    if path.name != expected:
        raise LedgerError(f"manifest 内容寻址文件名不匹配，应为 {expected}")
    return path, manifest, digest


def referenced_pairs(value: Any) -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_file") and isinstance(item, str):
                sha = value.get(key[:-5] + "_sha256")
                if not isinstance(sha, str):
                    raise LedgerError(f"引用 {key} 缺少配对 sha256")
                yield item, sha
            yield from referenced_pairs(item)
    elif isinstance(value, list):
        for item in value:
            yield from referenced_pairs(item)


def verdict_from_output(text: str) -> str:
    matches = re.findall(
        r"(?im)^#{1,4}\s*Verdict\s*(?:[:：]\s*|\n+\s*)[*_]{0,2}"
        r"(approve|request-changes|reject)[*_]{0,2}\s*$",
        text,
    )
    if len(matches) != 1:
        raise LedgerError("聚合输出必须有且只有一个可核对的 Verdict")
    return matches[0]


def _argv_is_pinned(argv: Any, model: str, effort: str) -> bool:
    """argv 是否确实钉了 (model, effort) —— 参数来自 receipt 自报的钉参，不是当前全局
    REVIEW_MODEL/REVIEW_EFFORT。这样历史 receipt 的自洽性校验不会被日后升级模型打破。
    """
    if not isinstance(argv, list) or any(not isinstance(x, str) for x in argv):
        return False
    model_values = [argv[i + 1] for i in range(len(argv) - 1) if argv[i] in {"-m", "--model"}]
    effort_values = [argv[i + 1] for i in range(len(argv) - 1)
                     if argv[i] == "-c" and argv[i + 1].startswith("model_reasoning_effort")]
    return model_values == [model] and effort_values == [f'model_reasoning_effort="{effort}"']


def validate_manifest_data(
    root: Path,
    review_id: str,
    manifest: dict[str, Any],
    *,
    enforce_current_templates: bool = False,
    legacy_allowed: bool = False,
) -> list[str]:
    """所有非 legacy attempt manifest 的统一严格校验器。

    历史 manifest 走内容寻址，其记录的 contract hash 具有权威性；与当前模板
    不一致在 verify 时只警告。launcher 生成新 manifest 时按当前模板强校验。
    """
    if manifest.get("review_id") != review_id:
        raise LedgerError("manifest review_id 与归档目录不一致")
    if manifest.get("phase") not in ("pre", "post") or not isinstance(manifest.get("n"), int) or manifest["n"] < 1:
        raise LedgerError("manifest phase/n 无效")
    if manifest.get("verdict") not in VERDICTS:
        raise LedgerError("manifest verdict 无效")
    if manifest["verdict"] == "approve" and not manifest.get("user_confirmation"):
        raise LedgerError("approve manifest 缺少用户确认摘录")
    legacy = manifest.get("legacy")
    if isinstance(legacy, dict) and legacy.get("binding") in ("unverified", "unbound"):
        if not legacy_allowed:
            raise LedgerError("legacy manifest 不在冻结白名单")
        return []

    mode = manifest.get("mode")
    roles = manifest.get("roles")
    expected_roles = set(REVIEW_ROLES) if mode == "dual" else None
    if mode == "single":
        if not isinstance(roles, dict) or len(roles) != 1 or not manifest.get("single_authorization"):
            raise LedgerError("single manifest 必须有且只有一个 role，并绑定授权摘录与理由")
        expected_roles = set(roles)
    elif mode != "dual":
        raise LedgerError("manifest mode 必须为 dual 或 single")
    if not isinstance(roles, dict) or set(roles) != expected_roles:
        raise LedgerError(f"manifest roles 与 {mode} 模式不匹配")

    materials = manifest.get("materials")
    if not isinstance(materials, list) or len(materials) != 1:
        raise LedgerError("manifest materials 必须严格为单材料")
    material_pairs = {
        (item.get("material_file"), item.get("material_sha256"))
        for item in materials if isinstance(item, dict)
    }
    if len(material_pairs) != len(materials):
        raise LedgerError("manifest materials schema 或重复项无效")

    warnings_out: list[str] = []
    sessions: list[str] = []
    bindings: set[tuple[str, str, str, str]] = set()
    for role, receipt in roles.items():
        if role not in REVIEW_ROLES or not isinstance(receipt, dict):
            raise LedgerError(f"manifest role 无效：{role}")
        required = ("receipt_file", "receipt_sha256", "prompt_file", "prompt_sha256",
                    "output_file", "output_sha256", "review_id", "material_file",
                    "material_sha256", "user_request_file", "user_request_sha256")
        if any(not isinstance(receipt.get(key), str) or not receipt[key] for key in required):
            raise LedgerError(f"{role} receipt 缺少严格绑定字段")
        receipt_path = resolve_repo_path(root, receipt["receipt_file"])
        receipt_file_value = load_json(receipt_path)
        embedded = {key: value for key, value in receipt.items()
                    if key not in {"receipt_file", "receipt_sha256"}}
        if receipt_file_value != embedded:
            raise LedgerError(f"{role} manifest 内 receipt 与内容寻址 receipt 文件不自洽")
        if receipt.get("role") != role or receipt.get("phase") != manifest["phase"] or receipt.get("review_id") != review_id:
            raise LedgerError(f"{role} receipt 的 role/phase/review_id 不匹配")
        fork_check = receipt.get("fork_check", {})
        if receipt.get("exit_code") != 0 or fork_check.get("verified") is not True:
            raise LedgerError(f"{role} receipt 未成功或禁分叉未验证")
        if fork_check.get("guarantee") != "best-effort":
            if enforce_current_templates:
                raise LedgerError(f"{role} receipt 缺少 best-effort 保证标识")
            warnings_out.append(f"历史 manifest 的 {role} receipt 早于 best-effort 标识契约")
        requested_model = receipt.get("requested_model")
        requested_effort = receipt.get("requested_effort")
        if not isinstance(requested_model, str) or not isinstance(requested_effort, str):
            raise LedgerError(f"{role} receipt 缺少钉参声明")
        if not _argv_is_pinned(receipt.get("argv"), requested_model, requested_effort):
            raise LedgerError(f"{role} argv 与 receipt 自报的钉参不一致")
        if enforce_current_templates and (requested_model != REVIEW_MODEL or requested_effort != REVIEW_EFFORT):
            raise LedgerError(f"{role} 请求钉参不符合当前 policy（新 attempt 必须用当前配置发起）")
        session = receipt.get("session_id")
        if not isinstance(session, str) or not session:
            raise LedgerError(f"{role} session id 缺失")
        sessions.append(session)
        binding = (receipt["material_file"], receipt["material_sha256"],
                   receipt["user_request_file"], receipt["user_request_sha256"])
        bindings.add(binding)
        if (binding[0], binding[1]) not in material_pairs:
            raise LedgerError(f"{role} receipt 材料与本次 manifest 不一致")
        contract = receipt.get("role_contract_hash")
        if not isinstance(contract, str) or not re.fullmatch(r"[0-9a-f]{64}", contract):
            raise LedgerError(f"{role} role contract hash 无效")
        snapshot_ref = receipt.get("role_contract_snapshot_file")
        snapshot_sha = receipt.get("role_contract_snapshot_sha256")
        if snapshot_ref is not None or snapshot_sha is not None:
            if not isinstance(snapshot_ref, str) or snapshot_sha != contract:
                raise LedgerError(f"{role} role contract 快照绑定无效")
            snapshot = resolve_repo_path(root, snapshot_ref)
            expected_parent = root / "cross-review/archive" / review_id / "contracts"
            if snapshot.parent != expected_parent.resolve() or snapshot.name != f"{role}-{contract[:12]}.md":
                raise LedgerError(f"{role} role contract 快照路径无效")
            if file_sha(snapshot) != contract:
                raise LedgerError(f"{role} role contract 快照内容不匹配")
            if enforce_current_templates:
                # 和模型/档位钉参同一个坑：只验证快照内部自洽，不等于验证过"这是当前
                # 角色模板"。新 attempt 必须真的用当前 review-templates/*.md 发起。
                current_template = Path(__file__).resolve().parent / "review-templates" / f"{role}.md"
                if not current_template.is_file() or file_sha(current_template) != contract:
                    raise LedgerError(f"{role} role contract 与当前角色模板不一致，新 attempt 必须用当前模板发起")
        elif enforce_current_templates:
            raise LedgerError(f"{role} receipt 缺少 role contract 快照")
        else:
            warnings_out.append(f"历史 manifest 的 {role} receipt 早于 contract 快照契约")
    if len(set(sessions)) != len(sessions):
        raise LedgerError("各路 session id 必须互异")
    if len(bindings) != 1:
        raise LedgerError("各路 receipt 的 review/material/user-request 绑定必须一致")

    verdict_role = "aggregate" if mode == "dual" else next(iter(roles))
    verdict_receipt = roles[verdict_role]
    if mode == "dual":
        aggregate_prompt = checked_bytes(resolve_repo_path(root, verdict_receipt["prompt_file"])).decode()
        for role in ("regular", "adversarial"):
            output = checked_bytes(resolve_repo_path(root, roles[role]["output_file"])).decode()
            if output not in aggregate_prompt or roles[role]["output_sha256"] not in aggregate_prompt:
                raise LedgerError("聚合 prompt 未包含两路完整输出及 hash")
    verdict_text = checked_bytes(resolve_repo_path(root, verdict_receipt["output_file"])).decode()
    if verdict_from_output(verdict_text) != manifest["verdict"]:
        raise LedgerError("manifest verdict 与审查输出不一致")
    return warnings_out


def verify_manifest(root: Path, review_id: str, ref: str) -> tuple[dict[str, Any], str]:
    _, manifest, digest = manifest_from_ref(root, review_id, ref)
    for path_value, expected in referenced_pairs(manifest):
        actual = file_sha(resolve_repo_path(root, path_value))
        if actual != expected:
            raise LedgerError(f"manifest 内部引用哈希不匹配：{path_value}")
    legacy = isinstance(manifest.get("legacy"), dict)
    legacy_allowed = False
    if legacy:
        allow = load_json(root / LEGACY_ALLOWLIST)
        entries = allow.get("legacy_manifests", [])
        legacy_allowed = any(
            isinstance(item, dict)
            and item.get("review_id") == review_id
            and item.get("phase") == manifest.get("phase")
            and item.get("n") == manifest.get("n")
            and item.get("manifest_sha256") == digest
            for item in entries
        )
        if not legacy_allowed:
            raise LedgerError("legacy manifest 不在冻结白名单")
        warnings.warn("evidence_quality=legacy-unverified", UserWarning)
    for message in validate_manifest_data(root, review_id, manifest, legacy_allowed=legacy_allowed):
        warnings.warn(message, UserWarning)
    return manifest, digest


def cmd_new(args: argparse.Namespace, root: Path) -> None:
    path = ledger_path(root, args.review_id)
    archive = root / "cross-review/archive" / args.review_id
    if path.exists():
        raise LedgerError("review_id 已使用，永不允许复用")
    if args.bootstrap and not archive.exists():
        raise LedgerError("--bootstrap 仅允许用于已有 archive 的存量任务")
    if archive.exists() and not args.bootstrap:
        raise LedgerError("review_id 已使用，永不允许复用")
    history = subprocess.run(
        ["git", "log", "--all", "--format=%H", "--", relative(root, path)],
        cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    if history.stdout.strip():
        raise LedgerError("review_id 曾在 Git 历史出现，永不允许复用")
    state = {
        "schema_version": 1, "review_id": args.review_id, "topic": args.topic,
        "created": utc_now(), "status": "open", "attempts": [],
        "plan": {"digest": None, "approved_manifest_ref": None, "user_quote_file": None,
                 "user_quote_sha256": None, "approved_at": None},
        "impl": {"base_sha": None, "covered_commits": [], "post_manifest_ref": None,
                 "approved_at": None, "post_watermark": 0},
        "invalidated_approvals": [], "exemption_refs": [], "archive_ref": None,
        "closed_at": None,
    }
    atomic_json(path, state)
    print(args.review_id)


def cmd_attempt(args: argparse.Namespace, root: Path) -> None:
    path, state = load_state(root, args.review_id)
    require_state(state, "open", "implementing")
    expected_phase = "pre" if state["status"] == "open" else "post"
    manifest, digest = verify_manifest(root, args.review_id, args.manifest)
    if manifest["phase"] != expected_phase:
        raise LedgerError(f"跨 phase 借用：{state['status']} 只接受 {expected_phase}")
    phase_attempts = [a for a in state["attempts"] if a["phase"] == expected_phase]
    if manifest["n"] != len(phase_attempts) + 1:
        raise LedgerError("attempt 序号必须按 phase 从 1 连续递增")
    if args.verdict and args.verdict != manifest["verdict"]:
        raise LedgerError("命令 verdict 与 manifest 不一致")
    state["attempts"].append({
        "phase": expected_phase, "n": manifest["n"],
        "manifest_ref": relative(root, resolve_repo_path(root, args.manifest)),
        "manifest_sha256": digest, "verdict": manifest["verdict"], "ts": utc_now(),
    })
    atomic_json(path, state)


def latest_approve(state: dict[str, Any], phase: str) -> dict[str, Any]:
    attempts = [a for a in state["attempts"] if a["phase"] == phase]
    if not attempts or attempts[-1]["verdict"] != "approve":
        raise LedgerError(f"最新 {phase} attempt 必须为 approve")
    return attempts[-1]


def cmd_plan_approve(args: argparse.Namespace, root: Path) -> None:
    path, state = load_state(root, args.review_id)
    require_state(state, "open")
    attempt = latest_approve(state, "pre")
    plan = resolve_repo_path(root, args.plan_file)
    quote = resolve_repo_path(root, args.user_quote_file)
    approved_manifest, _ = verify_manifest(root, args.review_id, attempt["manifest_ref"])
    confirmation = approved_manifest.get("user_confirmation", {})
    if confirmation.get("user_quote_sha256") != file_sha(quote):
        raise LedgerError("plan-approve 摘录与 approve manifest 中的用户确认不一致")
    # 防止方案 A 通过审查后被误传方案 B：plan-approve 的 --plan-file 必须就是
    # approve manifest 里实际绑定、已经被审查过的那份材料，不能事后才靠 verify 发现错绑。
    plan_sha = file_sha(plan)
    bound_materials = approved_manifest.get("materials", [])
    if not any(isinstance(item, dict) and item.get("material_sha256") == plan_sha for item in bound_materials):
        raise LedgerError("plan-approve 的 --plan-file 与 approve manifest 绑定的审查材料不一致")
    state["plan"] = {
        "digest": file_sha(plan),
        "approved_manifest_ref": attempt["manifest_ref"],
        "user_quote_file": relative(root, quote), "user_quote_sha256": file_sha(quote),
        "approved_at": utc_now(),
    }
    state["status"] = "plan_approved"
    atomic_json(path, state)


def full_sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise LedgerError(f"要求完整 40 位 commit SHA：{value}")
    return value.lower()


def cmd_impl_start(args: argparse.Namespace, root: Path) -> None:
    path, state = load_state(root, args.review_id)
    require_state(state, "plan_approved", "post_approved")
    if state["status"] == "post_approved":
        if args.reason not in ("rebase", "revision"):
            raise LedgerError("post_approved 失效必须给 --reason rebase|revision")
        ref = state["impl"].get("post_manifest_ref")
        state["invalidated_approvals"].append(
            {"post_manifest_ref": ref, "reason": args.reason, "ts": utc_now()}
        )
        state["impl"]["post_manifest_ref"] = None
        state["impl"]["approved_at"] = None
        state["impl"]["covered_commits"] = []
        # 记下失效那一刻已经有几个 post attempt：post-approve 必须看到比这个数字更多
        # 的 post attempt 才能通过，否则 post-approve 会重新捞到失效前的旧 approve，
        # 状态机看起来推进了，实际上从没经过失效后的新审查——这是正常 CLI 可达路径，
        # 不是蓄意绕过。
        state["impl"]["post_watermark"] = len([a for a in state["attempts"] if a["phase"] == "post"])
    elif args.reason:
        raise LedgerError("首次 impl-start 不接受 --reason")
    base_sha = full_sha(args.base_sha)
    # 传一个不存在的 SHA 会让状态机卡在 implementing 且无法重新 impl-start 纠正，
    # 所以写状态前先确认它确实是一个可达的 commit。
    _git_output(root, ["cat-file", "-e", f"{base_sha}^{{commit}}"], f"base_sha 不是有效 commit：{base_sha}")
    state["impl"]["base_sha"] = base_sha
    state["status"] = "implementing"
    atomic_json(path, state)


def cmd_post_approve(args: argparse.Namespace, root: Path) -> None:
    path, state = load_state(root, args.review_id)
    require_state(state, "implementing")
    attempt = latest_approve(state, "post")
    post_count = len([a for a in state["attempts"] if a["phase"] == "post"])
    # fail-closed，不用 .get(..., 0) 默认放行：手工改过的、或由更早版本工具生成、
    # 缺这个字段的台账一律当成"水位未知"直接拒绝，而不是当成"水位为零"从而放行。
    watermark = state["impl"].get("post_watermark")
    # type(x) is int，不用 isinstance：Python 里 bool 是 int 的子类，
    # isinstance(False, int) 为真，"post_watermark": false 会被当成 0 放行。
    if type(watermark) is not int or watermark < 0 or watermark > post_count:
        raise LedgerError("impl.post_watermark 缺失或非法，拒绝 post-approve（可能是手工改过的台账或早期版本产物）")
    if post_count <= watermark:
        raise LedgerError("批准失效后必须先有一轮新的 post 审查 attempt，不能直接复用失效前的旧 approve")
    commits = [full_sha(x) for x in args.covered_commits]
    if not commits:
        raise LedgerError("covered_commits 不得为空")
    _verify_covered_commits(root, state, attempt["manifest_ref"], commits)
    state["impl"]["covered_commits"] = commits
    state["impl"]["post_manifest_ref"] = attempt["manifest_ref"]
    state["impl"]["approved_at"] = utc_now()
    state["status"] = "post_approved"
    atomic_json(path, state)


def _git_output(root: Path, args: list[str], error: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise LedgerError(f"{error}：{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def _verify_covered_commits(root: Path, state: dict[str, Any], manifest_ref: str,
                            commits: list[str]) -> None:
    """把批准区间与 Git 实际历史、以及本次唯一审查材料强绑定。"""
    base = full_sha(str(state.get("impl", {}).get("base_sha", "")))
    if len(set(commits)) != len(commits):
        raise LedgerError("covered_commits 不得重复")
    for sha in commits:
        _git_output(root, ["cat-file", "-e", f"{sha}^{{commit}}"], f"commit 不存在 {sha}")
        _git_output(root, ["merge-base", "--is-ancestor", base, sha], f"commit 不是 base 后代 {sha}")
    expected = _git_output(root, ["rev-list", "--reverse", "--topo-order", f"{base}..{commits[-1]}"],
                           "无法枚举 covered commit 范围")
    expected_commits = [line for line in expected.splitlines() if line]
    if commits != expected_commits:
        raise LedgerError("covered_commits 必须与 base..末提交完整集合及祖先顺序精确相等")
    manifest, _ = verify_manifest(root, state["review_id"], manifest_ref)
    materials = manifest.get("materials")
    if not isinstance(materials, list) or len(materials) != 1:
        raise LedgerError("approve manifest 必须严格绑定一份材料")
    text = checked_bytes(resolve_repo_path(root, materials[0]["material_file"])).decode("utf-8")
    # 只认完整 40 位 SHA，不接受短 SHA：git 的缩写长度会随仓库对象数增长或配置变化，
    # 用短 SHA 匹配会让今天写对的材料，未来在毫无修改的情况下重新校验时突然失败。
    for sha in commits:
        token = re.compile(rf"(?<![0-9a-fA-F]){re.escape(sha)}(?![0-9a-fA-F])")
        if not token.search(text):
            raise LedgerError(f"approve 材料未列出 covered commit 的完整 SHA：{sha}")


def cmd_close(args: argparse.Namespace, root: Path) -> None:
    path, state = load_state(root, args.review_id)
    require_state(state, "post_approved")
    if args.source not in ("regular", "adversarial", "both", "mixed", "-"):
        raise LedgerError("source 只允许 regular/adversarial/both/mixed/-")
    latest = latest_approve(state, "post")
    state["closure"] = {"issues": args.issues, "adopted": args.adopted,
                        "source": args.source, "note": args.note}
    state["archive_ref"] = {"path": latest["manifest_ref"], "sha256": latest["manifest_sha256"]}
    state["status"] = "closed"
    state["closed_at"] = utc_now()
    atomic_json(path, state)


def cmd_exempt(args: argparse.Namespace, root: Path) -> None:
    quote = resolve_repo_path(root, args.authorized_by_file)
    quote_sha = file_sha(quote)
    commits = [full_sha(x) for x in args.target_commits]
    if not args.paths:
        raise LedgerError("paths 不得为空")
    date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    # exemption 文件名会进 tracked 的 cross-review/exemptions/ 目录。默认用本机 hostname
    # 区分多机并发写入，但很多人的 hostname 带真实姓名/公司名，不想让它进 git 历史的话，
    # 设置 EXEMPT_HOST_LABEL 覆盖成一个不透露身份的标签（如 "dev-1"）。
    # 覆盖值和默认 hostname 走同一条清洗规则（只留小写字母数字连字符），否则
    # EXEMPT_HOST_LABEL 里塞路径分隔符或 ".." 可能让文件名逃出 exemptions 目录。
    label_source = os.environ.get("EXEMPT_HOST_LABEL") or socket.gethostname()
    host = re.sub(r"[^a-z0-9-]+", "-", label_source.lower()).strip("-")[:32] or "host"
    directory = root / "cross-review/exemptions"
    n = 1
    while (directory / f"EX-{date}-{host}-{n}.json").exists():
        n += 1
    exempt_id = f"EX-{date}-{host}-{n}"
    exempt_path = (directory / f"{exempt_id}.json").resolve()
    if exempt_path.parent != directory.resolve():
        raise LedgerError("exempt_id 拼出的路径越出 exemptions 目录")
    value = {"exempt_id": exempt_id, "ts": utc_now(), "reason": args.reason,
             "target_commits": commits, "paths": args.paths,
             "authorized_by_file": relative(root, quote), "sha256": quote_sha}
    if args.review_id:
        path, state = load_state(root, args.review_id)
        require_state(state, "open", "plan_approved", "implementing", "post_approved")
        value["review_id"] = args.review_id
        state["exemption_refs"].append(exempt_id)
        atomic_json(path, state)
    atomic_json(directory / f"{exempt_id}.json", value)
    print(exempt_id)


def verify_state(root: Path, path: Path) -> None:
    state = load_json(path)
    required = {"schema_version", "review_id", "topic", "created", "status", "attempts",
                "plan", "impl", "invalidated_approvals", "exemption_refs", "archive_ref", "closed_at"}
    if not required.issubset(state) or state["schema_version"] != 1 or state["status"] not in STATUSES:
        raise LedgerError(f"状态 schema 无效：{path}")
    if state.get("legacy"):
        if state["status"] != "closed" or not state["legacy"].get("original_row"):
            raise LedgerError(f"legacy 状态无效：{path}")
        allow = load_json(root / LEGACY_ALLOWLIST)
        if state.get("review_id") not in allow.get("legacy_state_review_ids", []):
            raise LedgerError(f"legacy 状态不在冻结 review_id 白名单：{path}")
        return
    seen: dict[str, int] = {"pre": 0, "post": 0}
    saw_post = False
    for attempt in state["attempts"]:
        phase = attempt.get("phase")
        if phase not in seen:
            raise LedgerError(f"attempt phase 无效：{path}")
        seen[phase] += 1
        saw_post = saw_post or phase == "post"
        if saw_post and phase == "pre":
            raise LedgerError(f"pre attempt 不得出现在 post attempt 之后：{path}")
        if attempt.get("n") != seen[phase]:
            raise LedgerError(f"attempt 序号不连续：{path}")
        manifest, digest = verify_manifest(root, state["review_id"], attempt["manifest_ref"])
        if digest != attempt["manifest_sha256"]:
            raise LedgerError(f"台账 manifest_sha256 不匹配：{path}")
        if manifest["phase"] != phase or manifest["n"] != attempt["n"] or manifest["verdict"] != attempt.get("verdict"):
            raise LedgerError(f"台账 attempt 与 manifest 事实不一致：{path}")
    plan = state["plan"]
    if plan.get("digest"):
        approved_ref = plan.get("approved_manifest_ref")
        if not approved_ref:
            raise LedgerError(f"plan digest 不匹配：{path}")
        approved, _ = verify_manifest(root, state["review_id"], approved_ref)
        candidates = [item for item in approved.get("materials", [])
                      if isinstance(item, dict) and item.get("material_sha256") == plan["digest"]]
        if not candidates or not any(file_sha(resolve_repo_path(root, item["material_file"])) == plan["digest"]
                                     for item in candidates):
            raise LedgerError(f"plan digest 不匹配：{path}")
    for key in ("user_quote_file",):
        if plan.get(key) and file_sha(resolve_repo_path(root, plan[key])) != plan.get("user_quote_sha256"):
            raise LedgerError(f"plan 授权摘录哈希不匹配：{path}")
    status = state["status"]
    if status == "open" and (seen["post"] or plan.get("digest")):
        raise LedgerError(f"open 状态含越级字段：{path}")
    if status in ("plan_approved", "implementing", "post_approved", "closed"):
        if not plan.get("digest") or not seen["pre"] or state["attempts"][seen["pre"] - 1].get("verdict") != "approve":
            raise LedgerError(f"状态缺少有效 pre approve：{path}")
    impl = state["impl"]
    if status in ("implementing", "post_approved", "closed") and not impl.get("base_sha"):
        raise LedgerError(f"实施状态缺少 base_sha：{path}")
    if status in ("post_approved", "closed"):
        post_attempts = [a for a in state["attempts"] if a["phase"] == "post"]
        if not post_attempts or post_attempts[-1]["verdict"] != "approve":
            raise LedgerError(f"批准状态缺少最新 post approve：{path}")
        if not impl.get("covered_commits") or not impl.get("post_manifest_ref"):
            raise LedgerError(f"批准状态缺少实施绑定：{path}")
        _verify_covered_commits(root, state, impl["post_manifest_ref"],
                                [full_sha(x) for x in impl["covered_commits"]])
    if status == "closed":
        archive = state.get("archive_ref")
        if not isinstance(archive, dict) or set(archive) != {"path", "sha256"}:
            raise LedgerError(f"closed 状态 archive_ref 无效：{path}")
        if file_sha(resolve_repo_path(root, archive["path"])) != archive["sha256"] or not state.get("closed_at"):
            raise LedgerError(f"closed 状态归档绑定无效：{path}")
    elif state.get("closed_at") is not None:
        raise LedgerError(f"非 closed 状态不得设置 closed_at：{path}")


def _verify_root(root: Path) -> tuple[int, int]:
    directory = root / "cross-review/ledger"
    paths = sorted(directory.glob("*.json")) if directory.exists() else []
    for path in paths:
        verify_state(root, path)
    exemptions = sorted((root / "cross-review/exemptions").glob("*.json")) if (root / "cross-review/exemptions").exists() else []
    for path in exemptions:
        value = load_json(path)
        required = {"exempt_id", "ts", "reason", "target_commits", "paths",
                    "authorized_by_file", "sha256"}
        if not required.issubset(value) or path.stem != value["exempt_id"]:
            raise LedgerError(f"豁免 schema 或文件名无效：{path}")
        if not value["target_commits"] or any(not re.fullmatch(r"[0-9a-f]{40}", x) for x in value["target_commits"]):
            raise LedgerError(f"豁免 commit SHA 无效：{path}")
        if file_sha(resolve_repo_path(root, value["authorized_by_file"])) != value["sha256"]:
            raise LedgerError(f"豁免授权摘录哈希不匹配：{path}")
    return len(paths), len(exemptions)


def _snapshot_root(root: Path, tip: str, target: Path) -> None:
    resolved = subprocess.check_output(
        ["git", "rev-parse", "--verify", f"{tip}^{{commit}}"], cwd=root, text=True
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", resolved):
        raise LedgerError("--tip 无法解析为完整 commit")
    proc = subprocess.run(
        ["git", "archive", "--format=tar", resolved, "cross-review", "tools"],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise LedgerError("无法从 tip 提取 ledger 验证快照：" + proc.stderr.decode("utf-8", "replace").strip())
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            destination = (target / member.name).resolve()
            try:
                destination.relative_to(target.resolve())
            except ValueError as exc:
                raise LedgerError("tip archive 含越界路径") from exc
        archive.extractall(target)


def cmd_verify(args: argparse.Namespace, root: Path) -> None:
    if args.tip:
        with tempfile.TemporaryDirectory(prefix="cross-review-ledger-tip-") as tmp:
            snapshot = Path(tmp).resolve()
            _snapshot_root(root, args.tip, snapshot)
            reviews, exemptions = _verify_root(snapshot)
    else:
        reviews, exemptions = _verify_root(root)
    print(f"verify 通过：{reviews} 个 review，{exemptions} 个 exemption")


def display_mode(attempts: list[dict[str, Any]]) -> str:
    return "dual-lens" if any(a.get("mode") == "dual" for a in attempts) else "single-review"


def cmd_render(args: argparse.Namespace, root: Path) -> None:
    rows: list[tuple[str, str]] = []
    directory = root / "cross-review/ledger"
    for path in sorted(directory.glob("*.json")) if directory.exists() else []:
        state = load_json(path)
        if state.get("legacy"):
            row = state["legacy"]["original_row"].rstrip()
            rows.append((state.get("created", ""), row + " (legacy)"))
            continue
        if state.get("status") != "closed":
            continue
        closure = state.get("closure") or {}
        for phase in ("pre", "post"):
            attempts = [a.copy() for a in state["attempts"] if a["phase"] == phase]
            if not attempts:
                continue
            strict_attempts: list[dict[str, Any]] = []
            legacy_attempts: list[dict[str, Any]] = []
            for attempt in attempts:
                manifest, _ = verify_manifest(root, state["review_id"], attempt["manifest_ref"])
                attempt["mode"] = manifest.get("mode")
                (legacy_attempts if isinstance(manifest.get("legacy"), dict)
                 else strict_attempts).append(attempt)
            date = (state.get("created") or state["review_id"][:10])[:10]
            archive = state.get("archive_ref") or {}
            ref = archive.get("path", "-") if isinstance(archive, dict) else str(archive)
            topic = f"{phase}:{state['topic']}"
            if legacy_attempts:
                legacy_fields = [date, legacy_attempts[-1]["manifest_ref"], topic,
                                 "legacy", "-", "-", "-", str(len(legacy_attempts)),
                                 "evidence_quality=legacy-unverified (legacy)"]
                legacy_fields = [str(x).replace("|", "\\|").replace("\n", " ") for x in legacy_fields]
                rows.append((state.get("created", ""), "| " + " | ".join(legacy_fields) + " |"))
            if not strict_attempts:
                continue
            mode = display_mode(strict_attempts)
            source = closure.get("source", "-") if mode == "dual-lens" else "-"
            fields = [date, ref, topic, mode, source,
                      closure.get("issues", ""), closure.get("adopted", ""),
                      str(len(strict_attempts)), closure.get("note", "")]
            fields = [str(x).replace("|", "\\|").replace("\n", " ") for x in fields]
            rows.append((state.get("created", ""), "| " + " | ".join(fields) + " |"))
    output = HEADER + "\n".join(row for _, row in sorted(rows, key=lambda item: item[0])) + ("\n" if rows else "")
    target = resolve_repo_path(root, args.output)
    data = output.encode()
    if len(data) > MAX_FILE or any(p.search(data) for p in SECRET_PATTERNS):
        raise LedgerError("render 输出违反容量或 secret 约束")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    print(relative(root, target))


def cmd_show(args: argparse.Namespace, root: Path) -> None:
    _, state = load_state(root, args.review_id)
    print(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="cross-review 生命周期台账（参考实现）")
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("new"); q.add_argument("--review-id", required=True); q.add_argument("--topic", required=True); q.add_argument("--bootstrap", action="store_true", help="仅用于工具上线前已有归档的存量任务建档"); q.set_defaults(fn=cmd_new)
    q = sub.add_parser("attempt"); q.add_argument("--review-id", required=True); q.add_argument("--manifest", required=True); q.add_argument("--verdict", choices=VERDICTS); q.set_defaults(fn=cmd_attempt)
    q = sub.add_parser("plan-approve"); q.add_argument("--review-id", required=True); q.add_argument("--plan-file", required=True); q.add_argument("--user-quote-file", required=True); q.set_defaults(fn=cmd_plan_approve)
    q = sub.add_parser("impl-start"); q.add_argument("--review-id", required=True); q.add_argument("--base-sha", required=True); q.add_argument("--reason", choices=("rebase", "revision")); q.set_defaults(fn=cmd_impl_start)
    q = sub.add_parser("post-approve"); q.add_argument("--review-id", required=True); q.add_argument("--covered-commits", nargs="+", required=True); q.set_defaults(fn=cmd_post_approve)
    q = sub.add_parser("close"); q.add_argument("--review-id", required=True); q.add_argument("--issues", required=True); q.add_argument("--adopted", required=True); q.add_argument("--source", required=True); q.add_argument("--note", required=True); q.set_defaults(fn=cmd_close)
    q = sub.add_parser("exempt"); q.add_argument("--reason", required=True); q.add_argument("--target-commits", nargs="+", required=True); q.add_argument("--paths", nargs="+", required=True); q.add_argument("--authorized-by-file", required=True); q.add_argument("--review-id"); q.set_defaults(fn=cmd_exempt)
    q = sub.add_parser("verify"); q.add_argument("--tip", help="只验证该 commit 树中的 ledger/manifest/模板快照"); q.set_defaults(fn=cmd_verify)
    q = sub.add_parser("render"); q.add_argument("--output", default="cross-review/review-log.md"); q.set_defaults(fn=cmd_render)
    q = sub.add_parser("show"); q.add_argument("--review-id", required=True); q.set_defaults(fn=cmd_show)
    return p


def main() -> int:
    args = parser().parse_args()
    try:
        root = repo_root()
        write = args.command not in ("show", "verify")
        with writer_lock(root) if write else contextlib.nullcontext():
            args.fn(args, root)
        return 0
    except (LedgerError, subprocess.CalledProcessError) as exc:
        print(f"拒绝：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
