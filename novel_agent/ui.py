from __future__ import annotations

import csv
import json
import datetime as dt
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .cli import APP_ROOT, PROJECTS_ROOT, load_json, load_profiles, read_optional, reset_project_writing_only
from .llm_client import split_api_keys


HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_SUB2API_BASE_URL = os.environ.get("NOVEL_AGENT_SUB2API_BASE_URL", "https://sub2api.aifakapro.com").rstrip("/")
DEFAULT_DEEPSEEK_BASE_URL = os.environ.get("NOVEL_AGENT_DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
JOBS: dict[str, dict[str, Any]] = {}
JOB_PROCESSES: dict[str, subprocess.Popen[str]] = {}
SECRETS_ROOT = APP_ROOT / "secrets"


PROVIDERS = {
    "chatgpt": {
        "name": "ChatGPT",
        "url": "https://chatgpt.com/",
        "note": "官方 ChatGPT 网页。OpenAI 帮助中心也说明可直接访问 chatgpt.com 登录。",
    },
    "claude": {
        "name": "Claude",
        "url": "https://claude.ai/",
        "note": "官方 Claude 网页。Claude Pro 登录在 claude.ai 完成。",
    },
    "manus": {
        "name": "Manus",
        "url": "https://manus.im/",
        "note": "Manus 官方站点。若你的订阅入口不同，可在浏览器中保持原登录页。",
    },
    "sub2api": {
        "name": "Sub2API",
        "url": DEFAULT_SUB2API_BASE_URL + "/",
        "note": "本地 Sub2API 管理页。可作为 OpenAI-compatible 模型入口接入自动写作。",
    },
    "deepseek": {
        "name": "DeepSeek API",
        "url": "https://platform.deepseek.com/",
        "note": "DeepSeek 官方 API 控制台。当前 Agent 通过 OpenAI-compatible 协议调用 deepseek-v4-pro / deepseek-v4-flash。",
    },
    "kimi": {
        "name": "Kimi API",
        "url": "https://platform.moonshot.cn/",
        "note": "Moonshot Kimi 官方 API 控制台。当前 Agent 默认通过 OpenAI-compatible 协议调用 kimi-k3。",
    },
}


def json_response(handler: BaseHTTPRequestHandler, data: Any, status: int = 200) -> None:
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def text_response(handler: BaseHTTPRequestHandler, text: str, content_type: str = "text/html; charset=utf-8") -> None:
    payload = text.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def read_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    payload = handler.rfile.read(length).decode("utf-8")
    if not payload:
        return {}
    return json.loads(payload)


def list_projects() -> list[dict[str, Any]]:
    projects = []
    if not PROJECTS_ROOT.exists():
        return projects
    for config_path in sorted(PROJECTS_ROOT.glob("*/00_config/project.json")):
        try:
            config = load_json(config_path)
        except Exception:
            continue
        project = config_path.parents[1]
        projects.append(
            {
                "name": config.get("name") or project.name,
                "platform": config.get("platform", ""),
                "path": str(project),
                "updated_at": config.get("updated_at", ""),
            }
        )
    return projects


def latest_key_pool_file() -> Path | None:
    if not SECRETS_ROOT.exists():
        return None
    files = sorted(SECRETS_ROOT.glob("sub2api_keys_*.txt"), key=lambda path: path.stat().st_mtime, reverse=True)
    return files[0] if files else None


def provider_key_file(provider: str) -> Path | None:
    path = SECRETS_ROOT / f"runtime_{provider}_keys.txt"
    return path if path.exists() else None


def key_env_for_provider(provider: str, data: dict[str, Any]) -> dict[str, str]:
    keys = split_api_keys(data.get("api_keys", "") or data.get("api_key", ""))
    if not keys:
        return {}

    joined = "\n".join(keys)
    first = keys[0]
    if provider == "openai":
        return {"OPENAI_API_KEYS": joined, "OPENAI_API_KEY": first}
    if provider == "anthropic":
        return {"ANTHROPIC_API_KEYS": joined, "ANTHROPIC_API_KEY": first}
    if provider == "deepseek":
        return {"DEEPSEEK_API_KEYS": joined, "DEEPSEEK_API_KEY": first}
    if provider == "kimi":
        return {"KIMI_API_KEYS": joined, "KIMI_API_KEY": first}
    return {
        "OPENAI_COMPATIBLE_API_KEYS": joined,
        "OPENAI_COMPATIBLE_API_KEY": first,
    }


def key_file_for_provider(provider: str, data: dict[str, Any]) -> Path | None:
    keys = split_api_keys(data.get("api_keys", "") or data.get("api_key", ""))
    if keys:
        SECRETS_ROOT.mkdir(parents=True, exist_ok=True)
        path = SECRETS_ROOT / f"runtime_{provider}_keys.txt"
        path.write_text("\n".join(keys) + "\n", encoding="utf-8")
        return path
    if provider == "sub2api":
        return latest_key_pool_file()
    if provider in {"deepseek", "kimi"}:
        return provider_key_file(provider)
    return None


def source_timeout_for_mode(data: dict[str, Any]) -> int:
    value = int(data.get("source_timeout") or 0)
    if value > 0:
        return value
    return 3600 if data.get("source_mode") == "deep" else 900


SOURCE_AGGREGATE_FILES = [
    "source_bibles.md",
    "source_style_profile.md",
    "motif_library.csv",
    "character_pool.csv",
    "plot_pool.csv",
    "power_system_pool.csv",
    "fusion_opportunities.md",
    "source_risk_notes.md",
]

OUTLINE_CANDIDATE_FILES = [
    "outline_1.md",
    "outline_2.md",
    "outline_3.md",
]

BLUEPRINT_FILES = [
    "novel_bible.md",
    "style_guide.md",
    "power_system.md",
    "worldbuilding.md",
    "character_table.csv",
    "volume_outline.md",
    "full_story_outline.md",
    "fusion_traceability.md",
    "chapter_plan.csv",
    "continuity_ledger.md",
    "foreshadowing_ledger.md",
    "memory_rollup.md",
]

CHAPTER_PLAN_HEADER = (
    "chapter_no,volume,title,core_conflict,small_hook,power_usage,character_change,"
    "foreshadowing,ending_hook,mainline_progress,source_inspiration,status"
)


def ready_file(path: Path, min_size: int = 200) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= min_size
    except OSError:
        return False


def ready_count(root: Path, names: list[str], min_size: int = 200) -> int:
    return sum(1 for name in names if ready_file(root / name, min_size))


def chapter_plan_complete(path: Path, target_chapters: int) -> bool:
    if not ready_file(path, 500):
        return False
    text = read_optional(path, 10_000_000)
    rows = list(csv.reader(text.splitlines()))
    expected_header = next(csv.reader([CHAPTER_PLAN_HEADER]))
    if not rows or rows[0] != expected_header:
        return False
    numbers: set[int] = set()
    for row in rows[1:]:
        if row and re.fullmatch(r"\d{1,4}", row[0].strip()):
            numbers.add(int(row[0]))
    return all(number in numbers for number in range(1, max(1, target_chapters) + 1))


def chapter_plan_parts_progress(project: Path, target_chapters: int) -> tuple[int, int]:
    final_plan = project / "03_new_novel" / "chapter_plan.csv"
    if chapter_plan_complete(final_plan, target_chapters):
        return target_chapters, target_chapters
    numbers: set[int] = set()
    paths = [final_plan]
    parts_dir = project / "03_new_novel" / "chapter_plan_parts"
    if parts_dir.exists():
        paths.extend(path for path in parts_dir.glob("ch_*.csv") if "_raw_" not in path.name)
    for path in paths:
        if not path.exists():
            continue
        for row in csv.reader(read_optional(path, 2_000_000).splitlines()):
            if not row or not re.fullmatch(r"\d{1,4}", row[0].strip()):
                continue
            content_cells = [cell.strip() for cell in row[1:11]]
            has_usable_plan = len(row) >= 8 and sum(1 for cell in content_cells if cell) >= 4
            if has_usable_plan:
                number = int(row[0])
                if 1 <= number <= target_chapters:
                    numbers.add(number)
    continuous = 0
    while continuous + 1 in numbers:
        continuous += 1
    return len(numbers), continuous


def blueprint_ready_count(project: Path, target_chapters: int) -> int:
    total = 0
    rewrite_snapshot = project / "00_config" / "rewrite_basis.json"
    for name in BLUEPRINT_FILES:
        path = project / "03_new_novel" / name
        if name == "chapter_plan.csv":
            if chapter_plan_complete(path, target_chapters):
                total += 1
        elif name == "character_table.csv" and rewrite_snapshot.exists() and ready_file(path, 50):
            total += 1
        elif name == "foreshadowing_ledger.md" and rewrite_snapshot.exists() and "从第0001章重写开始记录" in read_optional(path, 1000):
            total += 1
        elif ready_file(path, 500):
            total += 1
    return total


def average_chapter_words(config: dict[str, Any]) -> int:
    try:
        profile = load_profiles().get(config.get("platform", ""), {})
    except Exception:
        profile = {}
    raw = str(profile.get("chapter_words") or "2500")
    values = [int(value) for value in re.findall(r"\d+", raw)]
    if not values:
        return 2500
    if len(values) == 1:
        return max(1000, values[0])
    return max(1000, round(sum(values[:2]) / 2))


def planned_chapter_count(project: Path, config: dict[str, Any], fallback: int = 0) -> int:
    text = "\n\n".join(
        [
            read_optional(project / "03_new_novel" / "full_story_outline.md", 80000),
            read_optional(project / "03_new_novel" / "volume_outline.md", 80000),
        ]
    ).replace("状态：待生成", "")
    numbers: list[int] = []
    for pattern in (
        r"(?:目标总章数|总章数目标|计划总章数|预计总章数|全书总章数)[:：]?\s*(?:约|大约|预计)?\s*(\d{2,4})\s*章",
        r"(?:章节范围|章数范围)[:：]\s*(\d{1,4})\s*[-—~至到]\s*(\d{1,4})",
        r"第\s*(\d{1,4})\s*章\s*[-—~至到]\s*第?\s*(\d{1,4})\s*章",
        r"(\d{1,4})\s*[-—~至到]\s*(\d{1,4})\s*章",
    ):
        for match in re.finditer(pattern, text):
            groups = [int(group) for group in match.groups() if group and group.isdigit()]
            if groups:
                numbers.append(max(groups))
    plausible = [number for number in numbers if 100 <= number <= 2000]
    if plausible:
        return max(plausible)
    if fallback > 0:
        return fallback
    estimate = round(2_500_000 / average_chapter_words(config))
    return int(round(estimate / 25) * 25)


def draft_chapter_progress(drafts: list[Path]) -> tuple[int, int]:
    batch_count = 0
    max_chapter = 0
    for path in drafts:
        if not ready_file(path, 500):
            continue
        match = re.match(r"ch_(\d+)_to_(\d+)\.md$", path.name)
        if not match:
            continue
        batch_count += 1
        max_chapter = max(max_chapter, int(match.group(2)))
    return batch_count, max_chapter


def progress_step(name: str, done: bool, detail: str = "", current: bool = False) -> dict[str, Any]:
    return {"name": name, "done": done, "detail": detail, "current": current}


def mtime_iso(path: Path) -> str:
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return ""


def file_status(project: Path, rel: str, label: str = "") -> dict[str, Any]:
    path = project / rel
    exists = path.exists()
    return {
        "label": label or path.name,
        "rel": rel,
        "path": str(path),
        "exists": exists,
        "size": path.stat().st_size if exists and path.is_file() else 0,
        "updated_at": mtime_iso(path) if exists else "",
    }


def load_state_json(path: Path) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        data = load_json(path)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def latest_revision_status(project: Path, state: dict[str, Any]) -> dict[str, Any]:
    revisions = state.get("revisions") or []
    if not revisions:
        approvals = state.get("approvals") or []
        last_note = approvals[-1].get("note", "") if approvals else ""
        return {
            "exists": False,
            "title": "还没有成功应用自动微调",
            "summary": "这里会显示“应用微调”真正改了哪些设定文件。直接点“确认并继续”只会记录说明，不会改文件。",
            "note": last_note,
            "note_label": "上次确认说明（没有改文件）",
            "files": [],
            "report": {},
        }
    revision = revisions[-1]
    files = []
    changed = revision.get("changed_files") or []
    if changed:
        for item in changed:
            info = file_status(project, item.get("file", ""))
            info.update(
                {
                    "delta_chars": item.get("delta_chars", 0),
                    "before_chars": item.get("before_chars", 0),
                    "after_chars": item.get("after_chars", 0),
                    "before_lines": item.get("before_lines", 0),
                    "after_lines": item.get("after_lines", 0),
                }
            )
            files.append(info)
    else:
        files = [file_status(project, str(rel).replace("\\", "/")) for rel in revision.get("files", [])]
    report_rel = revision.get("report") or ""
    return {
        "exists": True,
        "title": "最近一次自动微调",
        "summary": f"已按你的要求更新 {len(files)} 个设定文件。检查无误后点“确认并继续”；不满意就继续写新的微调要求。",
        "note": revision.get("note", ""),
        "note_label": "微调要求",
        "revised_at": revision.get("revised_at", ""),
        "files": files,
        "report": file_status(project, report_rel, "微调效果报告") if report_rel else {},
    }


def decision_status(project: Path, state: dict[str, Any], progress: dict[str, Any]) -> dict[str, Any]:
    awaiting = state.get("awaiting") or {}
    awaiting_type = awaiting.get("type")
    activity = state.get("activity") or ""
    is_revising = any(
        marker in activity
        for marker in (
            "开始应用节点微调",
            "微调调用模型中",
            "微调模型已返回",
            "正在解析并写入文件",
        )
    )
    if is_revising:
        title = "正在应用微调：融合设定包重写中"
        body = "模型正在根据你的微调要求重写相关设定文件。当前不要再点“应用微调”或“确认并继续”，等日志出现写入文件和微调报告后再检查。"
        action = "等待任务完成；完成后看“最近微调效果”和 03_new_novel/revision_report_*.md。"
    elif awaiting_type == "outline_selection":
        title = "现在需要你决定：选择哪套候选大纲"
        body = (
            "模型已经基于源书素材池生成三套不同方向的大纲候选。"
            "先查看三套方案，选择最接近你想要的那一套；选择后再点“启动 / 继续”，Agent 会把它扩展成完整设定包和章节规划。"
        )
        action = "先点候选大纲里的“选择此方案”，再点“启动 / 继续”。"
    elif awaiting_type == "blueprint":
        title = "现在需要你决定：设定包是否通过"
        body = (
            "模型已经把源书素材融合成新书设定包和章节规划，流程暂停等你检查。"
            "如果要改方向、爽点、境界、主角能力、女主/配角功能位，就在输入框写要求后点“应用微调”。"
            "如果满意，点“确认并继续”，Agent 会开始写正文。"
        )
        action = "检查 03_new_novel；需要修改就应用微调，满意就确认并继续。"
    elif awaiting_type == "source_failures":
        title = "源书还没有拆全：暂不融合"
        body = (
            "有少量源书在模型调用或上游连接中失败。为避免遗漏你提供的素材，"
            "Agent 已阻止进入候选大纲和设定包生成。"
        )
        action = "点“启动 / 继续”只会重试尚未完成的源书；拆解齐全后才会开始融合。"
    elif awaiting:
        title = "现在需要你决定：当前批次是否通过"
        body = awaiting.get("message") or "流程暂停在确认节点。"
        action = "检查最新正文/摘要；满意就确认并继续，不满意就先人工修改或输入微调说明。"
    else:
        title = progress.get("label") or "当前没有等待你确认的节点"
        body = state.get("activity") or progress.get("eta") or "流程可以继续运行。"
        action = "如果后台没有运行，点“启动 / 继续”。"
    if awaiting_type == "outline_selection":
        suggested_files = []
        for index in range(1, 4):
            info = file_status(
                project,
                f"03_new_novel/outline_candidates/outline_{index}.md",
                f"候选大纲 {index}",
            )
            info["candidate_no"] = index
            suggested_files.append(info)
    else:
        suggested_files = [
            file_status(project, "03_new_novel/novel_bible.md", "新书总设定"),
            file_status(project, "03_new_novel/power_system.md", "力量体系/升级爽点"),
            file_status(project, "03_new_novel/full_story_outline.md", "全书规划/大结局"),
            file_status(project, "03_new_novel/fusion_traceability.md", "融合来源映射"),
            file_status(project, "03_new_novel/chapter_plan.csv", "章节规划"),
            file_status(project, "03_new_novel/memory_rollup.md", "长期记忆"),
        ]
    return {
        "title": title,
        "body": body,
        "action": action,
        "awaiting_type": awaiting_type or "",
        "target": file_status(project, awaiting.get("target", ""), "当前检查入口") if awaiting.get("target") else {},
        "selected_outline_candidate": state.get("selected_outline_candidate") or "",
        "suggested_files": suggested_files,
    }


def project_progress(
    project: Path,
    config: dict[str, Any],
    state: dict[str, Any],
    source_info: dict[str, Any],
    drafts: list[Path],
) -> dict[str, Any]:
    books = int(source_info.get("book_count") or 0)
    source_files = int(source_info.get("file_count") or 0)
    per_book_dir = project / "02_source_analysis" / "per_book"
    per_book_done = len([path for path in per_book_dir.glob("*.md") if ready_file(path)])
    failed_dir = project / "02_source_analysis" / "failed_books"
    per_book_failed = (
        len(
            [
                path
                for path in failed_dir.glob("*.md")
                if ready_file(path, 50) and not ready_file(per_book_dir / path.name)
            ]
        )
        if failed_dir.exists()
        else 0
    )
    aggregate_done = ready_count(project / "02_source_analysis", SOURCE_AGGREGATE_FILES, 500)
    outline_done = ready_count(project / "03_new_novel" / "outline_candidates", OUTLINE_CANDIDATE_FILES, 500)
    draft_batches, draft_chapter = draft_chapter_progress(drafts)
    target_chapters = planned_chapter_count(project, config, draft_chapter)
    if target_chapters <= 0:
        target_chapters = max(draft_chapter, int(state.get("current_chapter") or 1), 1)
    blueprint_done = blueprint_ready_count(project, target_chapters)
    chapter_plan_done, chapter_plan_continuous = chapter_plan_parts_progress(project, target_chapters)
    story_outline_ready = ready_file(project / "03_new_novel" / "full_story_outline.md", 500)
    chapter_target_text = str(target_chapters) if story_outline_ready else f"预估 {target_chapters}"

    phase = state.get("phase") or "new"
    awaiting = state.get("awaiting") or {}
    awaiting_type = awaiting.get("type")
    activity = state.get("activity") or ""
    is_revising = any(
        marker in activity
        for marker in (
            "开始应用节点微调",
            "微调调用模型中",
            "微调模型已返回",
            "正在解析并写入文件",
        )
    )

    imported = books > 0
    per_book_complete = imported and per_book_done >= books
    aggregate_complete = aggregate_done >= len(SOURCE_AGGREGATE_FILES)
    outline_complete = outline_done >= len(OUTLINE_CANDIDATE_FILES)
    blueprint_complete = blueprint_done >= len(BLUEPRINT_FILES)
    outline_selected = bool(state.get("selected_outline_candidate"))
    writing_started = draft_batches > 0 or phase == "writing"
    writing_complete = phase == "completed"
    blueprint_approved = phase in {"writing", "completed"} and not awaiting

    if is_revising:
        percent = 70
        label = "正在应用微调"
        eta = "模型正在按你的要求重写融合设定包；完成后会写入文件并生成微调效果报告。"
    elif writing_complete:
        percent = 100
        label = "已完成本轮目标"
        eta = "本轮自动流程已经完成。"
    elif awaiting:
        base = (
            10 + int(28 * per_book_done / max(books, 1))
            if awaiting_type == "source_failures"
            else 55
            if awaiting_type == "outline_selection"
            else 70
            if awaiting_type == "blueprint"
            else 72
        )
        writing_part = int(25 * min(draft_chapter, target_chapters) / max(target_chapters, 1))
        percent = min(96, base + writing_part)
        if awaiting_type == "source_failures":
            label = "源书拆解缺失，等待重试"
        elif awaiting_type == "outline_selection":
            label = "等待选择候选大纲"
        elif awaiting_type == "blueprint":
            label = "等待确认融合设定包"
        else:
            label = "等待确认正文批次"
        eta = (
            "现在没有在跑模型；点“启动 / 继续”后只重试尚未拆解成功的源书。"
            if awaiting_type == "source_failures"
            else "现在没有在跑模型，等你确认后才会继续消耗额度。"
        )
    elif imported and not per_book_complete:
        percent = 10 + int(28 * per_book_done / max(books, 1))
        label = "源书拆解不完整，需先补拆"
        eta = "检测到还有源书未完成拆解；继续或推翻重建后，会先补齐源书再生成强融合大纲。"
    elif phase == "writing":
        percent = min(96, 70 + int(25 * min(draft_chapter, target_chapters) / max(target_chapters, 1)))
        if draft_chapter <= 0:
            label = "已确认，待启动正文"
            eta = "设定包和完整章纲已经确认通过；点击“启动 / 继续”后会从第 1 章开始生成正文。"
        else:
            label = "正文写作中"
            eta = "正文会按章节和批次推进，每章完成后都会更新日志。"
    elif blueprint_complete:
        percent = 68
        label = "融合设定包已生成"
        eta = "下一步会进入确认节点。"
    elif aggregate_complete and not outline_selected and not outline_complete:
        percent = 50 + int(5 * outline_done / max(len(OUTLINE_CANDIDATE_FILES), 1))
        label = "生成候选大纲"
        eta = "会连续生成 3 套候选大纲，完成后暂停等你选择。"
    elif aggregate_complete or phase == "decomposed":
        percent = 50 + int(15 * blueprint_done / max(len(BLUEPRINT_FILES), 1))
        if outline_selected:
            if chapter_plan_done:
                percent = min(69, percent + int(8 * chapter_plan_continuous / max(target_chapters, 1)))
                label = "生成全文章纲"
                eta = f"逐段生成中：已保存 {chapter_plan_done}/{target_chapters} 章，连续推进到第 {chapter_plan_continuous} 章。"
            else:
                label = "生成融合设定包"
                eta = "已选择候选大纲，正在逐个生成完整设定文件。"
        else:
            label = "等待选择候选大纲" if outline_complete else "生成候选大纲"
            eta = "候选大纲完成后，需要先选择一套，再生成完整设定包。"
    elif per_book_complete:
        percent = 40 + int(10 * aggregate_done / max(len(SOURCE_AGGREGATE_FILES), 1))
        label = "聚合源书素材池"
        eta = "素材池会逐个生成 7 个文件，避免一次长输出被截断。"
    elif imported:
        percent = 10 + int(28 * per_book_done / max(books, 1))
        label = "拆解源书素材"
        eta = "源书拆解支持并发；失败书会自动重试，全部成功后才进入融合。"
    else:
        percent = 0
        label = "等待导入源书"
        eta = "先选择源书目录并启动流程。"

    percent = max(0, min(100, percent))
    selected_text = f"，已选 {state.get('selected_outline_candidate')}" if outline_selected else ""
    stale_source_suffix = "（待源书补齐后重建）" if imported and not per_book_complete else ""
    detail = (
        f"源书 {books} 本 / {source_files} 文件；"
        f"单书拆解 {per_book_done}/{books or 0}"
        f"{f'，失败 {per_book_failed}' if per_book_failed else ''}；"
        f"素材池 {aggregate_done}/{len(SOURCE_AGGREGATE_FILES)}{stale_source_suffix}；"
        f"候选大纲 {outline_done}/{len(OUTLINE_CANDIDATE_FILES)}{stale_source_suffix}"
        f"{selected_text}；"
        f"设定包 {blueprint_done}/{len(BLUEPRINT_FILES)}；"
        f"章纲 {chapter_plan_done}/{target_chapters}（连续到 {chapter_plan_continuous}）；"
        f"正文 {draft_chapter}/{chapter_target_text} 章。"
    )

    steps = [
        progress_step("源书导入", imported, f"{books} 本 / {source_files} 文件", not imported),
        progress_step(
            "单书拆解",
            per_book_complete,
            f"{per_book_done}/{books or 0}" + (f"，失败 {per_book_failed}" if per_book_failed else ""),
            imported and not per_book_complete,
        ),
        progress_step(
            "素材池聚合",
            per_book_complete and aggregate_complete,
            f"{aggregate_done}/{len(SOURCE_AGGREGATE_FILES)}{stale_source_suffix}",
            per_book_complete and not aggregate_complete,
        ),
        progress_step(
            "候选大纲",
            per_book_complete and outline_complete and outline_selected,
            f"{outline_done}/{len(OUTLINE_CANDIDATE_FILES)}"
            + stale_source_suffix
            + (f"，已选 {state.get('selected_outline_candidate')}" if outline_selected else ""),
            per_book_complete and aggregate_complete and not outline_selected,
        ),
        progress_step(
            "融合设定包",
            blueprint_complete and not is_revising,
            f"{blueprint_done}/{len(BLUEPRINT_FILES)}",
            (aggregate_complete and outline_selected and not blueprint_complete) or is_revising,
        ),
        progress_step(
            "完整章纲",
            chapter_plan_complete(project / "03_new_novel" / "chapter_plan.csv", target_chapters),
            f"{chapter_plan_done}/{target_chapters}，连续到 {chapter_plan_continuous}",
            outline_selected and chapter_plan_done > 0 and chapter_plan_continuous < target_chapters,
        ),
        progress_step(
            "确认节点",
            blueprint_approved and blueprint_complete and not is_revising,
            "微调中"
            if is_revising
            else "已通过"
            if blueprint_approved
            else ("待确认" if awaiting and awaiting_type != "source_failures" else "未到达"),
            bool(awaiting) and awaiting_type != "source_failures",
        ),
        progress_step("正文写作", writing_complete, f"{draft_chapter}/{chapter_target_text} 章", writing_started and not writing_complete),
    ]

    return {
        "percent": percent,
        "label": label,
        "detail": detail,
        "eta": eta,
        "steps": steps,
    }


def project_status(project_raw: str) -> dict[str, Any]:
    project = Path(project_raw).expanduser().resolve()
    config_path = project / "00_config" / "project.json"
    if not config_path.exists():
        raise ValueError(f"项目不存在或不是有效项目：{project}")
    config = load_json(config_path)
    state_path = project / "00_config" / "agent_state.json"
    state = load_state_json(state_path)
    drafts = sorted((project / "05_drafts").glob("ch_*_to_*.md"))
    summaries = sorted((project / "06_summaries").glob("summary_*.md"))
    prompts = sorted((project / "04_prompts").glob("*.md"))
    source_index = project / "01_sources" / "source_index.json"
    source_info = load_json(source_index) if source_index.exists() else {}
    progress = project_progress(project, config, state, source_info, drafts)
    return {
        "project": str(project),
        "config": config,
        "state": state,
        "paths": {
            "project": str(project),
            "sources": str(project / "01_sources"),
            "analysis": str(project / "02_source_analysis"),
            "new_novel": str(project / "03_new_novel"),
            "outline_candidates": str(project / "03_new_novel" / "outline_candidates"),
            "drafts": str(project / "05_drafts"),
            "exports": str(project / "08_exports"),
        },
        "counts": {
            "books": source_info.get("book_count", 0),
            "source_files": source_info.get("file_count", 0),
            "draft_batches": len(drafts),
            "summaries": len(summaries),
            "prompts": len(prompts),
        },
        "progress": progress,
        "decision": decision_status(project, state, progress),
        "revision": latest_revision_status(project, state),
        "files": {
            "blueprint": [
                file_status(project, "03_new_novel/novel_bible.md", "新书总设定"),
                file_status(project, "03_new_novel/style_guide.md", "文风规则"),
                file_status(project, "03_new_novel/power_system.md", "力量体系"),
                file_status(project, "03_new_novel/worldbuilding.md", "世界观"),
                file_status(project, "03_new_novel/character_table.csv", "角色表"),
                file_status(project, "03_new_novel/volume_outline.md", "分卷大纲"),
                file_status(project, "03_new_novel/full_story_outline.md", "全书规划"),
                file_status(project, "03_new_novel/chapter_plan.csv", "章节规划"),
                file_status(project, "03_new_novel/memory_rollup.md", "长期记忆"),
            ]
        },
        "latest": {
            "draft": str(drafts[-1]) if drafts else "",
            "summary": str(summaries[-1]) if summaries else "",
        },
        "preview": {
            "novel_bible": read_optional(project / "03_new_novel" / "novel_bible.md", 2500),
            "memory": read_optional(project / "03_new_novel" / "memory_rollup.md", 1800),
        },
    }


def append_log(job: dict[str, Any], line: str) -> None:
    job["log"].append(line)
    if len(job["log"]) > 800:
        job["log"] = job["log"][-800:]


def run_job(command: list[str], env: dict[str, str] | None = None) -> str:
    job_id = str(int(time.time() * 1000))
    job = {"id": job_id, "status": "running", "command": command, "log": [], "started_at": time.time(), "returncode": None}
    JOBS[job_id] = job
    append_log(job, "任务已启动，正在等待后台步骤输出...")

    def worker() -> None:
        process_env = os.environ.copy()
        process_env.setdefault("PYTHONIOENCODING", "utf-8")
        process_env.setdefault("PYTHONUTF8", "1")
        process_env.setdefault("PYTHONUNBUFFERED", "1")
        if env:
            process_env.update({key: value for key, value in env.items() if value})
        try:
            process = subprocess.Popen(
                command,
                cwd=str(APP_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=process_env,
            )
            job["pid"] = process.pid
            JOB_PROCESSES[job_id] = process
            assert process.stdout is not None
            for line in process.stdout:
                append_log(job, line.rstrip())
            job["returncode"] = process.wait()
            job["status"] = "done" if job["returncode"] == 0 else "failed"
            append_log(job, f"任务结束：status={job['status']} returncode={job['returncode']}")
        except Exception as exc:  # noqa: BLE001 - surfaced in UI.
            append_log(job, f"启动失败：{exc}")
            job["status"] = "failed"
            job["returncode"] = -1
        finally:
            JOB_PROCESSES.pop(job_id, None)

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def command_kind(command: list[str]) -> str:
    for name in (
        "reset-rebuild",
        "select-outline",
        "continue-outline",
        "promote-trial",
        "trial-write",
        "power-rewrite",
        "revise",
        "autopilot",
        "test-llm",
        "approve",
    ):
        if name in command:
            return name
    return ""


def operation_label(kind: str) -> str:
    return {
        "revise": "正在微调",
        "autopilot": "自动流程运行中",
        "reset-rebuild": "推翻重建中",
        "select-outline": "正在选择大纲",
        "continue-outline": "正在补章纲",
        "promote-trial": "正在转正式正文",
        "trial-write": "正在试写正文",
        "power-rewrite": "正在回修战力境界",
        "test-llm": "正在测试模型",
        "approve": "正在确认节点",
    }.get(kind, "任务运行中")


def operation_progress(kind: str, job: dict[str, Any] | None, project: str = "") -> dict[str, Any]:
    logs = "\n".join(str(line) for line in (job or {}).get("log", []))
    elapsed = max(0, int(time.time() - float((job or {}).get("started_at") or time.time())))
    if kind == "revise":
        percent = 18
        label = "微调已启动"
        detail = "正在排队或等待模型响应。"
        if "微调调用模型中" in logs:
            percent = 45
            label = "微调处理中"
            detail = "模型正在重写融合设定包；这是微调里最慢的一段。"
        if "微调模型已返回" in logs:
            percent = 72
            label = "模型已返回"
            detail = "正在解析模型结果。"
        if "正在解析并写入文件" in logs or "写入" in logs:
            percent = 86
            label = "写入设定文件"
            detail = "正在把修改落到 03_new_novel 并生成微调报告。"
        if job and job.get("status") == "done":
            percent = 100
            label = "微调完成"
            detail = "可以查看“最近微调效果”，满意后点确认继续。"
        elif job and job.get("status") in {"failed", "cancelled"}:
            percent = 100
            label = "微调已停止" if job.get("status") == "cancelled" else "微调失败"
            detail = "请看运行日志里的最后几行错误。"
        return {
            "percent": percent,
            "label": label,
            "detail": detail,
            "eta": "通常 3-10 分钟；如果模型很慢可能到 15-20 分钟。" if elapsed < 900 else "已经超过 15 分钟，建议先看日志是否有 429/401 或网络错误。",
        }
    if kind == "power-rewrite":
        done = len(re.findall(r"完成战力境界回修：第\s*\d+\s*章", logs))
        percent = min(95, 8 + done * 6)
        label = "战力境界回修中"
        detail = f"已完成 {done} 章；只回修当前修为/等级、核心能力状态、关键道具权限、敌我差距、越级依据和代价，尽量不动剧情。"
        if job and job.get("status") == "done":
            percent = 100
            label = "战力境界回修完成"
            detail = "已写回正文批次文件，并保留了回修前备份。"
        elif job and job.get("status") in {"failed", "cancelled"}:
            percent = 100
            label = "战力境界回修已停止" if job.get("status") == "cancelled" else "战力境界回修失败"
            detail = "请看运行日志最后几行；已写回的批次会保留，未到的章节不会被改。"
        return {
            "percent": percent,
            "label": label,
            "detail": detail,
            "eta": "按章调用模型，3-150章会比较久；建议先跑3-10章样本，满意后再扩大范围。",
        }
    if kind in {"autopilot", "reset-rebuild", "select-outline"} and project:
        try:
            status = project_status(project)
            progress = status.get("progress") or {}
            return {
                "percent": int(progress.get("percent") or 0),
                "label": progress.get("label") or operation_label(kind),
                "detail": progress.get("detail") or "正在推进写作工作流。",
                "eta": progress.get("eta") or "",
            }
        except Exception:
            pass
    if kind == "approve":
        return {"percent": 45, "label": "正在确认节点", "detail": "正在记录确认并推进到下一步。", "eta": ""}
    if kind == "reset-rebuild":
        return {"percent": 35, "label": "推翻重建中", "detail": "正在归档旧产物、重新读取源书并生成新的设定包。", "eta": "这会重新跑源书拆解和融合设定，通常比普通微调更久。"}
    if kind == "test-llm":
        return {"percent": 50, "label": "正在测试模型", "detail": "正在向模型入口发送测试请求。", "eta": "通常几十秒内完成。"}
    return {"percent": 15, "label": operation_label(kind), "detail": "后台任务正在运行。", "eta": ""}


def running_job_id(command_name: str, project: str = "") -> str:
    for job_id, job in sorted(JOBS.items(), key=lambda item: item[1].get("started_at") or 0, reverse=True):
        if job.get("status") != "running":
            continue
        command = [str(part) for part in job.get("command") or []]
        if command_name not in command:
            continue
        if project and project not in command:
            continue
        return job_id
    return ""


def find_external_python_jobs(command_name: str = "", project: str = "") -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    filters = ["$_.Name -like 'python*'", "$_.CommandLine -match 'novel_agent'"]
    if command_name:
        filters.append(f"$_.CommandLine -match ' {re.escape(command_name)}( |$)'")
    if project:
        escaped_project = project.replace("'", "''")
        filters.append(f"$_.CommandLine -like '*{escaped_project}*'")
    script = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ {' -and '.join(filters)} }} | "
        "Select-Object ProcessId,CreationDate,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except Exception:
        return []
    text = result.stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    jobs = []
    for item in data:
        command = item.get("CommandLine") or ""
        if "-Command" in command and "Get-CimInstance" in command:
            continue
        jobs.append(
            {
                "pid": int(item.get("ProcessId") or 0),
                "created_at": item.get("CreationDate") or "",
                "command": command,
                "kind": command_name or ("revise" if " novel_agent revise" in command else "autopilot" if " novel_agent autopilot" in command else ""),
            }
        )
    return [job for job in jobs if job["pid"]]


def operation_status(project: str = "") -> dict[str, Any]:
    for job_id, job in sorted(JOBS.items(), key=lambda item: item[1].get("started_at") or 0, reverse=True):
        if job.get("status") != "running":
            continue
        command = [str(part) for part in job.get("command") or []]
        if project and project not in command:
            continue
        kind = command_kind(command)
        if not kind:
            continue
        label = operation_label(kind)
        return {
            "running": True,
            "job_id": job_id,
            "kind": kind,
            "label": label,
            "started_at": job.get("started_at"),
            "elapsed": max(0, int(time.time() - float(job.get("started_at") or time.time()))),
            "pid": job.get("pid"),
            "source": "job",
            "can_cancel": True,
            "progress": operation_progress(kind, job, project),
        }
    if not project:
        return {
            "running": False,
            "job_id": "",
            "kind": "",
            "label": "空闲",
            "elapsed": 0,
            "can_cancel": False,
            "progress": {"percent": 0, "label": "空闲", "detail": "当前没有后台任务。", "eta": ""},
        }
    for kind in (
        "reset-rebuild",
        "select-outline",
        "continue-outline",
        "promote-trial",
        "trial-write",
        "power-rewrite",
        "revise",
        "autopilot",
        "approve",
        "test-llm",
    ):
        external = find_external_python_jobs(kind, project)
        if external:
            job = sorted(external, key=lambda item: item.get("created_at") or "", reverse=True)[0]
            label = operation_label(kind)
            return {
                "running": True,
                "job_id": "",
                "kind": kind,
                "label": label,
                "started_at": "",
                "elapsed": 0,
                "pid": job.get("pid"),
                "source": "process",
                "can_cancel": True,
                "progress": operation_progress(kind, None, project),
            }
    return {
        "running": False,
        "job_id": "",
        "kind": "",
        "label": "空闲",
        "elapsed": 0,
        "can_cancel": False,
        "progress": {"percent": 0, "label": "空闲", "detail": "当前没有后台任务。", "eta": ""},
    }


def active_operation_for(project: str = "", kinds: set[str] | None = None) -> dict[str, Any]:
    operation = operation_status(project)
    if not operation.get("running"):
        return operation
    if kinds and operation.get("kind") not in kinds:
        return {"running": False, "job_id": "", "kind": "", "label": "空闲", "elapsed": 0, "can_cancel": False}
    return operation


def guard_operation(project: str = "", *, force_restart: bool = False, allowed_kinds: set[str] | None = None) -> dict[str, Any]:
    operation = active_operation_for(project, allowed_kinds)
    if not operation.get("running"):
        return operation
    if force_restart:
        cancel_operation(
            job_id=str(operation.get("job_id") or ""),
            project=project,
            kind=str(operation.get("kind") or ""),
        )
        return {"running": False, "job_id": "", "kind": "", "label": "空闲", "elapsed": 0, "can_cancel": False}
    return operation


def kill_process_tree(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, timeout=15, check=False)
        return True
    try:
        os.kill(pid, 15)
        return True
    except OSError:
        return False


def cancel_operation(job_id: str = "", project: str = "", kind: str = "") -> dict[str, Any]:
    cancelled: list[int] = []
    if not job_id and not kind:
        return {"ok": False, "cancelled": [], "error": "缺少要停止的任务类型"}
    if job_id and job_id in JOBS:
        job = JOBS[job_id]
        process = JOB_PROCESSES.get(job_id)
        pid = int(job.get("pid") or (process.pid if process else 0))
        if kill_process_tree(pid):
            cancelled.append(pid)
        job["status"] = "cancelled"
        job["returncode"] = -9
        append_log(job, "任务已手动停止。")
    for item in find_external_python_jobs(kind, project):
        pid = int(item.get("pid") or 0)
        if pid and pid not in cancelled and kill_process_tree(pid):
            cancelled.append(pid)
    return {"ok": True, "cancelled": cancelled}


def latest_job() -> dict[str, Any]:
    if not JOBS:
        external = detect_external_autopilot_job()
        return external or {"status": "missing", "log": []}
    return max(JOBS.values(), key=lambda item: item.get("started_at") or 0)


def detect_external_autopilot_job() -> dict[str, Any] | None:
    if os.name != "nt":
        return None
    script = (
        "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
        "Where-Object { $_.CommandLine -match 'novel_agent autopilot' } | "
        "Sort-Object CreationDate -Descending | "
        "Select-Object -First 1 ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except Exception:
        return None
    text = result.stdout.strip()
    if not text:
        return None
    try:
        process = json.loads(text)
    except json.JSONDecodeError:
        return None
    pid = process.get("ProcessId")
    command = process.get("CommandLine") or ""
    if not pid:
        return None
    return {
        "id": f"process-{pid}",
        "status": "running",
        "command": [command],
        "log": [
            "检测到刷新前已经在跑的后台 autopilot 进程。",
            "这个进程的实时 stdout 日志无法在刷新后重新接上；请看项目状态和文件更新时间判断进度。",
            f"后台进程 PID：{pid}",
        ],
        "started_at": time.time(),
        "returncode": None,
    }


def python_cmd() -> list[str]:
    venv_python = APP_ROOT / ".venv" / "Scripts" / "python.exe"
    executable = str(venv_python) if venv_python.exists() else sys.executable
    return [executable, "-m", "novel_agent"]


class UIHandler(BaseHTTPRequestHandler):
    server_version = "WebNovelAgentUI/0.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003 - inherited name.
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib API.
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                text_response(self, INDEX_HTML)
            elif parsed.path == "/api/providers":
                json_response(self, {"providers": PROVIDERS})
            elif parsed.path == "/api/profiles":
                json_response(self, {"profiles": load_profiles()})
            elif parsed.path == "/api/projects":
                json_response(self, {"projects": list_projects()})
            elif parsed.path == "/api/status":
                project = query.get("project", [""])[0]
                json_response(self, project_status(project))
            elif parsed.path == "/api/key-pool/latest":
                provider = query.get("provider", ["sub2api"])[0]
                key_file = provider_key_file(provider) if provider != "sub2api" else latest_key_pool_file()
                if not key_file:
                    json_response(self, {"keys": "", "count": 0, "path": ""})
                else:
                    text = key_file.read_text(encoding="utf-8-sig")
                    json_response(self, {"keys": text, "count": len(split_api_keys(text)), "path": str(key_file)})
            elif parsed.path == "/api/job":
                job_id = query.get("id", [""])[0]
                json_response(self, JOBS.get(job_id, {"status": "missing", "log": []}))
            elif parsed.path == "/api/jobs/latest":
                json_response(self, latest_job())
            elif parsed.path == "/api/operation":
                project = query.get("project", [""])[0]
                json_response(self, operation_status(project))
            else:
                self.send_error(404)
        except Exception as exc:  # noqa: BLE001
            json_response(self, {"error": str(exc)}, status=500)

    def do_POST(self) -> None:  # noqa: N802 - stdlib API.
        parsed = urllib.parse.urlparse(self.path)
        try:
            data = read_body(self)
            if parsed.path == "/api/open-provider":
                provider = data.get("provider", "")
                if provider not in PROVIDERS:
                    raise ValueError("未知平台")
                webbrowser.open(PROVIDERS[provider]["url"])
                json_response(self, {"ok": True, "url": PROVIDERS[provider]["url"]})
            elif parsed.path == "/api/autopilot":
                json_response(self, {"job_id": self._start_autopilot(data)})
            elif parsed.path == "/api/reset-rebuild":
                json_response(self, {"job_id": self._reset_rebuild(data)})
            elif parsed.path == "/api/reset-writing":
                json_response(self, self._reset_writing(data))
            elif parsed.path == "/api/approve":
                project = data.get("project", "")
                note = data.get("note", "")
                operation = guard_operation(
                    project,
                    force_restart=bool(data.get("force_restart")),
                    allowed_kinds={"reset-rebuild", "select-outline", "continue-outline", "promote-trial", "trial-write", "power-rewrite", "autopilot", "revise", "approve"},
                )
                if operation.get("running"):
                    job_id = str(operation.get("job_id") or "")
                    if not job_id:
                        raise ValueError(f"{operation.get('label')}，请先停止当前任务再继续。")
                    json_response(self, {"job_id": job_id, "operation": operation})
                    return
                command = python_cmd() + ["approve", "--project", project]
                if note:
                    command += ["--note", note]
                json_response(self, {"job_id": run_job(command)})
            elif parsed.path == "/api/select-outline":
                project = data.get("project", "")
                candidate = int(data.get("candidate") or 0)
                operation = guard_operation(
                    project,
                    force_restart=bool(data.get("force_restart")),
                    allowed_kinds={"reset-rebuild", "select-outline", "continue-outline", "promote-trial", "trial-write", "power-rewrite", "autopilot", "revise", "approve"},
                )
                if operation.get("running"):
                    job_id = str(operation.get("job_id") or "")
                    if not job_id:
                        raise ValueError(f"{operation.get('label')}，请先停止当前任务再选择。")
                    json_response(self, {"job_id": job_id, "operation": operation})
                    return
                command = python_cmd() + ["select-outline", "--project", project, "--candidate", str(candidate)]
                json_response(self, {"job_id": run_job(command)})
            elif parsed.path == "/api/revise":
                json_response(self, {"job_id": self._revise_checkpoint(data)})
            elif parsed.path == "/api/trial-write":
                json_response(self, {"job_id": self._trial_write(data)})
            elif parsed.path == "/api/promote-trial":
                json_response(self, {"job_id": self._promote_trial(data)})
            elif parsed.path == "/api/continue-outline":
                json_response(self, {"job_id": self._continue_outline(data)})
            elif parsed.path == "/api/power-rewrite":
                json_response(self, {"job_id": self._power_rewrite(data)})
            elif parsed.path == "/api/install-tools":
                command = [str(APP_ROOT / "install_tools.bat")]
                json_response(self, {"job_id": run_job(command)})
            elif parsed.path == "/api/start-sub2api":
                script = APP_ROOT.parent / "sub2api-local" / "start.ps1"
                if not script.exists():
                    raise ValueError(f"没有找到 Sub2API 启动脚本：{script}")
                command = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)]
                json_response(self, {"job_id": run_job(command)})
            elif parsed.path == "/api/test-llm":
                json_response(self, {"job_id": self._test_llm(data)})
            elif parsed.path == "/api/cancel-job":
                json_response(
                    self,
                    cancel_operation(
                        job_id=data.get("job_id", "").strip(),
                        project=data.get("project", "").strip(),
                        kind=data.get("kind", "").strip(),
                    ),
                )
            elif parsed.path == "/api/open-path":
                target = data.get("path", "")
                if not target:
                    raise ValueError("缺少 path")
                os.startfile(str(Path(target).expanduser()))  # type: ignore[attr-defined]
                json_response(self, {"ok": True})
            else:
                self.send_error(404)
        except Exception as exc:  # noqa: BLE001
            json_response(self, {"error": str(exc)}, status=500)

    def _start_autopilot(self, data: dict[str, Any]) -> str:
        project = data.get("project", "").strip()
        operation = guard_operation(
            project,
            force_restart=bool(data.get("force_restart")),
            allowed_kinds={"reset-rebuild", "select-outline", "continue-outline", "promote-trial", "trial-write", "power-rewrite", "autopilot", "revise", "approve"},
        )
        if operation.get("running"):
            job_id = str(operation.get("job_id") or "")
            if not job_id:
                raise ValueError(f"{operation.get('label')}，请先停止当前任务再启动。")
            return job_id

        source_mode = "deep"
        data["source_mode"] = source_mode
        command = python_cmd() + ["autopilot"]
        if project:
            command += ["--project", project]
            source_dir = data.get("source_dir", "").strip()
            if source_dir:
                command += ["--source-dir", source_dir]
        else:
            command += [
                "--name",
                data.get("name", "").strip() or "my-novel",
                "--platform",
                data.get("platform", "feilu"),
                "--source-dir",
                data.get("source_dir", "").strip(),
            ]
            project_dir = data.get("project_dir", "").strip()
            if project_dir:
                command += ["--project-dir", project_dir]
        provider = data.get("provider", "openai")
        base_url = data.get("base_url", "").strip()
        if provider == "sub2api" and not base_url:
            base_url = DEFAULT_SUB2API_BASE_URL
        command += [
            "--provider",
            provider,
            "--engine",
            data.get("engine", "langgraph"),
            "--max-chapters",
            str(int(data.get("max_chapters") or 0)),
            "--batch-size",
            str(int(data.get("batch_size") or 30)),
            "--source-mode",
            source_mode,
            "--fusion-mode",
            data.get("fusion_mode", "licensed"),
            "--source-workers",
            str(int(data.get("source_workers") or 1)),
            "--source-timeout",
            str(source_timeout_for_mode(data)),
            "--source-retries",
            str(int(data.get("source_retries") or 3)),
        ]
        if provider == "sub2api":
            command += ["--retries", "6", "--timeout", "1200"]
        model = data.get("model", "").strip()
        if model:
            command += ["--model", model]
        if base_url:
            command += ["--base-url", base_url]
        if data.get("no_checkpoints"):
            command += ["--no-checkpoints"]
        if data.get("stream_preview", True):
            command += ["--stream-preview"]

        key_file = key_file_for_provider(provider, data)
        if key_file:
            command += ["--api-keys-file", str(key_file)]

        env = {} if key_file else key_env_for_provider(provider, data)
        return run_job(command, env=env)

    def _revise_checkpoint(self, data: dict[str, Any]) -> str:
        project = data.get("project", "").strip()
        note = data.get("note", "").strip()
        if not project:
            raise ValueError("缺少项目路径")
        if not note:
            raise ValueError("请先填写微调要求")
        operation = guard_operation(
            project,
            force_restart=bool(data.get("force_restart")),
            allowed_kinds={"reset-rebuild", "select-outline", "continue-outline", "promote-trial", "trial-write", "power-rewrite", "autopilot", "revise", "approve"},
        )
        if operation.get("running"):
            job_id = str(operation.get("job_id") or "")
            if not job_id:
                raise ValueError(f"{operation.get('label')}，请先停止当前任务再微调。")
            return job_id
        provider = data.get("provider", "sub2api")
        base_url = data.get("base_url", "").strip()
        if provider == "sub2api" and not base_url:
            base_url = DEFAULT_SUB2API_BASE_URL
        command = python_cmd() + [
            "revise",
            "--project",
            project,
            "--note",
            note,
            "--provider",
            provider,
            "--fusion-mode",
            data.get("fusion_mode", "strong"),
        ]
        if provider == "sub2api":
            command += ["--retries", "6", "--timeout", "1200"]
        model = data.get("model", "").strip()
        if model:
            command += ["--model", model]
        if base_url:
            command += ["--base-url", base_url]
        if data.get("stream_preview", True):
            command += ["--stream-preview"]

        key_file = key_file_for_provider(provider, data)
        if key_file:
            command += ["--api-keys-file", str(key_file)]
        env = {} if key_file else key_env_for_provider(provider, data)
        return run_job(command, env=env)

    def _power_rewrite(self, data: dict[str, Any]) -> str:
        project = data.get("project", "").strip()
        if not project:
            raise ValueError("缺少项目路径")
        start = max(1, int(data.get("start") or 1))
        end = int(data.get("end") or 150)
        if end < start:
            raise ValueError("回修结束章节不能小于开始章节")
        operation = guard_operation(
            project,
            force_restart=bool(data.get("force_restart")),
            allowed_kinds={"reset-rebuild", "select-outline", "continue-outline", "promote-trial", "trial-write", "power-rewrite", "autopilot", "revise", "approve"},
        )
        if operation.get("running"):
            job_id = str(operation.get("job_id") or "")
            if not job_id:
                raise ValueError(f"{operation.get('label')}，请先停止当前任务再回修战力境界。")
            return job_id
        provider = data.get("provider", "sub2api")
        base_url = data.get("base_url", "").strip()
        if provider == "sub2api" and not base_url:
            base_url = DEFAULT_SUB2API_BASE_URL
        command = python_cmd() + [
            "power-rewrite",
            "--project",
            project,
            "--start",
            str(start),
            "--end",
            str(end),
            "--provider",
            provider,
        ]
        if provider == "sub2api":
            command += ["--retries", "6", "--timeout", "1800"]
        model = data.get("model", "").strip()
        if model:
            command += ["--model", model]
        if base_url:
            command += ["--base-url", base_url]
        if data.get("stream_preview", True):
            command += ["--stream-preview"]

        key_file = key_file_for_provider(provider, data)
        if key_file:
            command += ["--api-keys-file", str(key_file)]
        env = {} if key_file else key_env_for_provider(provider, data)
        return run_job(command, env=env)

    def _trial_write(self, data: dict[str, Any]) -> str:
        project = data.get("project", "").strip()
        if not project:
            raise ValueError("缺少项目路径")
        start = max(1, int(data.get("start") or 1))
        end = int(data.get("end") or start)
        if end < start:
            raise ValueError("试写结束章节不能小于开始章节")
        if end - start + 1 > 10:
            raise ValueError("试写一次最多 10 章，建议先试 1-3 章。")
        operation = guard_operation(
            project,
            force_restart=bool(data.get("force_restart")),
            allowed_kinds={"reset-rebuild", "select-outline", "continue-outline", "promote-trial", "trial-write", "power-rewrite", "autopilot", "revise", "approve"},
        )
        if operation.get("running"):
            job_id = str(operation.get("job_id") or "")
            if not job_id:
                raise ValueError(f"{operation.get('label')}，请先停止当前任务再试写正文。")
            return job_id
        provider = data.get("provider", "sub2api")
        base_url = data.get("base_url", "").strip()
        if provider == "sub2api" and not base_url:
            base_url = DEFAULT_SUB2API_BASE_URL
        command = python_cmd() + [
            "trial-write",
            "--project",
            project,
            "--start",
            str(start),
            "--end",
            str(end),
            "--provider",
            provider,
        ]
        if provider == "sub2api":
            command += ["--retries", "6", "--timeout", "1800"]
        model = data.get("model", "").strip()
        if model:
            command += ["--model", model]
        if base_url:
            command += ["--base-url", base_url]
        if data.get("stream_preview", True):
            command += ["--stream-preview"]

        key_file = key_file_for_provider(provider, data)
        if key_file:
            command += ["--api-keys-file", str(key_file)]
        env = {} if key_file else key_env_for_provider(provider, data)
        return run_job(command, env=env)

    def _promote_trial(self, data: dict[str, Any]) -> str:
        project = data.get("project", "").strip()
        if not project:
            raise ValueError("缺少项目路径")
        start = max(1, int(data.get("start") or 1))
        end = int(data.get("end") or start)
        if end < start:
            raise ValueError("转正式结束章节不能小于开始章节")
        operation = guard_operation(
            project,
            force_restart=bool(data.get("force_restart")),
            allowed_kinds={"reset-rebuild", "select-outline", "continue-outline", "promote-trial", "trial-write", "power-rewrite", "autopilot", "revise", "approve"},
        )
        if operation.get("running"):
            job_id = str(operation.get("job_id") or "")
            if not job_id:
                raise ValueError(f"{operation.get('label')}，请先停止当前任务再转正式正文。")
            return job_id
        command = python_cmd() + [
            "promote-trial",
            "--project",
            project,
            "--start",
            str(start),
            "--end",
            str(end),
        ]
        return run_job(command)

    def _continue_outline(self, data: dict[str, Any]) -> str:
        project = data.get("project", "").strip()
        if not project:
            raise ValueError("缺少项目路径")
        until = max(1, int(data.get("until") or 0))
        if until < 1:
            raise ValueError("补章纲目标章节不能小于 1")
        operation = guard_operation(
            project,
            force_restart=bool(data.get("force_restart")),
            allowed_kinds={"reset-rebuild", "select-outline", "continue-outline", "promote-trial", "trial-write", "power-rewrite", "autopilot", "revise", "approve"},
        )
        if operation.get("running"):
            job_id = str(operation.get("job_id") or "")
            if not job_id:
                raise ValueError(f"{operation.get('label')}，请先停止当前任务再继续补章纲。")
            return job_id
        provider = data.get("provider", "sub2api")
        base_url = data.get("base_url", "").strip()
        if provider == "sub2api" and not base_url:
            base_url = DEFAULT_SUB2API_BASE_URL
        command = python_cmd() + [
            "continue-outline",
            "--project",
            project,
            "--until",
            str(until),
            "--provider",
            provider,
            "--fusion-mode",
            data.get("fusion_mode", "licensed"),
        ]
        if provider == "sub2api":
            command += ["--retries", "6", "--timeout", "1800"]
        model = data.get("model", "").strip()
        if model:
            command += ["--model", model]
        if base_url:
            command += ["--base-url", base_url]
        if data.get("stream_preview", True):
            command += ["--stream-preview"]

        key_file = key_file_for_provider(provider, data)
        if key_file:
            command += ["--api-keys-file", str(key_file)]
        env = {} if key_file else key_env_for_provider(provider, data)
        return run_job(command, env=env)

    def _reset_rebuild(self, data: dict[str, Any]) -> str:
        project = data.get("project", "").strip()
        if not project:
            raise ValueError("缺少项目路径")
        operation = guard_operation(
            project,
            force_restart=bool(data.get("force_restart")),
            allowed_kinds={"reset-rebuild", "select-outline", "continue-outline", "promote-trial", "trial-write", "power-rewrite", "autopilot", "revise", "approve"},
        )
        if operation.get("running"):
            job_id = str(operation.get("job_id") or "")
            if not job_id:
                raise ValueError(f"{operation.get('label')}，请先停止当前任务再推翻重建。")
            return job_id

        provider = data.get("provider", "sub2api")
        base_url = data.get("base_url", "").strip()
        if provider == "sub2api" and not base_url:
            base_url = DEFAULT_SUB2API_BASE_URL
        command = python_cmd() + [
            "reset-rebuild",
            "--project",
            project,
            "--provider",
            provider,
            "--engine",
            data.get("engine", "langgraph"),
            "--max-chapters",
            str(int(data.get("max_chapters") or 0)),
            "--batch-size",
            str(int(data.get("batch_size") or 30)),
            "--source-mode",
            data.get("source_mode", "deep"),
            "--fusion-mode",
            data.get("fusion_mode", "licensed"),
            "--source-workers",
            str(int(data.get("source_workers") or 1)),
            "--source-timeout",
            str(source_timeout_for_mode(data)),
            "--source-retries",
            str(int(data.get("source_retries") or 3)),
        ]
        if provider == "sub2api":
            command += ["--retries", "6", "--timeout", "1200"]
        model = data.get("model", "").strip()
        if model:
            command += ["--model", model]
        if base_url:
            command += ["--base-url", base_url]
        if data.get("stream_preview", True):
            command += ["--stream-preview"]

        key_file = key_file_for_provider(provider, data)
        if key_file:
            command += ["--api-keys-file", str(key_file)]
        env = {} if key_file else key_env_for_provider(provider, data)
        return run_job(command, env=env)

    def _reset_writing(self, data: dict[str, Any]) -> dict[str, str]:
        project = data.get("project", "").strip()
        if not project:
            raise ValueError("缺少项目路径")
        operation = guard_operation(
            project,
            force_restart=bool(data.get("force_restart")),
            allowed_kinds={"reset-rebuild", "select-outline", "continue-outline", "promote-trial", "trial-write", "power-rewrite", "autopilot", "revise", "approve"},
        )
        if operation.get("running"):
            raise ValueError(f"{operation.get('label')}，请先停止当前任务再清空正文重写。")
        backup = reset_project_writing_only(Path(project).expanduser().resolve())
        return {
            "backup": str(backup),
            "basis": str(Path(project).expanduser().resolve() / "00_config" / "rewrite_basis.json"),
            "message": "已保留源书拆解、素材池、新版设定包、800章章纲和力量体系；已归档正文/摘要/审稿/导出/提示词，以及旧稿衍生的角色当前状态、伏笔台账和风险提醒。现在点“启动 / 继续”会从第0001章按当前新版设定重写正文。",
        }

    def _test_llm(self, data: dict[str, Any]) -> str:
        provider = data.get("provider", "sub2api")
        model = data.get("model", "").strip()
        base_url = data.get("base_url", "").strip()
        if provider == "sub2api" and not base_url:
            base_url = DEFAULT_SUB2API_BASE_URL
        command = python_cmd() + ["test-llm", "--provider", provider]
        if model:
            command += ["--model", model]
        if base_url:
            command += ["--base-url", base_url]
        key_file = key_file_for_provider(provider, data)
        if key_file:
            command += ["--api-keys-file", str(key_file)]
        env = {} if key_file else key_env_for_provider(provider, data)
        return run_job(command, env=env)


def run_server(port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer((HOST, port), UIHandler)
    url = f"http://{HOST}:{port}"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"WebNovel Agent 控制台已启动：{url}")
    print("按 Ctrl+C 停止。")
    server.serve_forever()


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>网文 Agent 控制台</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101114;
      --panel: #181a20;
      --panel-2: #20232b;
      --line: #333845;
      --text: #f2f4f8;
      --muted: #aab0bd;
      --accent: #39c27f;
      --accent-2: #5aa9ff;
      --danger: #ff6961;
      --warn: #f2b84b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      font-size: 14px;
    }
    .app {
      display: grid;
      grid-template-columns: 320px 1fr;
      min-height: 100vh;
    }
    aside {
      border-right: 1px solid var(--line);
      background: #13151a;
      padding: 18px;
      overflow: auto;
    }
    main {
      padding: 18px 22px 28px;
      overflow: auto;
    }
    h1, h2, h3 { margin: 0; letter-spacing: 0; }
    h1 { font-size: 21px; margin-bottom: 4px; }
    h2 { font-size: 16px; margin-bottom: 12px; }
    h3 { font-size: 14px; margin-bottom: 8px; color: var(--muted); }
    .sub { color: var(--muted); line-height: 1.45; margin-bottom: 18px; }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin-bottom: 14px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }
    .grid-3 {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    label {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin: 10px 0 6px;
    }
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line);
      background: #111318;
      color: var(--text);
      border-radius: 6px;
      padding: 9px 10px;
      outline: none;
    }
    textarea { min-height: 86px; resize: vertical; }
    button, a.button {
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--text);
      border-radius: 6px;
      padding: 9px 11px;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
      min-height: 36px;
    }
    button.primary { background: #1c6f48; border-color: #2ea76d; }
    button.blue { background: #164b7d; border-color: #2b78c2; }
    button.warn { background: #77551c; border-color: #b7822e; }
    button.danger { background: #73302d; border-color: #a94b45; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .check-row {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      width: auto;
      margin: 10px 0 0;
      color: var(--muted);
      font-size: 12px;
    }
    .check-row input { width: auto; }
    .project-list { display: grid; gap: 8px; }
    .project {
      width: 100%;
      justify-content: flex-start;
      text-align: left;
      display: block;
      line-height: 1.35;
    }
    .project small { display: block; color: var(--muted); overflow-wrap: anywhere; margin-top: 4px; }
    .kpis { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; }
    .kpi {
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
    }
    .kpi strong { font-size: 21px; display: block; margin-bottom: 4px; }
    .kpi span { color: var(--muted); font-size: 12px; }
    .progress-card, .operation-card {
      margin-top: 12px;
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
    }
    .operation-card {
      border-color: #2b78c2;
      background: #111722;
    }
    .progress-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 9px;
    }
    .progress-head strong { font-size: 14px; }
    .progress-head span { color: var(--accent); font-weight: 700; }
    .progress-bar {
      height: 12px;
      border-radius: 999px;
      border: 1px solid #2d3442;
      background: #0e1014;
      overflow: hidden;
    }
    .progress-fill {
      width: 0%;
      height: 100%;
      border-radius: 999px;
      background: linear-gradient(90deg, #2ea76d, #2b78c2);
      transition: width .35s ease;
    }
    .operation-fill { background: linear-gradient(90deg, #b7822e, #2b78c2); }
    .steps {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
      margin-top: 10px;
    }
    .step {
      min-height: 54px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px;
      background: #111318;
      color: var(--muted);
    }
    .step strong {
      display: block;
      color: #d7dce6;
      font-size: 12px;
      margin-bottom: 4px;
    }
    .step span { font-size: 12px; }
    .step em {
      display: inline-flex;
      margin-top: 6px;
      color: var(--warn);
      font-style: normal;
      font-size: 11px;
      font-weight: 700;
    }
    .step.done { border-color: #27764f; }
    .step.current { border-color: #876529; }
    .step.done strong { color: var(--accent); }
    .step.current strong { color: var(--warn); }
    .decision-card, .path-card, .revision-card {
      margin-top: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #111318;
      padding: 12px;
    }
    .decision-card {
      border-color: #876529;
      background: #171511;
    }
    .decision-card h3, .revision-card h3, .path-card h3 {
      color: var(--text);
      margin-bottom: 6px;
    }
    .decision-card p, .revision-card p, .path-card p {
      margin: 0;
      color: #d7dce6;
      line-height: 1.55;
    }
    .path-line {
      display: grid;
      grid-template-columns: 92px 1fr;
      gap: 10px;
      align-items: start;
      margin: 8px 0;
    }
    .path-line span { color: var(--muted); font-size: 12px; padding-top: 2px; }
    code.path {
      display: block;
      overflow-wrap: anywhere;
      color: #cfe5ff;
      background: #0e1014;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 8px;
      line-height: 1.35;
    }
    .file-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-top: 10px;
    }
    .file-chip {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px;
      background: var(--panel-2);
      min-height: 62px;
    }
    .file-chip strong {
      display: block;
      font-size: 12px;
      color: #e5e9f2;
      margin-bottom: 4px;
    }
    .file-chip small {
      display: block;
      color: var(--muted);
      overflow-wrap: anywhere;
      line-height: 1.35;
    }
    .file-chip .delta { color: var(--accent); }
    .file-chip.missing { opacity: .55; }
    .status {
      display: inline-flex;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 10px;
      color: var(--muted);
      background: #111318;
    }
    .status.wait { color: var(--warn); border-color: #876529; }
    .status.run { color: var(--accent); border-color: #27764f; }
    .status.busy { color: #8cc8ff; border-color: #2b78c2; }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      background: #0e1014;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      max-height: 360px;
      overflow: auto;
      line-height: 1.5;
      color: #d7dce6;
    }
    .split { display: grid; grid-template-columns: 1.2fr .8fr; gap: 14px; }
    .hint { color: var(--muted); font-size: 12px; line-height: 1.5; margin-top: 8px; }
    .error { color: var(--danger); }
    @media (max-width: 980px) {
      .app { grid-template-columns: 1fr; }
      aside { border-right: none; border-bottom: 1px solid var(--line); }
      .grid, .grid-3, .split { grid-template-columns: 1fr; }
      .kpis { grid-template-columns: repeat(2, 1fr); }
      .steps { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <h1>网文 Agent 控制台</h1>
      <div class="sub">源书融合、自动写作、节点确认、账号入口都在这里。</div>

      <div class="panel">
        <h2>平台账号</h2>
        <div class="grid-3">
          <button onclick="openProvider('chatgpt')">ChatGPT</button>
          <button onclick="openProvider('claude')">Claude</button>
          <button onclick="openProvider('manus')">Manus</button>
          <button onclick="openProvider('sub2api')">Sub2API</button>
          <button onclick="openProvider('deepseek')">DeepSeek</button>
          <button onclick="openProvider('kimi')">Kimi</button>
        </div>
        <div class="hint">登录在官方网页完成，本地控制台不接触你的账号密码。</div>
      </div>

      <div class="panel">
        <h2>项目</h2>
        <div id="projectList" class="project-list"></div>
      </div>
    </aside>

    <main>
      <div class="grid">
        <section class="panel">
          <h2>启动 / 继续自动工作流</h2>
          <label>已有项目路径</label>
          <input id="project" placeholder="可留空；留空时会新建项目" />
          <label>新项目名</label>
          <input id="name" value="my-first-novel" />
          <label>源书目录</label>
          <input id="sourceDir" placeholder="D:\你的小说目录" />
          <div class="grid">
            <div>
              <label>平台风格</label>
              <select id="platform">
                <option value="feilu">飞卢</option>
                <option value="fanqie">番茄</option>
                <option value="qidian">起点</option>
                <option value="zongheng">纵横</option>
              </select>
            </div>
            <div>
              <label>引擎</label>
              <select id="engine">
                <option value="langgraph">LangGraph</option>
                <option value="builtin">内置状态机</option>
              </select>
            </div>
          </div>
          <div class="grid">
            <div>
              <label>模型入口</label>
              <select id="provider">
                <option value="openai">OpenAI API</option>
                <option value="anthropic">Anthropic API</option>
                <option value="openai-compatible">OpenAI-compatible</option>
                <option value="deepseek">DeepSeek API</option>
                <option value="kimi">Kimi K3 API</option>
                <option value="sub2api" selected>Sub2API / GPT Plus 聚合</option>
                <option value="mock">Mock 测试</option>
              </select>
            </div>
            <div>
              <label>模型名</label>
              <input id="model" value="gpt-5.4" list="modelPresets" placeholder="kimi-k3 / deepseek-v4-pro / gpt-5.4 / 本地模型名" />
              <datalist id="modelPresets">
                <option value="kimi-k3">Kimi K3</option>
                <option value="deepseek-v4-pro">DeepSeek V4 Pro</option>
                <option value="deepseek-v4-flash">DeepSeek V4 Flash</option>
                <option value="gpt-5.4">Sub2API GPT</option>
                <option value="claude-3-5-sonnet-latest">Claude Sonnet</option>
              </datalist>
            </div>
          </div>
          <label>API Key 池（可批量；一行一个；不会保存到项目配置）</label>
          <textarea id="apiKeys" spellcheck="false" placeholder="sk-...&#10;sk-...&#10;sk-..."></textarea>
          <div class="row" style="margin-top: 8px;">
            <button type="button" onclick="loadLocalKeyPool()">载入当前入口 key 池</button>
            <button type="button" onclick="clearKeyPool()">清空 key</button>
            <span class="hint" id="keyCount">0 个 key；失败、限额、429/502/524 或超时时会自动换下一个。</span>
          </div>
          <label>Base URL（OpenAI-compatible 或代理服务）</label>
          <input id="baseUrl" value="https://sub2api.aifakapro.com" placeholder="https://sub2api.aifakapro.com" />
          <div class="grid">
            <div>
              <label>本轮推进到第几章</label>
              <input id="maxChapters" type="number" value="0" />
              <div class="hint">0 表示按当前全书大纲一直推进到终章。</div>
            </div>
            <div>
              <label>每个确认批次</label>
              <input id="batchSize" type="number" value="30" />
            </div>
          </div>
          <label>源书读取模式</label>
          <select id="sourceMode" onchange="syncSourceTimeoutDefault()">
            <option value="sampled" disabled>sampled：已禁用，不符合深读融合</option>
            <option value="deep" selected>deep：慢，逐块深读源书</option>
          </select>
          <label>源书融合方式</label>
          <select id="fusionMode">
            <option value="strong">强融合：优先复用并重组源书优秀设计</option>
            <option value="conservative">保守融合：主要提炼结构与节奏</option>
            <option value="licensed" selected>完全融合授权复用：剧情链/人物/境界可沿用</option>
          </select>
          <div class="hint">完全融合授权复用适用于所有平台：优先沿用并跨书整段融合源书的爽点段、剧情链、人物功能段、升级段和境界体系；人物名和专有人名必须改名；源句不大幅改写，只做词语级近义替换、称谓替换、专名替换和少量衔接替换，保留原意、基本句式和爽点力度。</div>
          <div class="grid">
            <div>
              <label>源书并发数</label>
              <input id="sourceWorkers" type="number" min="1" max="8" value="1" />
            </div>
            <div>
              <label>单书超时（秒）</label>
              <input id="sourceTimeout" type="number" min="60" value="3600" />
            </div>
          </div>
          <div class="grid">
            <div>
              <label>单书失败重试</label>
              <input id="sourceRetries" type="number" min="0" max="5" value="3" />
            </div>
            <div>
              <label class="check-row"><input id="streamPreview" type="checkbox" checked />流式预览模型返回</label>
              <div class="hint">开启后，运行日志会显示 <code>[模型流:...]</code>，拆书、聚合、设定、正文都会回显。</div>
            </div>
          </div>
          <div class="row" style="margin-top: 12px;">
            <button id="startButton" class="primary" onclick="startAutopilot()">启动 / 继续</button>
            <button id="testButton" class="blue" onclick="testLLM()">测试模型</button>
            <button onclick="refreshAll()">刷新状态</button>
            <button onclick="resetWritingOnly()">仅清空正文重写</button>
            <button class="danger" onclick="resetRebuild()">推翻重建</button>
            <button onclick="startSub2API()">启动本地 Sub2API（备用）</button>
            <button onclick="installTools()">安装 GitHub 工具</button>
          </div>
          <div class="hint">Sub2API 默认地址：<code>https://sub2api.aifakapro.com</code>。当前 key 绑定 new-plus 分组；后续新增到该分组的账号会被 Sub2API 轮询使用。</div>
        </section>

        <section class="panel">
          <h2>当前状态</h2>
          <div class="row" style="margin-bottom: 12px;">
            <span id="phase" class="status">未选择项目</span>
            <span id="operationBadge" class="status">空闲</span>
            <button id="reviseButton" class="blue" onclick="reviseCheckpoint()">应用微调</button>
            <button id="reviewFixButton" class="blue" onclick="reviseFromReview()">按审稿修稿</button>
            <button id="trialWriteButton" class="blue" onclick="trialWrite()">试写已生成章纲</button>
            <button id="promoteTrialButton" class="blue" onclick="promoteTrial()">试写稿转正式</button>
            <button id="continueOutlineButton" class="blue" onclick="continueOutline()">继续补章纲</button>
            <button id="powerRewriteButton" class="blue" onclick="powerRewrite()">回修战力境界</button>
            <button id="approveButton" class="primary" onclick="approve()">确认并继续</button>
            <button id="stopJobButton" class="danger" onclick="stopCurrentOperation()" disabled>停止当前任务</button>
            <button onclick="openProjectFolder()">打开项目文件夹</button>
          </div>
          <div class="grid" style="margin-bottom: 12px;">
            <div>
              <label>试写起始章</label>
              <input id="trialWriteStart" type="number" min="1" value="1" />
            </div>
            <div>
              <label>试写结束章</label>
              <input id="trialWriteEnd" type="number" min="1" value="3" />
            </div>
          </div>
          <div class="hint" style="margin-bottom: 12px;">试写只读取当前已经生成的 <code>chapter_plan_parts</code>，输出到 <code>05_drafts/trial_ch_*.md</code>，不会推进正式正文进度，也不会要求先生成完整章纲。</div>
          <div class="grid" style="margin-bottom: 12px;">
            <div>
              <label>转正式起始章</label>
              <input id="promoteTrialStart" type="number" min="1" value="1" />
            </div>
            <div>
              <label>转正式结束章</label>
              <input id="promoteTrialEnd" type="number" min="1" value="4" />
            </div>
          </div>
          <div class="grid" style="margin-bottom: 12px;">
            <div>
              <label>章纲补到第几章</label>
              <input id="outlineContinueUntil" type="number" min="1" value="120" />
            </div>
            <div>
              <label>增量写法</label>
              <div class="hint" style="padding-top: 10px;">可以先按已有章纲写正文；章纲不够时再补到 120、160、200 这种节点。</div>
            </div>
          </div>
          <div class="grid" style="margin-bottom: 12px;">
            <div>
              <label>战力回修起始章</label>
              <input id="powerRewriteStart" type="number" min="1" value="1" />
            </div>
            <div>
              <label>战力回修结束章</label>
              <input id="powerRewriteEnd" type="number" min="1" value="150" />
            </div>
          </div>
          <div class="operation-card">
            <div class="progress-head">
              <strong id="operationLabel">当前操作：空闲</strong>
              <span id="operationPercent">0%</span>
            </div>
            <div class="progress-bar"><div id="operationFill" class="progress-fill operation-fill"></div></div>
            <div class="hint" id="operationDetail">当前没有后台任务；点“启动 / 继续”后会显示微调或写作进度。</div>
            <div class="hint" id="operationEta"></div>
          </div>
          <div class="kpis">
            <div class="kpi"><strong id="books">0</strong><span>源书</span></div>
            <div class="kpi"><strong id="sourceFiles">0</strong><span>文件</span></div>
            <div class="kpi"><strong id="drafts">0</strong><span>正文批次</span></div>
            <div class="kpi"><strong id="summaries">0</strong><span>摘要</span></div>
            <div class="kpi"><strong id="nextChapter">-</strong><span>下一章</span></div>
          </div>
          <div class="path-card">
            <h3>生成位置</h3>
            <div class="path-line"><span>项目根目录</span><code class="path" id="pathProject">未选择项目</code></div>
            <div class="path-line"><span>候选大纲</span><code class="path" id="pathOutlineCandidates">未生成</code></div>
            <div class="path-line"><span>融合设定包</span><code class="path" id="pathNewNovel">未生成</code></div>
            <div class="row">
              <button type="button" onclick="openStatusPath('project')">打开项目根目录</button>
              <button type="button" onclick="openStatusPath('outline_candidates')">打开候选大纲</button>
              <button type="button" onclick="openStatusPath('new_novel')">打开 03_new_novel</button>
              <button type="button" onclick="openStatusPath('drafts')">打开正文草稿</button>
            </div>
          </div>
          <div class="progress-card">
            <div class="progress-head">
              <strong id="progressLabel">等待项目</strong>
              <span id="progressPercent">0%</span>
            </div>
            <div class="progress-bar"><div id="progressFill" class="progress-fill"></div></div>
            <div class="hint" id="progressDetail">选择项目后显示完整进度。</div>
            <div class="hint" id="progressEta"></div>
            <div class="steps" id="progressSteps"></div>
          </div>
          <div class="decision-card">
            <h3 id="decisionTitle">现在需要你做什么</h3>
            <p id="decisionBody">选择项目后显示当前节点。</p>
            <div class="hint" id="decisionAction"></div>
            <div class="file-grid" id="decisionFiles"></div>
          </div>
          <div class="revision-card">
            <h3 id="revisionTitle">最近微调效果</h3>
            <p id="revisionSummary">还没有成功应用自动微调。</p>
            <div class="hint" id="revisionNote"></div>
            <div class="row" id="revisionActions" style="margin-top: 8px;"></div>
            <div class="file-grid" id="revisionFiles"></div>
          </div>
          <label>微调要求 / 确认说明</label>
          <input id="approveNote" placeholder="比如：武道升级爽感更强，境界划分和主角等级写清楚" />
          <div class="hint" id="awaiting">没有等待确认的节点。</div>
        </section>
      </div>

      <div class="split">
        <section class="panel">
          <h2>运行日志</h2>
          <pre id="log">暂无任务。</pre>
        </section>
        <section class="panel">
          <h2>项目预览</h2>
          <h3>Novel Bible</h3>
          <pre id="bible">请选择项目。</pre>
          <h3 style="margin-top: 12px;">长期记忆</h3>
          <pre id="memory">请选择项目。</pre>
        </section>
      </div>
    </main>
  </div>

<script>
let currentJob = "";
let pollTimer = null;
let lastStatusData = null;
let currentOperation = {running: false, job_id: "", kind: "", label: "空闲"};

async function api(url, options = {}) {
  const res = await fetch(url, options);
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || res.statusText);
  return data;
}

async function refreshProjects() {
  const data = await api("/api/projects");
  const box = document.getElementById("projectList");
  box.innerHTML = "";
  if (!data.projects.length) {
    box.innerHTML = '<div class="hint">暂无项目。填写右侧表单启动一个。</div>';
    return;
  }
  for (const item of data.projects) {
    const btn = document.createElement("button");
    btn.className = "project";
    btn.innerHTML = `<strong>${item.name}</strong><small>${item.platform} · ${item.path}</small>`;
    btn.onclick = () => {
      document.getElementById("project").value = item.path;
      refreshStatus();
    };
    box.appendChild(btn);
  }
  const current = document.getElementById("project").value.trim();
  const saved = localStorage.getItem("webnovelAgentProject") || "";
  const fallback = data.projects.length === 1 ? data.projects[0].path : "";
  const nextProject = current || saved || fallback;
  if (nextProject) {
    document.getElementById("project").value = nextProject;
  }
}

async function refreshStatus() {
  const project = document.getElementById("project").value.trim();
  if (!project) {
    await refreshOperation();
    return;
  }
  localStorage.setItem("webnovelAgentProject", project);
  try {
    const data = await api(`/api/status?project=${encodeURIComponent(project)}`);
    lastStatusData = data;
    const state = data.state || {};
    const phase = document.getElementById("phase");
    phase.textContent = state.phase || "未启动 autopilot";
    phase.className = "status " + (state.awaiting ? "wait" : "run");
    document.getElementById("books").textContent = data.counts.books;
    document.getElementById("sourceFiles").textContent = data.counts.source_files;
    document.getElementById("drafts").textContent = data.counts.draft_batches;
    document.getElementById("summaries").textContent = data.counts.summaries;
    document.getElementById("nextChapter").textContent = state.current_chapter || "-";
    renderProgress(data.progress || {});
    renderPaths(data.paths || {});
    renderDecision(data.decision || {});
    renderRevision(data.revision || {});
    document.getElementById("awaiting").textContent = state.awaiting
      ? state.awaiting.message
      : (state.activity ? `当前动态：${state.activity}` : "没有等待确认的节点。");
    document.getElementById("bible").textContent = data.preview.novel_bible || "";
    document.getElementById("memory").textContent = data.preview.memory || "";
    await refreshOperation();
  } catch (err) {
    document.getElementById("awaiting").innerHTML = `<span class="error">${err.message}</span>`;
  }
}

function formatDuration(seconds) {
  const n = Math.max(0, Number(seconds || 0));
  const minutes = Math.floor(n / 60);
  const rest = Math.floor(n % 60);
  if (minutes <= 0) return `${rest} 秒`;
  return `${minutes} 分 ${String(rest).padStart(2, "0")} 秒`;
}

function setButton(id, props = {}) {
  const button = document.getElementById(id);
  if (!button) return;
  if (props.text !== undefined) button.textContent = props.text;
  if (props.disabled !== undefined) button.disabled = props.disabled;
}

function renderOperation(operation) {
  currentOperation = operation || {running: false, label: "空闲", progress: {}};
  const progress = currentOperation.progress || {};
  const running = Boolean(currentOperation.running);
  const percent = Math.max(0, Math.min(100, Number(progress.percent || 0)));
  const label = currentOperation.label || progress.label || "空闲";
  const badge = document.getElementById("operationBadge");
  badge.textContent = running ? `${label} · ${formatDuration(currentOperation.elapsed || 0)}` : "空闲";
  badge.className = "status " + (running ? "busy" : "");
  document.getElementById("operationLabel").textContent = running ? `当前操作：${progress.label || label}` : "当前操作：空闲";
  document.getElementById("operationPercent").textContent = `${Math.round(percent)}%`;
  document.getElementById("operationFill").style.width = `${percent}%`;
  document.getElementById("operationDetail").textContent = progress.detail || (running ? "后台任务正在运行。" : "当前没有后台任务；点“启动 / 继续”后会显示微调或写作进度。");
  document.getElementById("operationEta").textContent = progress.eta || "";
  setButton("stopJobButton", {disabled: !running, text: running ? "停止当前任务" : "停止当前任务"});
  setButton("startButton", {text: running ? "运行中..." : "启动 / 继续"});
  setButton("reviseButton", {text: currentOperation.kind === "revise" ? "微调中..." : "应用微调"});
  setButton("reviewFixButton", {disabled: running, text: currentOperation.kind === "revise" ? "修稿中..." : "按审稿修稿"});
  setButton("trialWriteButton", {disabled: running, text: currentOperation.kind === "trial-write" ? "试写中..." : "试写已生成章纲"});
  setButton("promoteTrialButton", {disabled: running, text: currentOperation.kind === "promote-trial" ? "转正式中..." : "试写稿转正式"});
  setButton("continueOutlineButton", {disabled: running, text: currentOperation.kind === "continue-outline" ? "补章纲中..." : "继续补章纲"});
  setButton("powerRewriteButton", {disabled: running, text: currentOperation.kind === "power-rewrite" ? "回修中..." : "回修战力境界"});
  setButton("approveButton", {text: currentOperation.kind === "approve" ? "确认中..." : "确认并继续"});
  setButton("testButton", {disabled: running});
  if (running && currentOperation.job_id && currentJob !== currentOperation.job_id) {
    watchJob(currentOperation.job_id);
  }
}

async function refreshOperation() {
  const project = document.getElementById("project").value.trim();
  try {
    const operation = await api(`/api/operation?project=${encodeURIComponent(project)}`);
    renderOperation(operation);
    return operation;
  } catch (err) {
    document.getElementById("operationDetail").innerHTML = `<span class="error">${err.message}</span>`;
    return currentOperation;
  }
}

function renderProgress(progress) {
  const percent = Math.max(0, Math.min(100, Number(progress.percent || 0)));
  document.getElementById("progressLabel").textContent = progress.label || "等待项目";
  document.getElementById("progressPercent").textContent = `${Math.round(percent)}%`;
  document.getElementById("progressFill").style.width = `${percent}%`;
  document.getElementById("progressDetail").textContent = progress.detail || "选择项目后显示完整进度。";
  document.getElementById("progressEta").textContent = progress.eta || "";
  const stepsBox = document.getElementById("progressSteps");
  stepsBox.innerHTML = "";
  for (const step of (progress.steps || [])) {
    const item = document.createElement("div");
    item.className = "step" + (step.done ? " done" : "") + (step.current ? " current" : "");
    item.innerHTML = `<strong>${step.name}</strong><span>${step.detail || ""}</span>${step.current ? "<em>当前阶段</em>" : ""}`;
    stepsBox.appendChild(item);
  }
}

function formatSize(bytes) {
  const n = Number(bytes || 0);
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${n} B`;
}

function formatTime(value) {
  if (!value) return "";
  return value.replace("T", " ").slice(0, 16);
}

function renderPaths(paths) {
  document.getElementById("pathProject").textContent = paths.project || "未选择项目";
  document.getElementById("pathOutlineCandidates").textContent = paths.outline_candidates || "未生成";
  document.getElementById("pathNewNovel").textContent = paths.new_novel || "未生成";
}

function renderFileChips(boxId, files, options = {}) {
  const box = document.getElementById(boxId);
  box.innerHTML = "";
  if (!(files || []).length) {
    const empty = document.createElement("div");
    empty.className = "hint";
    empty.textContent = options.emptyText || "暂无文件记录。";
    box.appendChild(empty);
    return;
  }
  for (const file of (files || [])) {
    const chip = document.createElement("div");
    chip.className = "file-chip" + (file.exists ? "" : " missing");
    const title = document.createElement("strong");
    title.textContent = file.label || file.rel || "文件";
    const meta = document.createElement("small");
    const bits = [];
    if (file.rel) bits.push(file.rel);
    if (file.exists) bits.push(formatSize(file.size));
    if (file.updated_at) bits.push(formatTime(file.updated_at));
    meta.textContent = bits.join(" · ") || "尚未生成";
    chip.appendChild(title);
    chip.appendChild(meta);
    if (file.delta_chars !== undefined && file.delta_chars !== null) {
      const delta = document.createElement("small");
      delta.className = "delta";
      delta.textContent = `字数变化：${file.before_chars || 0} -> ${file.after_chars || 0} (${Number(file.delta_chars || 0) >= 0 ? "+" : ""}${file.delta_chars || 0})`;
      chip.appendChild(delta);
    }
    if (file.path) {
      const row = document.createElement("div");
      row.className = "row";
      row.style.marginTop = "8px";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = options.buttonText || "打开";
      btn.onclick = () => openPath(file.path);
      row.appendChild(btn);
      if (file.candidate_no) {
        const choose = document.createElement("button");
        choose.type = "button";
        choose.className = "primary";
        choose.textContent = "选择此方案";
        choose.onclick = () => selectOutline(file.candidate_no);
        row.appendChild(choose);
      }
      chip.appendChild(row);
    }
    box.appendChild(chip);
  }
}

function renderDecision(decision) {
  document.getElementById("decisionTitle").textContent = decision.title || "现在需要你做什么";
  document.getElementById("decisionBody").textContent = decision.body || "选择项目后显示当前节点。";
  document.getElementById("decisionAction").textContent = decision.action || "";
  renderFileChips("decisionFiles", decision.suggested_files || [], {buttonText: "查看"});
}

function renderRevision(revision) {
  document.getElementById("revisionTitle").textContent = revision.title || "最近微调效果";
  document.getElementById("revisionSummary").textContent = revision.summary || "还没有成功应用自动微调。";
  document.getElementById("revisionNote").textContent = revision.note ? `${revision.note_label || "微调要求"}：${revision.note}` : "";
  const actions = document.getElementById("revisionActions");
  actions.innerHTML = "";
  if (revision.report && revision.report.path) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "打开微调效果报告";
    btn.onclick = () => openPath(revision.report.path);
    actions.appendChild(btn);
  }
  renderFileChips("revisionFiles", revision.files || [], {buttonText: "查看", emptyText: "还没有自动微调改动记录。"});
}

async function openPath(path) {
  if (!path) return;
  await api("/api/open-path", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({path})
  });
}

async function openStatusPath(key) {
  if (!lastStatusData || !lastStatusData.paths) return;
  await openPath(lastStatusData.paths[key] || "");
}

async function refreshAll() {
  await refreshProjects();
  await refreshStatus();
}

async function openProvider(provider) {
  await api("/api/open-provider", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({provider})
  });
}

function splitApiKeys(raw) {
  return raw.split(/[\r\n,;]+/).map(x => x.trim()).filter(Boolean);
}

function updateKeyCount() {
  const count = splitApiKeys(document.getElementById("apiKeys").value).length;
  document.getElementById("keyCount").textContent = `${count} 个 key；失败、限额、429/502/524 或超时时会自动换下一个。`;
}

async function loadLocalKeyPool(options = {}) {
  const provider = document.getElementById("provider").value || "sub2api";
  const data = await api(`/api/key-pool/latest?provider=${encodeURIComponent(provider)}`);
  if (!data.count) {
    if (!options.silent) {
      document.getElementById("keyCount").textContent = `没有找到本地 ${provider} key 池文件。`;
    }
    return;
  }
  document.getElementById("apiKeys").value = data.keys.trim();
  updateKeyCount();
}

function clearKeyPool() {
  document.getElementById("apiKeys").value = "";
  updateKeyCount();
}

function syncSourceTimeoutDefault() {
  const mode = document.getElementById("sourceMode").value;
  const timeout = document.getElementById("sourceTimeout");
  const current = Number(timeout.value || 0);
  if (mode === "deep" && (!current || current <= 900)) {
    timeout.value = 3600;
  }
  if (mode === "sampled" && (!current || current === 3600)) {
    timeout.value = 900;
  }
}

function formData() {
  const apiKeys = document.getElementById("apiKeys").value.trim();
  return {
    project: document.getElementById("project").value.trim(),
    name: document.getElementById("name").value.trim(),
    source_dir: document.getElementById("sourceDir").value.trim(),
    platform: document.getElementById("platform").value,
    engine: document.getElementById("engine").value,
    provider: document.getElementById("provider").value,
    model: document.getElementById("model").value.trim(),
    api_keys: apiKeys,
    api_key: splitApiKeys(apiKeys)[0] || "",
    base_url: document.getElementById("baseUrl").value.trim(),
    max_chapters: Number(document.getElementById("maxChapters").value || 0),
    batch_size: Number(document.getElementById("batchSize").value || 30),
    source_mode: document.getElementById("sourceMode").value,
    fusion_mode: document.getElementById("fusionMode").value,
    source_workers: Number(document.getElementById("sourceWorkers").value || 1),
    source_timeout: Number(document.getElementById("sourceTimeout").value || 3600),
    source_retries: Number(document.getElementById("sourceRetries").value || 3),
    stream_preview: document.getElementById("streamPreview").checked
  };
}

async function resolveRunningConflict(nextKind) {
  const operation = await refreshOperation();
  if (!operation.running) return {ok: true, force_restart: false};
  const same = operation.kind === nextKind;
  const current = operation.label || "后台任务";
  const nextName = {
    revise: "新的微调",
    autopilot: "启动/继续自动流程",
    approve: "确认并继续",
    "select-outline": "选择候选大纲",
    "trial-write": "试写已生成章纲",
    "promote-trial": "试写稿转正式",
    "continue-outline": "继续补章纲",
    "power-rewrite": "回修战力境界",
    "reset-writing": "仅清空正文重写",
    rebuild: "推翻重建",
    test: "测试模型"
  }[nextKind] || "新任务";
  const message = same
    ? `当前已经在${current}，已运行 ${formatDuration(operation.elapsed || 0)}。\n\n确定要停止前一次并重新开始吗？\n点“取消”就是继续等待当前任务。`
    : `当前${current}还在运行，已运行 ${formatDuration(operation.elapsed || 0)}。\n\n如果现在执行“${nextName}”，需要先停止当前任务。确定停止并继续吗？\n点“取消”就是继续等待当前任务。`;
  if (!window.confirm(message)) {
    if (operation.job_id) watchJob(operation.job_id);
    return {ok: false, force_restart: false};
  }
  await cancelOperation(operation);
  return {ok: true, force_restart: true};
}

async function cancelOperation(operation) {
  const project = document.getElementById("project").value.trim();
  await api("/api/cancel-job", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      job_id: operation.job_id || "",
      project,
      kind: operation.kind || ""
    })
  });
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  currentJob = "";
  localStorage.removeItem("webnovelAgentJob");
  await refreshAll();
}

async function stopCurrentOperation() {
  const operation = await refreshOperation();
  if (!operation.running) return;
  if (!window.confirm(`确定停止当前任务：${operation.label || "后台任务"}？`)) return;
  await cancelOperation(operation);
}

async function startAutopilot() {
  const conflict = await resolveRunningConflict("autopilot");
  if (!conflict.ok) return;
  const payload = formData();
  payload.force_restart = conflict.force_restart;
  const data = await api("/api/autopilot", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  watchJob(data.job_id);
}

async function resetRebuild() {
  const project = document.getElementById("project").value.trim();
  if (!project) {
    document.getElementById("awaiting").innerHTML = '<span class="error">请先选择已有项目。</span>';
    return;
  }
  const first = window.confirm("这会把当前源书索引、拆解、设定包、正文草稿等产物归档到 _backups，然后重新读取源书并从头生成。确认继续吗？");
  if (!first) return;
  const second = window.confirm("再次确认：这不是普通微调，而是推翻当前大纲重建。旧文件会归档保留，但当前项目会回到新流程。");
  if (!second) return;
  const conflict = await resolveRunningConflict("rebuild");
  if (!conflict.ok) return;
  const payload = formData();
  payload.force_restart = conflict.force_restart;
  const data = await api("/api/reset-rebuild", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  watchJob(data.job_id);
}

async function resetWritingOnly() {
  const project = document.getElementById("project").value.trim();
  if (!project) {
    document.getElementById("awaiting").innerHTML = '<span class="error">请先选择已有项目。</span>';
    return;
  }
  const confirmed = window.confirm(
    "这只会把当前正文草稿、摘要、审稿、导出、提示词，以及旧稿衍生的角色当前状态/伏笔台账/风险提醒归档到 _backups。\n\n" +
    "它会保留源书拆解、素材池、融合设定包、完整章纲、力量体系和从头重写规则；不会重新读取原书，也不会重新生成大纲。\n\n" +
    "完成后点“启动 / 继续”，会从第0001章按当前新版设定、章纲和力量体系重写正文。确定继续吗？"
  );
  if (!confirmed) return;
  const conflict = await resolveRunningConflict("reset-writing");
  if (!conflict.ok) return;
  const data = await api("/api/reset-writing", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({project, force_restart: conflict.force_restart})
  });
  document.getElementById("log").textContent = data.message + "\n备份位置：" + data.backup + "\n重写依据快照：" + (data.basis || "");
  await refreshAll();
}

async function approve() {
  const project = document.getElementById("project").value.trim();
  if (!project) return;
  const conflict = await resolveRunningConflict("approve");
  if (!conflict.ok) return;
  const note = document.getElementById("approveNote").value.trim();
  if (note && !window.confirm("确认并继续只记录这段说明，不会自动改设定。要先自动修改，请点“取消”，再点“应用微调”。仍要继续吗？")) {
    return;
  }
  const data = await api("/api/approve", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({project, note, force_restart: conflict.force_restart})
  });
  watchJob(data.job_id);
}

async function selectOutline(candidateNo) {
  const project = document.getElementById("project").value.trim();
  if (!project) return;
  const conflict = await resolveRunningConflict("select-outline");
  if (!conflict.ok) return;
  const data = await api("/api/select-outline", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({project, candidate: candidateNo, force_restart: conflict.force_restart})
  });
  watchJob(data.job_id);
}

async function reviseCheckpoint() {
  const note = document.getElementById("approveNote").value.trim();
  if (!note) {
    document.getElementById("awaiting").innerHTML = '<span class="error">请先填写微调要求。</span>';
    return;
  }
  const conflict = await resolveRunningConflict("revise");
  if (!conflict.ok) return;
  const payload = formData();
  payload.note = note;
  payload.force_restart = conflict.force_restart;
  const data = await api("/api/revise", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  watchJob(data.job_id);
}

async function reviseFromReview() {
  const project = document.getElementById("project").value.trim();
  if (!project) {
    document.getElementById("awaiting").innerHTML = '<span class="error">请先选择已有项目。</span>';
    return;
  }
  const conflict = await resolveRunningConflict("revise");
  if (!conflict.ok) return;
  const payload = formData();
  payload.note = "根据当前批次最新审稿报告逐章修复当前正文批次。优先执行审稿报告中的逐章修改建议；必要时重写问题章节；保留主线剧情、人物关系、金手指规则、既定收益和后续承接；修完后重新审稿。";
  payload.force_restart = conflict.force_restart;
  const data = await api("/api/revise", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  watchJob(data.job_id);
}

async function trialWrite() {
  const project = document.getElementById("project").value.trim();
  if (!project) {
    document.getElementById("awaiting").innerHTML = '<span class="error">请先选择已有项目。</span>';
    return;
  }
  const start = Math.max(1, Number(document.getElementById("trialWriteStart").value || 1));
  const end = Number(document.getElementById("trialWriteEnd").value || start);
  if (end < start) {
    document.getElementById("awaiting").innerHTML = '<span class="error">试写结束章不能小于起始章。</span>';
    return;
  }
  if (end - start + 1 > 10) {
    document.getElementById("awaiting").innerHTML = '<span class="error">试写一次最多 10 章，建议先试 1-3 章。</span>';
    return;
  }
  const confirmed = window.confirm(
    `将基于当前已经生成的章纲分段，试写第 ${String(start).padStart(4, "0")} 章到第 ${String(end).padStart(4, "0")} 章正文。\n\n` +
    "这不会推进正式正文进度，也不会要求先生成完整章纲；输出会保存到 05_drafts/trial_ch_*.md。\n\n" +
    "如果后台还在补章纲，建议先停止当前任务，避免继续消耗额度。确定试写吗？"
  );
  if (!confirmed) return;
  const conflict = await resolveRunningConflict("trial-write");
  if (!conflict.ok) return;
  const payload = formData();
  payload.start = start;
  payload.end = end;
  payload.force_restart = conflict.force_restart;
  const data = await api("/api/trial-write", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  watchJob(data.job_id);
}

async function promoteTrial() {
  const project = document.getElementById("project").value.trim();
  if (!project) {
    document.getElementById("awaiting").innerHTML = '<span class="error">请先选择已有项目。</span>';
    return;
  }
  const start = Math.max(1, Number(document.getElementById("promoteTrialStart").value || 1));
  const end = Number(document.getElementById("promoteTrialEnd").value || start);
  if (end < start) {
    document.getElementById("awaiting").innerHTML = '<span class="error">转正式结束章不能小于起始章。</span>';
    return;
  }
  const confirmed = window.confirm(
    `将把 05_drafts/trial_ch_*.md 里的第 ${String(start).padStart(4, "0")} 章到第 ${String(end).padStart(4, "0")} 章转成正式正文批次。\n\n` +
    `转成后，正式续写会从第 ${String(end + 1).padStart(4, "0")} 章开始；原试写稿仍会保留。确定继续吗？`
  );
  if (!confirmed) return;
  const conflict = await resolveRunningConflict("promote-trial");
  if (!conflict.ok) return;
  const data = await api("/api/promote-trial", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({project, start, end, force_restart: conflict.force_restart})
  });
  watchJob(data.job_id);
}

async function continueOutline() {
  const project = document.getElementById("project").value.trim();
  if (!project) {
    document.getElementById("awaiting").innerHTML = '<span class="error">请先选择已有项目。</span>';
    return;
  }
  const until = Math.max(1, Number(document.getElementById("outlineContinueUntil").value || 0));
  const confirmed = window.confirm(
    `将继续补逐章章纲，目标连续补到第 ${String(until).padStart(4, "0")} 章。\n\n` +
    "这一步会消耗 API 额度；它只补章纲，不会自动写正文。确定继续吗？"
  );
  if (!confirmed) return;
  const conflict = await resolveRunningConflict("continue-outline");
  if (!conflict.ok) return;
  const payload = formData();
  payload.until = until;
  payload.force_restart = conflict.force_restart;
  const data = await api("/api/continue-outline", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  watchJob(data.job_id);
}

async function powerRewrite() {
  const project = document.getElementById("project").value.trim();
  if (!project) {
    document.getElementById("awaiting").innerHTML = '<span class="error">请先选择已有项目。</span>';
    return;
  }
  const start = Math.max(1, Number(document.getElementById("powerRewriteStart").value || 1));
  const end = Number(document.getElementById("powerRewriteEnd").value || 150);
  if (end < start) {
    document.getElementById("awaiting").innerHTML = '<span class="error">结束章不能小于起始章。</span>';
    return;
  }
  const confirmed = window.confirm(
    `将从第 ${String(start).padStart(4, "0")} 章到第 ${String(end).padStart(4, "0")} 章回修战力境界描写。\n\n` +
    "规则：尽量不改剧情、人物行为、事件顺序和章末钩子，只把当前修为/等级、核心能力状态、关键道具权限、敌我差距、越级依据和代价自然融入正文。\n\n" +
    "建议先跑 3-10 章样本，满意后再扩大到 150 章。确定继续吗？"
  );
  if (!confirmed) return;
  const conflict = await resolveRunningConflict("power-rewrite");
  if (!conflict.ok) return;
  const payload = formData();
  payload.start = start;
  payload.end = end;
  payload.force_restart = conflict.force_restart;
  const data = await api("/api/power-rewrite", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  watchJob(data.job_id);
}

async function installTools() {
  const data = await api("/api/install-tools", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
  watchJob(data.job_id);
}

async function startSub2API() {
  const data = await api("/api/start-sub2api", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
  watchJob(data.job_id);
}

async function testLLM() {
  const data = await api("/api/test-llm", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(formData())
  });
  watchJob(data.job_id);
}

async function openProjectFolder() {
  const project = document.getElementById("project").value.trim();
  if (!project) return;
  await api("/api/open-path", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({path: project})
  });
}

function watchJob(jobId) {
  if (!jobId) {
    refreshAll();
    return;
  }
  currentJob = jobId;
  localStorage.setItem("webnovelAgentJob", jobId);
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollJob, 1200);
  pollJob();
}

async function pollJob() {
  if (!currentJob) return;
  const data = await api(`/api/job?id=${encodeURIComponent(currentJob)}`);
  document.getElementById("log").textContent = renderJobLog(data);
  if (data.status === "running") {
    await refreshStatus();
  }
  if (data.status === "done" || data.status === "failed" || data.status === "cancelled" || data.status === "missing") {
    clearInterval(pollTimer);
    pollTimer = null;
    if (data.status !== "missing") {
      localStorage.removeItem("webnovelAgentJob");
    }
    await refreshAll();
  }
}

async function resumeLastJob() {
  const saved = localStorage.getItem("webnovelAgentJob") || "";
  if (saved) {
    const data = await api(`/api/job?id=${encodeURIComponent(saved)}`);
    if (data.status === "running") {
      watchJob(saved);
      return;
    }
    localStorage.removeItem("webnovelAgentJob");
  }
  const latest = await api("/api/jobs/latest");
  if (latest.id && latest.status === "running") {
    watchJob(latest.id);
  }
}

function renderJobLog(data) {
  const lines = [];
  const elapsed = data.started_at
    ? Math.max(0, Math.round(Date.now() / 1000 - data.started_at))
    : 0;
  lines.push(`状态：${data.status || "unknown"}，已运行 ${elapsed} 秒`);
  if (data.returncode !== null && data.returncode !== undefined) {
    lines.push(`退出码：${data.returncode}`);
  }
  const logs = data.log || [];
  if (!logs.length && data.status === "running") {
    lines.push("等待后台输出。deep 模式首次拆源书时，单个模型调用可能需要几十秒到几分钟。");
  }
  return lines.concat(logs).join("\n");
}

refreshAll();
resumeLastJob();
updateKeyCount();
document.getElementById("apiKeys").addEventListener("input", updateKeyCount);
loadLocalKeyPool({silent: true});

document.getElementById("provider").addEventListener("change", () => {
  const provider = document.getElementById("provider").value;
  const base = document.getElementById("baseUrl");
  const model = document.getElementById("model");
  if (provider === "sub2api") {
    base.value = "https://sub2api.aifakapro.com";
    model.value = "gpt-5.4";
  }
  if (provider === "deepseek") {
    base.value = "https://api.deepseek.com";
    model.value = "deepseek-v4-pro";
  }
  if (provider === "kimi") {
    base.value = "https://api.moonshot.cn/v1";
    model.value = "kimi-k3";
  }
  loadLocalKeyPool({silent: true});
});
</script>
</body>
</html>
"""


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="启动本地网文 Agent 控制台")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    run_server(port=args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    main()
