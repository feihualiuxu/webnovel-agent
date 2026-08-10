from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import string
import sys
import time
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = APP_ROOT / "config"
PROJECTS_ROOT = APP_ROOT / "projects"
SUPPORTED_SOURCE_EXTS = {".txt", ".md", ".markdown"}
PLACEHOLDER = "状态：待生成"

SOURCE_BACKBONE_CLONE_RULE = """
源书主干复刻改名版硬规则：
- 每本导入源书都必须先锁定并保留题材类型、时代背景、社会/职业生态、主角身份、核心金手指功能、主线目标、反派压迫链、地图/场景媒介、关键剧情链、事件顺序、章节节奏、章末钩子和平台爽感。
- 绝对禁止把现代都市改成古代/玄幻/仙侠，或把任意题材换时代、换背景、换职业生态、换核心社会关系、换核心金手指逻辑、换核心剧情链。
- 源书没有玄幻境界、宗门地图、上界结构时，不得硬造境界表、宗门、秘境、王朝、古族、域外等玄幻元素；现代都市源书的升级只能按源书已有的系统等级、账号等级、财富、权限、职业段位、技术/资源成长等方式保留。
- 多源融合时，以主源书为骨架，其他源书只嵌入同功能位的爽点、桥段、反派压迫方式或升级/经营模块，不得推翻主源书背景和主线。单本源书时，只能生成同一源书主干的改名强化版，不得生成新故事。
- 人物名、专有人名、地名、势力名、公司/门派/组织名等可以替换，但场景功能、关系结构、事件顺序、爽点回收和章末钩子不变。
"""

FAST_PAYOFF_NO_DOWNGRADE_RULE = """
快爽与不得降配硬规则：
- 剧情一定要爽，升级一定要快，打脸和反杀一定要来得快且爽；必须跟随源书原本节奏，不得擅自拉长铺垫或改成慢燃。
- 爽点密度、升级速度、打脸频率、金手指功能强度、金手指反馈和收益兑现不得低于源书；允许在合理处更快、更大、更爽，但不允许削弱或稀释。
- 主角可以遇到压力，但压力只服务于快速反杀、立刻打脸、升级兑现和奖励回收，不得连续多章苦大仇深、被动挨打、沉重宿命化。
- 不要把源书简单爽点“高级化”成抽象设定解释、宏大空话、心理散文或 AI 总结腔。
"""


def read_text(path: Path, max_chars: int | None = None) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = path.read_text(encoding="utf-8", errors="ignore")
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars]
    return text


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def read_api_keys_file(path_raw: str) -> str:
    if not path_raw:
        return ""
    return read_text(Path(path_raw).expanduser().resolve())


def load_json(path: Path) -> Any:
    return json.loads(read_text(path))


def write_json(path: Path, data: Any) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{dt.datetime.now():%Y%m%d%H%M%S%f}.tmp")
    write_text(tmp, payload)

    last_error: OSError | None = None
    for _ in range(30):
        try:
            tmp.replace(path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.1)
        except OSError as exc:
            last_error = exc
            time.sleep(0.1)

    # Windows can briefly deny atomic replacement when another process has the
    # target open. Fall back to a plain write so progress logging never kills a
    # long generation run; keep a backup when possible.
    try:
        if path.exists() and path.stat().st_size > 0:
            backup = path.with_name(f"{path.name}.bak")
            shutil.copy2(path, backup)
    except OSError:
        pass
    try:
        write_text(path, payload)
    except OSError:
        if last_error is not None:
            raise last_error
        raise
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"\s+", "-", value)
    keep = set(string.ascii_lowercase + string.digits + "-_")
    slug = "".join(ch for ch in value if ch in keep)
    if slug:
        return slug[:64]
    digest = hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"novel-{digest}"


def safe_name(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", value.strip())
    value = re.sub(r"\s+", "_", value)
    return value[:80] or "untitled"


def timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def load_profiles() -> dict[str, Any]:
    return load_json(CONFIG_ROOT / "platform_profiles.json")


def resolve_project(path: str | None) -> Path:
    if not path:
        raise SystemExit("请用 --project 指定项目目录。")
    project = Path(path).expanduser().resolve()
    if not project.exists():
        raise SystemExit(f"项目不存在：{project}")
    if not (project / "00_config" / "project.json").exists():
        raise SystemExit(f"这不是一个 webnovel-agent 项目：{project}")
    return project


def load_project_config(project: Path) -> dict[str, Any]:
    return load_json(project / "00_config" / "project.json")


def save_project_config(project: Path, config: dict[str, Any]) -> None:
    config["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    write_json(project / "00_config" / "project.json", config)


def platform_block(profile: dict[str, Any]) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items)

    return (
        f"平台：{profile['display_name']}\n"
        f"定位：{profile['positioning']}\n"
        f"单章字数：{profile['chapter_words']}\n\n"
        "使用说明：以下内容只作市场分类和体量参考；标题、章纲颗粒度和正文文风必须以源书/样书证据为准，不要机械套固定模板。\n\n"
        "标题/开篇规则：\n"
        f"{bullets(profile['headline_rules'])}\n\n"
        "风格规则：\n"
        f"{bullets(profile['style_rules'])}\n\n"
        "避免：\n"
        f"{bullets(profile['avoid'])}"
    )


def mkdirs(project: Path) -> None:
    for folder in (
        "00_config",
        "01_sources/source_cards",
        "02_source_analysis",
        "03_new_novel",
        "04_prompts",
        "05_drafts",
        "06_summaries",
        "07_reviews",
        "08_exports",
        "09_handoff",
        "10_inbox",
    ):
        (project / folder).mkdir(parents=True, exist_ok=True)


RUNTIME_SEED_FILES = {
    "02_source_analysis/source_bibles.md": "# 源书拆解总表\n\n状态：待生成\n",
    "02_source_analysis/source_style_profile.md": "# 源书风格画像\n\n状态：待生成\n",
    "02_source_analysis/motif_library.csv": "motif,type,source,usable_pattern,avoid_copying\n",
    "02_source_analysis/character_pool.csv": "role_id,function_role,source_books,source_element,traits,relationship_use,growth_arc,keep_or_modify,fusion_target,avoid_copying\n",
    "02_source_analysis/plot_pool.csv": "plot_id,module_name,source_books,source_element,conflict_model,setup,payoff,keep_or_modify,fusion_target,risk_note\n",
    "02_source_analysis/power_system_pool.csv": "system_id,source_books,source_element,core_logic,realm_or_upgrade_path,limit,cost,payoff,keep_or_modify,fusion_target\n",
    "02_source_analysis/fusion_opportunities.md": "# 融合机会\n\n状态：待生成\n",
    "02_source_analysis/source_risk_notes.md": "# 源素材复用与融合笔记\n\n默认按完全融合授权复用整理源书素材：爽点、故事情节、剧情链、事件顺序、场景媒介、关键转折、人物功能设定、人物关系、境界体系和设定骨架都可以直接沿用并跨书整段融合；人物名和专有人名不能照搬，必须改名；背景、势力名、地名、设定名、金手指名和承接逻辑按本书统一。源文句子不要大幅改写，不要改变原句意思和基本句式，只做词语级近义替换、称谓替换、专名替换和少量衔接替换；必须保留原句语气、动作顺序、对白功能、信息量和爽点力度，不能整段逐字原样粘贴，也不能改成 AI 味解释腔。\n",
    "03_new_novel/novel_bible.md": "# Novel Bible\n\n状态：待生成\n",
    "03_new_novel/style_guide.md": "# Style Guide\n\n状态：待生成\n",
    "03_new_novel/power_system.md": "# Power System\n\n状态：待生成\n",
    "03_new_novel/power_state_ledger.md": "# Power State Ledger\n\n状态：待生成\n",
    "03_new_novel/worldbuilding.md": "# Worldbuilding\n\n状态：待生成\n",
    "03_new_novel/character_table.csv": "name,role,first_seen,status,goal,relationship,notes\n",
    "03_new_novel/volume_outline.md": "# Volume Outline\n\n状态：待生成\n",
    "03_new_novel/full_story_outline.md": "# Full Story Outline\n\n状态：待生成\n",
    "03_new_novel/fusion_traceability.md": "# Fusion Traceability\n\n状态：待生成\n",
    "03_new_novel/chapter_plan.csv": "chapter_no,volume,title,core_conflict,small_hook,power_usage,character_change,foreshadowing,ending_hook,mainline_progress,source_inspiration,status\n",
    "03_new_novel/continuity_ledger.md": "# Continuity Ledger\n\n状态：待生成\n",
    "03_new_novel/foreshadowing_ledger.md": "# Foreshadowing Ledger\n\n状态：待生成\n",
    "03_new_novel/memory_rollup.md": "# Memory Rollup\n\n状态：待生成\n",
    "10_inbox/README.md": "# Inbox\n\n把 ChatGPT / Claude / Manus 输出保存到这里，再用 `accept` 命令归档。\n",
}


def seed_runtime_files(project: Path) -> None:
    mkdirs(project)
    for rel, content in RUNTIME_SEED_FILES.items():
        write_text(project / rel, content)


def reset_project_for_rebuild(project: Path) -> Path:
    backup = project / "_backups" / f"reset_{timestamp()}"
    backup.mkdir(parents=True, exist_ok=True)
    for rel in (
        "01_sources",
        "02_source_analysis",
        "03_new_novel",
        "04_prompts",
        "05_drafts",
        "06_summaries",
        "07_reviews",
        "08_exports",
        "09_handoff",
    ):
        source = project / rel
        if source.exists():
            target = backup / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
    seed_runtime_files(project)
    write_json(
        project / "00_config" / "agent_state.json",
        {
            "phase": "new",
            "current_chapter": 1,
            "awaiting": None,
            "approvals": [],
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "activity": f"已归档旧产物并准备从源书重建：{backup}",
            "reset_backup": str(backup),
        },
    )
    return backup


REWRITE_BASIS_FILES = (
    "03_new_novel/novel_bible.md",
    "03_new_novel/style_guide.md",
    "03_new_novel/power_system.md",
    "03_new_novel/power_state_ledger.md",
    "03_new_novel/worldbuilding.md",
    "03_new_novel/volume_outline.md",
    "03_new_novel/full_story_outline.md",
    "03_new_novel/chapter_plan.csv",
    "03_new_novel/continuity_ledger.md",
    "03_new_novel/memory_rollup.md",
)

REWRITE_REQUIRED_BASIS_FILES = (
    "03_new_novel/novel_bible.md",
    "03_new_novel/power_system.md",
    "03_new_novel/power_state_ledger.md",
    "03_new_novel/full_story_outline.md",
    "03_new_novel/chapter_plan.csv",
)


def _file_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "size": 0, "updated_at": ""}
    return {
        "exists": True,
        "size": path.stat().st_size,
        "updated_at": dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
    }


def _chapter_plan_row_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        rows = csv.reader(read_text(path).splitlines())
        return sum(1 for row in rows if row and row[0].strip().isdigit())
    except Exception:
        return 0


def _ensure_rewrite_basis(project: Path) -> None:
    missing = []
    for rel in REWRITE_REQUIRED_BASIS_FILES:
        path = project / rel
        if not path.exists() or path.stat().st_size < 500:
            missing.append(rel)
    if missing:
        raise RuntimeError(
            "缺少从头重写所需的现有设定/章纲文件，请先完成新版设定包和完整章纲："
            + "、".join(missing)
        )
    plan_rows = _chapter_plan_row_count(project / "03_new_novel" / "chapter_plan.csv")
    if plan_rows < 50:
        raise RuntimeError("chapter_plan.csv 行数过少，不能确认会按完整章纲从头重写。")


def _write_rewrite_basis_snapshot(project: Path) -> Path:
    snapshot_path = project / "00_config" / "rewrite_basis.json"
    snapshot = {
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "purpose": "只清空正文重写：保留当前源书拆解、素材池、新版设定包、完整章纲和力量体系。",
        "chapter_plan_rows": _chapter_plan_row_count(project / "03_new_novel" / "chapter_plan.csv"),
        "basis_files": {rel: _file_snapshot(project / rel) for rel in REWRITE_BASIS_FILES},
    }
    write_json(snapshot_path, snapshot)
    return snapshot_path


def _archive_file(path: Path, project: Path, backup: Path) -> None:
    if not path.exists():
        return
    rel = path.relative_to(project)
    target = backup / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(target))


def _is_rewrite_ready_ledger(path: Path) -> bool:
    if not path.exists():
        return False
    text = read_text(path, 4000)
    return "从头重写" in text and ("旧稿" in text or "重新写" in text)


def _archive_stale_rewrite_runtime_files(project: Path, backup: Path) -> list[str]:
    archived: list[str] = []
    stale_paths: list[Path] = []

    character_table = project / "03_new_novel" / "character_table.csv"
    if character_table.exists():
        text = read_text(character_table, 3000)
        if "当前状态" in text or "截至第" in text or "井下仓" in text:
            stale_paths.append(character_table)

    foreshadowing = project / "03_new_novel" / "foreshadowing_ledger.md"
    if foreshadowing.exists() and "截至第" in read_text(foreshadowing, 1000):
        stale_paths.append(foreshadowing)

    for rel in ("03_new_novel/continuity_ledger.md", "03_new_novel/memory_rollup.md"):
        path = project / rel
        if path.exists() and not _is_rewrite_ready_ledger(path):
            stale_paths.append(path)

    for pattern in ("stage_*_risk_notes.md", "risk_reminder_*.md"):
        stale_paths.extend((project / "03_new_novel").glob(pattern))

    for path in sorted(set(stale_paths)):
        if not path.exists():
            continue
        archived.append(str(path.relative_to(project)).replace("\\", "/"))
        _archive_file(path, project, backup)

    character_table = project / "03_new_novel" / "character_table.csv"
    if not character_table.exists():
        write_text(
            character_table,
            "character_id,name,function_role,first_appearance,goal,secret,relationship_to_mc,growth_or_fall,power_level,status\n",
        )
    foreshadowing = project / "03_new_novel" / "foreshadowing_ledger.md"
    if not foreshadowing.exists():
        write_text(
            foreshadowing,
            "# Foreshadowing Ledger\n\n从第0001章重写开始记录。旧稿伏笔台账已归档；后续必须按新版 novel_bible、full_story_outline、chapter_plan、power_system 和 power_state_ledger 重新埋设与回收。\n",
        )
    return archived


def reset_project_writing_only(project: Path) -> Path:
    _ensure_rewrite_basis(project)
    backup = project / "_backups" / f"writing_reset_{timestamp()}"
    backup.mkdir(parents=True, exist_ok=True)
    state_path = project / "00_config" / "agent_state.json"
    old_state = load_json(state_path) if state_path.exists() else {}
    if state_path.exists():
        shutil.copy2(state_path, backup / "agent_state_before.json")
    stale_runtime_files = _archive_stale_rewrite_runtime_files(project, backup)
    basis_snapshot = _write_rewrite_basis_snapshot(project)
    for rel in (
        "04_prompts",
        "05_drafts",
        "06_summaries",
        "07_reviews",
        "08_exports",
        "09_handoff",
    ):
        source = project / rel
        if source.exists():
            target = backup / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
        (project / rel).mkdir(parents=True, exist_ok=True)

    now = dt.datetime.now().isoformat(timespec="seconds")
    new_state = {
        "phase": "writing",
        "current_chapter": 1,
        "awaiting": None,
        "approvals": old_state.get("approvals") or [],
        "created_at": old_state.get("created_at") or now,
        "updated_at": now,
        "activity": f"已仅归档正文产物和旧稿衍生记忆，准备按当前新版设定/800章章纲/力量体系从第0001章重写：{backup}",
        "writing_reset_backup": str(backup),
        "rewrite_basis_snapshot": str(basis_snapshot),
        "rewrite_basis_files": list(REWRITE_BASIS_FILES),
        "archived_stale_runtime_files": stale_runtime_files,
        "rewrite_notes": "下一次 autopilot 写正文时会读取当前 03_new_novel 的 novel_bible、style_guide、power_system、power_state_ledger、full_story_outline、chapter_plan、continuity_ledger、memory_rollup；不会重新读取原书或重新生成大纲。",
    }
    for key in ("selected_outline_candidate", "selected_outline_path", "reset_backup"):
        if old_state.get(key):
            new_state[key] = old_state[key]
    write_json(state_path, new_state)
    return backup


def command_init(args: argparse.Namespace) -> None:
    profiles = load_profiles()
    if args.platform not in profiles:
        raise SystemExit(f"未知平台：{args.platform}。可选：{', '.join(profiles)}")

    name = args.name.strip()
    project = Path(args.project_dir).expanduser().resolve() if args.project_dir else (PROJECTS_ROOT / slugify(name)).resolve()
    mkdirs(project)

    config = {
        "name": name,
        "platform": args.platform,
        "title": args.title or "",
        "source_dir": str(Path(args.source_dir).expanduser().resolve()) if args.source_dir else "",
        "target_chapters": args.target_chapters,
        "batch_size": args.batch_size or 30,
        "policy": {
            "source_use": "默认采用完全融合授权复用：优先深读多本源书并完整复用、拼合、优化其中的爽点、故事情节、剧情链、事件顺序、场景媒介、关键转折、人物功能设定、人物关系、境界体系、设定骨架和全书走向，并记录来源映射。",
            "forbidden": "人物名和专有人名不能照搬，必须改名；不要整段逐字原样粘贴原文。授权复用模式不把爽点、故事情节、完整剧情链、人物设定、境界体系或设定骨架列为禁用项；源文句子不要大幅改写，不要改变原句意思和基本句式，只做词语级近义替换、称谓替换、专名替换和少量衔接替换，不能改成 AI 味解释腔。",
            "audit": "每批正文可运行 similarity audit，发现长串重合需重写。",
        },
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    write_json(project / "00_config" / "project.json", config)

    for rel, content in RUNTIME_SEED_FILES.items():
        target = project / rel
        if not target.exists():
            write_text(target, content)

    readme = f"""# {name}

平台：{profiles[args.platform]['display_name']}

## 下一步

1. 把同题材/同类型小说放到一个目录。
2. 运行：

```powershell
python -m novel_agent ingest --project "{project}" --source-dir "你的小说目录"
python -m novel_agent make-prompt --project "{project}" --stage decompose --provider manus
```

3. 用生成的提示词让 Manus / ChatGPT / Claude 完成拆解。
4. 保存输出到 `10_inbox`，再用 `accept` 归档。
5. 继续生成 `blueprint` 和 `chapters` 提示词。

注意：GPT Plus、Claude Pro、Manus Pro 是网页/客户端订阅，不等于 API key。本工作流默认走“本地组织 + 手动/Manus 执行”。如果以后你有 API key，可以在这个项目上继续加自动调用层。
"""
    write_text(project / "README.md", readme)
    print(f"已创建项目：{project}")


def command_start(args: argparse.Namespace) -> None:
    if not args.source_dir:
        raise SystemExit("start 需要 --source-dir。")
    command_init(args)
    project = Path(args.project_dir).expanduser().resolve() if args.project_dir else (PROJECTS_ROOT / slugify(args.name)).resolve()
    command_ingest(argparse.Namespace(project=str(project), source_dir=args.source_dir))
    command_make_prompt(
        argparse.Namespace(
            project=str(project),
            stage="decompose",
            provider=args.provider,
            start=0,
            count=0,
        )
    )
    print("启动包已完成：项目、源书索引、第一步融合拆解提示词都已生成。")


def ensure_project_for_agent(args: argparse.Namespace) -> Path:
    if args.project:
        project = resolve_project(args.project)
        if not (project / "01_sources" / "source_index.json").exists():
            source_dir = args.source_dir or load_project_config(project).get("source_dir") or ""
            if not source_dir:
                raise SystemExit("缺少源书索引，请先填写源书目录后再启动。")
            command_ingest(argparse.Namespace(project=str(project), source_dir=source_dir))
            args.source_dir = source_dir
        return project
    if not args.name or not args.source_dir:
        raise SystemExit("不指定 --project 时，必须提供 --name 和 --source-dir。")
    project = Path(args.project_dir).expanduser().resolve() if args.project_dir else (PROJECTS_ROOT / slugify(args.name)).resolve()
    if not (project / "00_config" / "project.json").exists():
        command_init(args)
    if not (project / "01_sources" / "source_index.json").exists():
        command_ingest(argparse.Namespace(project=str(project), source_dir=args.source_dir))
    return project


def command_autopilot(args: argparse.Namespace) -> None:
    from .autopilot import AutoPilot, build_auto_options
    from .llm_client import LLMClient

    project = ensure_project_for_agent(args)
    config = load_project_config(project)
    provider = args.provider
    default_base_url = args.base_url or ("http://127.0.0.1:8080" if args.provider == "sub2api" else "")
    llm = LLMClient.from_values(
        provider=provider,
        model=args.model,
        api_key=args.api_key,
        api_keys=read_api_keys_file(args.api_keys_file),
        base_url=default_base_url,
        timeout=args.timeout,
        retries=args.retries,
    )
    options = build_auto_options(args, config_batch_size=config.get("batch_size") or 30)
    if args.engine == "langgraph":
        from .langgraph_engine import run_langgraph

        message = run_langgraph(project, llm, options)
    else:
        pilot = AutoPilot(project, llm, options)
        message = pilot.run()
    print(message)


def command_revise(args: argparse.Namespace) -> None:
    from .autopilot import AutoOptions, AutoPilot
    from .llm_client import LLMClient

    project = resolve_project(args.project)
    provider = args.provider
    default_base_url = args.base_url or ("http://127.0.0.1:8080" if args.provider == "sub2api" else "")
    llm = LLMClient.from_values(
        provider=provider,
        model=args.model,
        api_key=args.api_key,
        api_keys=read_api_keys_file(args.api_keys_file),
        base_url=default_base_url,
        timeout=args.timeout,
        retries=args.retries,
    )
    options = AutoOptions(
        fusion_mode=args.fusion_mode,
        max_tokens_blueprint=args.max_tokens_blueprint,
        stream_preview=args.stream_preview,
    )
    pilot = AutoPilot(project, llm, options)
    print(pilot.revise_current_checkpoint(args.note))


def command_power_rewrite(args: argparse.Namespace) -> None:
    from .autopilot import AutoOptions, AutoPilot
    from .llm_client import LLMClient

    project = resolve_project(args.project)
    provider = args.provider
    default_base_url = args.base_url or ("http://127.0.0.1:8080" if args.provider == "sub2api" else "")
    llm = LLMClient.from_values(
        provider=provider,
        model=args.model,
        api_key=args.api_key,
        api_keys=read_api_keys_file(args.api_keys_file),
        base_url=default_base_url,
        timeout=args.timeout,
        retries=args.retries,
    )
    options = AutoOptions(
        max_tokens_chapter=args.max_tokens_chapter,
        stream_preview=args.stream_preview,
    )
    pilot = AutoPilot(project, llm, options)
    print(pilot.revise_power_range(args.start, args.end))


def command_trial_write(args: argparse.Namespace) -> None:
    from .autopilot import AutoOptions, AutoPilot
    from .llm_client import LLMClient

    project = resolve_project(args.project)
    provider = args.provider
    default_base_url = args.base_url or ("http://127.0.0.1:8080" if args.provider == "sub2api" else "")
    llm = LLMClient.from_values(
        provider=provider,
        model=args.model,
        api_key=args.api_key,
        api_keys=read_api_keys_file(args.api_keys_file),
        base_url=default_base_url,
        timeout=args.timeout,
        retries=args.retries,
    )
    options = AutoOptions(
        max_tokens_chapter=args.max_tokens_chapter,
        temperature=args.temperature,
        stream_preview=args.stream_preview,
    )
    pilot = AutoPilot(project, llm, options)
    print(pilot.write_trial_batch(args.start, args.end))


def command_promote_trial(args: argparse.Namespace) -> None:
    from .autopilot import AutoOptions, AutoPilot
    from .llm_client import LLMClient

    project = resolve_project(args.project)
    pilot = AutoPilot(project, LLMClient.from_values(provider="mock", model="mock"), AutoOptions())
    print(pilot.promote_trial_range(args.start, args.end))


def command_continue_outline(args: argparse.Namespace) -> None:
    from .autopilot import AutoOptions, AutoPilot
    from .llm_client import LLMClient

    project = resolve_project(args.project)
    provider = args.provider
    default_base_url = args.base_url or ("http://127.0.0.1:8080" if args.provider == "sub2api" else "")
    llm = LLMClient.from_values(
        provider=provider,
        model=args.model,
        api_key=args.api_key,
        api_keys=read_api_keys_file(args.api_keys_file),
        base_url=default_base_url,
        timeout=args.timeout,
        retries=args.retries,
    )
    options = AutoOptions(
        fusion_mode=args.fusion_mode,
        max_tokens_blueprint=args.max_tokens_blueprint,
        stream_preview=args.stream_preview,
    )
    pilot = AutoPilot(project, llm, options)
    print(pilot.continue_chapter_plan_until(args.until))


def command_reset_rebuild(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    config = load_project_config(project)
    source_dir = args.source_dir or config.get("source_dir") or ""
    if not source_dir:
        raise SystemExit("项目配置里没有 source_dir，请传 --source-dir。")
    backup = reset_project_for_rebuild(project)
    print(f"旧产物已归档：{backup}")
    command_ingest(argparse.Namespace(project=str(project), source_dir=source_dir))
    args.source_dir = source_dir
    command_autopilot(args)


def command_reset_writing(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    backup = reset_project_writing_only(project)
    print(f"正文产物已归档：{backup}")
    print("已保留源书拆解、素材池、融合设定包和章纲；下一次运行 autopilot 会从第0001章开始写正文。")


def command_approve(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    state_path = project / "00_config" / "agent_state.json"
    if not state_path.exists():
        raise SystemExit("没有找到 agent_state.json，尚未启动 autopilot。")
    state = load_json(state_path)
    awaiting = state.get("awaiting")
    if not awaiting:
        print("当前没有等待确认的节点。")
        return
    if awaiting.get("type") == "outline_selection":
        print("当前是候选大纲选择节点。请先选择候选大纲 1/2/3，再继续生成完整设定包。")
        return
    state.setdefault("approvals", []).append(
        {
            "type": awaiting.get("type"),
            "note": args.note,
            "approved_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
    )
    if awaiting.get("type") == "blueprint":
        state["phase"] = "writing"
    state["awaiting"] = None
    state["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    write_json(state_path, state)
    print("已确认。现在可以继续运行 autopilot。")


def command_select_outline(args: argparse.Namespace) -> None:
    from .autopilot import AutoOptions, AutoPilot
    from .llm_client import LLMClient, LLMConfig

    project = resolve_project(args.project)
    pilot = AutoPilot(project, LLMClient(LLMConfig(provider="mock", model="mock")), AutoOptions())
    print(pilot.select_outline_candidate(args.candidate))


def command_agent_status(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    command_status(args)
    state_path = project / "00_config" / "agent_state.json"
    if not state_path.exists():
        print("自动状态：未启动 autopilot")
        return
    state = load_json(state_path)
    print(f"自动阶段：{state.get('phase')}")
    print(f"下一章：{state.get('current_chapter')}")
    awaiting = state.get("awaiting")
    if awaiting:
        print(f"等待确认：{awaiting.get('type')} - {awaiting.get('message')}")
    else:
        print("等待确认：无")


def command_test_llm(args: argparse.Namespace) -> None:
    from .llm_client import LLMClient

    provider = args.provider
    base_url = args.base_url or ("http://127.0.0.1:8080" if args.provider == "sub2api" else "")
    llm = LLMClient.from_values(
        provider=provider,
        model=args.model,
        api_key=args.api_key,
        api_keys=read_api_keys_file(args.api_keys_file),
        base_url=base_url,
        timeout=args.timeout,
        retries=0,
    )
    text = llm.complete(
        "请只回复：连接成功",
        system="你是连通性测试助手。",
        max_tokens=64,
        temperature=0,
    )
    print(text.strip())


def iter_source_files(source_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in source_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SOURCE_EXTS:
            files.append(path)
    return sorted(files, key=lambda p: str(p).lower())


def group_source_files(source_dir: Path, files: list[Path]) -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = {}
    for path in files:
        rel = path.relative_to(source_dir)
        if len(rel.parts) == 1:
            key = path.stem
        else:
            key = rel.parts[0]
        groups.setdefault(key, []).append(path)
    return dict(sorted(groups.items(), key=lambda item: item[0].lower()))


CHAPTER_RE = re.compile(
    r"^\s*(第[0-9零〇一二两三四五六七八九十百千万]+[章节回卷集][^\n\r]{0,40}|Chapter\s+\d+[^\n\r]{0,40})\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def detect_chapter_titles(text: str, limit: int = 80) -> list[str]:
    titles = [match.group(1).strip() for match in CHAPTER_RE.finditer(text)]
    deduped: list[str] = []
    seen = set()
    for title in titles:
        if title not in seen:
            deduped.append(title)
            seen.add(title)
        if len(deduped) >= limit:
            break
    return deduped


def excerpt(text: str, size: int = 900) -> dict[str, str]:
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(text) <= size * 3:
        return {"opening": text[:size], "middle": text[size : size * 2], "ending": text[size * 2 : size * 3]}
    mid = max(0, len(text) // 2 - size // 2)
    return {
        "opening": text[:size],
        "middle": text[mid : mid + size],
        "ending": text[-size:],
    }


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_ingest(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    config = load_project_config(project)
    source_dir = Path(args.source_dir or config.get("source_dir") or "").expanduser().resolve()
    if not source_dir.exists():
        raise SystemExit(f"源书目录不存在：{source_dir}")

    files = iter_source_files(source_dir)
    if not files:
        raise SystemExit(f"没有找到源书文件。支持扩展名：{', '.join(sorted(SUPPORTED_SOURCE_EXTS))}")

    groups = group_source_files(source_dir, files)
    books: list[dict[str, Any]] = []
    for title, book_files in groups.items():
        total_chars = 0
        all_titles: list[str] = []
        hashes = []
        combined_sample = []
        for path in book_files:
            text = read_text(path)
            total_chars += len(text)
            hashes.append({"path": str(path), "sha1": sha1_file(path), "chars": len(text)})
            all_titles.extend(detect_chapter_titles(text, limit=40))
            if len("".join(combined_sample)) < 12000:
                combined_sample.append(text[:4000])

        sample_text = "\n\n".join(combined_sample)
        samples = excerpt(sample_text)
        book = {
            "title": title,
            "safe_name": safe_name(title),
            "file_count": len(book_files),
            "total_chars": total_chars,
            "files": hashes,
            "detected_chapter_titles": all_titles[:80],
        }
        books.append(book)

        card = [
            f"# {title}",
            "",
            f"- 文件数：{len(book_files)}",
            f"- 估算字符数：{total_chars}",
            f"- 源路径：`{source_dir}`",
            "",
            "## 章节标题抽样",
            "",
        ]
        if all_titles:
            card.extend(f"- {line}" for line in all_titles[:60])
        else:
            card.append("- 未检测到标准章节标题，请在拆解阶段按文件内容判断。")
        card.extend(
            [
                "",
                "## 文本抽样（用于识别并融合优秀设计，正文生成时做词语级近义替换）",
                "",
                "### 开头抽样",
                "",
                samples["opening"],
                "",
                "### 中段抽样",
                "",
                samples["middle"],
                "",
                "### 末段抽样",
                "",
                samples["ending"],
                "",
                "## 融合提醒",
                "",
                "- 优先记录可参与跨书融合的境界/能力机制、人物关系、剧情节点、故事走向、爽点机制和节奏结构。",
                "- 默认完全融合授权复用：可以完整沿用并跨书整段融合优秀的爽点、剧情链、事件顺序、人物功能、升级段、境界体系和设定骨架；人物名和专有人名必须改名；源句不要大幅改写，不改变原意和基本句式，只做词语级近义替换、称谓替换、专名替换和少量衔接替换，不能整段逐字原样粘贴，也不能改成 AI 味解释腔。",
            ]
        )
        write_text(project / "01_sources" / "source_cards" / f"{safe_name(title)}.md", "\n".join(card))

    index = {
        "source_dir": str(source_dir),
        "ingested_at": dt.datetime.now().isoformat(timespec="seconds"),
        "book_count": len(books),
        "file_count": len(files),
        "books": books,
    }
    write_json(project / "01_sources" / "source_index.json", index)

    manifest_lines = [
        "# Source Manifest",
        "",
        f"- 源书目录：`{source_dir}`",
        f"- 书籍/分组数：{len(books)}",
        f"- 文件数：{len(files)}",
        f"- 导入时间：{index['ingested_at']}",
        "",
        "| # | 书名/分组 | 文件数 | 字符数 | 章节标题抽样 |",
        "|---:|---|---:|---:|---|",
    ]
    for idx, book in enumerate(books, start=1):
        first_titles = "；".join(book["detected_chapter_titles"][:3]) or "未检测"
        manifest_lines.append(
            f"| {idx} | {book['title']} | {book['file_count']} | {book['total_chars']} | {first_titles} |"
        )
    write_text(project / "01_sources" / "source_manifest.md", "\n".join(manifest_lines) + "\n")

    config["source_dir"] = str(source_dir)
    save_project_config(project, config)
    print(f"已导入 {len(books)} 个书籍/分组，{len(files)} 个文件。")
    print(f"清单：{project / '01_sources' / 'source_manifest.md'}")


def read_optional(path: Path, max_chars: int = 20000) -> str:
    if not path.exists():
        return f"（缺失：{path.name}）"
    text = read_text(path)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n...[已截断，原文件约 {len(text)} 字符]..."


def load_chapter_plan_rows(path: Path, start: int, end: int) -> list[dict[str, str]]:
    if not path.exists():
        return []
    text = read_text(path)
    if PLACEHOLDER in text:
        return []
    rows: list[dict[str, str]] = []
    reader = csv.DictReader(text.splitlines())
    for row in reader:
        raw_no = (row.get("chapter_no") or "").strip()
        try:
            no = int(re.sub(r"\D", "", raw_no))
        except ValueError:
            continue
        if start <= no <= end:
            rows.append(row)
    return rows


def markdown_table(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "（暂无章节细纲，请先生成或粘贴 chapter_plan.csv）"
    fields = list(rows[0].keys())
    lines = ["| " + " | ".join(fields) + " |", "|" + "|".join("---" for _ in fields) + "|"]
    for row in rows:
        lines.append("| " + " | ".join((row.get(field) or "").replace("\n", " ") for field in fields) + " |")
    return "\n".join(lines)


def recent_summaries(project: Path, max_files: int = 3) -> str:
    files = sorted((project / "06_summaries").glob("summary_*.md"))
    if not files:
        return "（暂无最近摘要）"
    chunks = []
    for path in files[-max_files:]:
        chunks.append(f"## {path.name}\n\n{read_optional(path, 8000)}")
    return "\n\n".join(chunks)


def previous_draft_tail(project: Path, start: int, chars: int = 900) -> str:
    drafts = sorted((project / "05_drafts").glob("ch_*_to_*.md"))
    previous = []
    for path in drafts:
        match = re.search(r"ch_(\d+)_to_(\d+)", path.name)
        if not match:
            continue
        end = int(match.group(2))
        if end < start:
            previous.append((end, path))
    if not previous:
        return "（暂无上一章正文）"
    _, path = sorted(previous)[-1]
    text = read_text(path)
    return text[-chars:]


def draft_text_for_range(project: Path, start: int, end: int, max_chars: int = 260_000) -> str:
    draft_dir = project / "05_drafts"
    files: list[tuple[int, int, Path]] = []
    for path in sorted(draft_dir.glob("ch_*_to_*.md")):
        match = re.search(r"ch_(\d+)_to_(\d+)", path.name)
        if not match:
            continue
        file_start = int(match.group(1))
        file_end = int(match.group(2))
        if file_end < start or file_start > end:
            continue
        files.append((file_start, file_end, path))

    if not files:
        return f"（缺失：未找到覆盖第 {start:04d}-{end:04d} 章的正文批次文件）"

    chunks: list[str] = []
    covered: set[int] = set()
    remaining = max_chars
    for file_start, file_end, path in files:
        for number in range(max(start, file_start), min(end, file_end) + 1):
            covered.add(number)
        text = read_text(path)
        header = f"## 正文文件：{path.name}（覆盖第 {file_start:04d}-{file_end:04d} 章）\n\n"
        if remaining <= len(header):
            chunks.append("## 审稿输入提示\n\n正文过长，后续批次已因长度限制截断；请优先审已提供正文。")
            break
        body_limit = remaining - len(header)
        if len(text) > body_limit:
            text = text[:body_limit] + "\n\n（后文因审稿输入长度限制截断。）"
        chunks.append(header + text)
        remaining -= len(chunks[-1])
        if remaining <= 0:
            break

    missing = [number for number in range(start, end + 1) if number not in covered]
    if missing:
        chunks.insert(
            0,
            "## 审稿输入警告\n\n"
            f"以下章节没有找到正文批次覆盖：{', '.join(f'{number:04d}' for number in missing)}\n",
        )
    return "\n\n".join(chunks)


def provider_note(provider: str) -> str:
    if provider == "manus":
        return (
            "你在 Manus Project / Agent 中执行，可以读取用户授权的本地项目文件；"
            "需要保存文件时，请按提示中的输出路径写入。"
        )
    if provider == "chatgpt":
        return (
            "你在 ChatGPT Plus 对话中执行，不能直接写入本地文件；"
            "请只输出结果，用户会把结果保存到本地项目。"
        )
    if provider == "claude":
        return (
            "你在 Claude Pro 对话中执行，不能直接写入本地文件；"
            "请保持长文一致性，按要求输出可归档内容。"
        )
    return "按当前对话能力执行；不要臆造未提供的本地文件内容。"


def prompt_decompose(project: Path, config: dict[str, Any], profile: dict[str, Any], provider: str) -> str:
    manifest = read_optional(project / "01_sources" / "source_manifest.md", 12000)
    source_dir = config.get("source_dir", "")
    return f"""# 源书拆解任务

{provider_note(provider)}

项目目录：`{project}`
源书目录：`{source_dir}`

## 平台分类

{platform_block(profile)}

## 创作与合规边界

这批源书是用户提供的融合素材库；任务不是从零原创大纲，而是做“强融合大纲”的素材拆解。请具体抽取值得继承的境界/能力机制、人物关系、反派压迫方式、剧情节点、故事走向、爽点结构、章节节奏、开篇钩子和高潮模型。

{SOURCE_BACKBONE_CLONE_RULE}

{FAST_PAYOFF_NO_DOWNGRADE_RULE}

如果用户导入的是当前目标平台排行榜/同类榜单源书，这批源书就是该目标平台的核心素材库。必须优先读取并融合这些源书对应该平台的爽点、整段剧情链、升级段落、人物功能段、节奏密度、章末钩子和卖点结构；在逻辑合理的前提下跨书拼接，不要另起一套原创情节。这条适用于飞卢、番茄、起点或任何其他平台，不只针对飞卢。

必须从多个源书中选取好用设计并标注保留/拼合/照搬/优化方向，不能为了追求原创性而将源书优势换掉。当前工作流默认完全融合授权复用：爽点、故事情节、剧情链、事件顺序、场景媒介、关键转折、人物功能设定、人物关系、升级段、境界体系和设定骨架都可以完整保留并跨书融合；人物名和专有人名不能照搬，必须改名；背景、势力名、地名、设定名、金手指名和承接细节按本书统一，再在原有基础上优化。后续新书必须统一主线、世界观、能力规则、角色关系和反派升级链。

## 已导入源书清单

{manifest}

## 执行要求

1. 逐本拆解，但不要大段复述原文。
2. 每本输出：定位、卖点、可保留的境界/能力设计、人物关系、剧情节点、故事走向、核心爽点、高潮模型、源书标题口味、开场方式、章末承接方式、适合与哪些书拼合。
3. 把具体可融合设计归类成素材池，而不是直接生成新小说正文。
4. 每条素材尽量标注来源书名，方便追溯。
5. 单独整理可完整沿用和融合的剧情链、事件顺序、场景媒介、关键转折、人物功能设定、人物关系、升级段、境界体系、设定骨架，以及必须替换的人物名/专有人名、需要统一改写的名称与背景信息；不要把完整剧情链列为禁用项。

## 输出文件

请生成/更新以下内容：

- `02_source_analysis/source_bibles.md`
- `02_source_analysis/source_style_profile.md`
- `02_source_analysis/motif_library.csv`
- `02_source_analysis/character_pool.csv`
- `02_source_analysis/plot_pool.csv`
- `02_source_analysis/power_system_pool.csv`
- `02_source_analysis/fusion_opportunities.md`
- `02_source_analysis/source_risk_notes.md`

完成后输出：素材拆解完成，可以进入融合设定阶段。
"""


def prompt_blueprint(project: Path, config: dict[str, Any], profile: dict[str, Any], provider: str) -> str:
    analysis_pack = "\n\n".join(
        [
            "## source_bibles.md\n" + read_optional(project / "02_source_analysis" / "source_bibles.md", 18000),
            "## source_style_profile.md\n" + read_optional(project / "02_source_analysis" / "source_style_profile.md", 9000),
            "## fusion_opportunities.md\n" + read_optional(project / "02_source_analysis" / "fusion_opportunities.md", 12000),
            "## source_risk_notes.md\n" + read_optional(project / "02_source_analysis" / "source_risk_notes.md", 12000),
        ]
    )
    return f"""# 融合设定包生成任务

{provider_note(provider)}

项目目录：`{project}`
目标平台：{profile['display_name']}
目标总体量：约 200-300 万字；请根据融合后的主线和节奏自行决定总章数，并明确写出计划总章数。

## 平台分类

{platform_block(profile)}

## 素材拆解摘要

{analysis_pack}

## 任务边界

这一步只生成“融合设定包”和章节规划，不写正文。当前工作流默认完全融合授权复用：不要为了追求原创感而换掉源书中好用的设计；请从多本源书中具体选择并拼合境界/能力机制、人物关系、关键剧情模块、升级高潮和全书走向，同时记录来源映射。若源书来自当前目标平台榜单，必须优先融合该平台源书的整段爽点链、剧情链、升级段、人物功能段、节奏密度和章末钩子；这条适用于所有平台，不只针对飞卢。爽点段、剧情链、人物功能段、升级段和境界体系可以整段融合；人物名和专有人名必须改名；后续正文不要大幅改写源句，不改变原意和基本句式，只做词语级近义替换、称谓替换、专名替换和少量衔接替换，也不要改成 AI 味解释腔。

{SOURCE_BACKBONE_CLONE_RULE}

{FAST_PAYOFF_NO_DOWNGRADE_RULE}

## 必须输出

1. `03_new_novel/novel_bible.md`
2. `03_new_novel/style_guide.md`
3. `03_new_novel/power_system.md`
4. `03_new_novel/power_state_ledger.md`
5. `03_new_novel/worldbuilding.md`
6. `03_new_novel/character_table.csv`
7. `03_new_novel/volume_outline.md`
8. `03_new_novel/full_story_outline.md`
9. `03_new_novel/fusion_traceability.md`
10. `03_new_novel/chapter_plan.csv`
11. `03_new_novel/continuity_ledger.md`
12. `03_new_novel/foreshadowing_ledger.md`
13. `03_new_novel/memory_rollup.md`

## 设定包要求

- 小说名称：强题材、强卖点、适配平台。
- 一句话简介：15-30 字，突出金手指、冲突和目标。
- 详细简介：100-180 字，能直接用于投稿/发布。
- 世界观：清楚、可扩展，不堆名词。
- 金手指：激活方式、限制、升级方式、代价、爽点兑现方式。
- 战力台账：`power_state_ledger.md` 必须持续记录主角当前修为/等级、当前小段或阶段、核心能力解锁状态、关键道具权限、越级依据、伤势/反噬代价和下一突破门槛。
- 主角：主动、讨喜、有短中长期目标。
- 反派：不降智，有压迫链和升级链。
- `style_guide.md`：只能根据 `source_style_profile.md` 和源书证据整理读感来源，不写固定文风命令，不预设标题模板。
- 卷纲：8-12 卷，每卷用事件因果链写清起因、阻碍、主角动作、结果和下一卷承接，不写抽象阶段报告。
- 全书规划：`full_story_outline.md` 必须先根据 200-300 万字体量和节奏确定计划总章数，再覆盖第 001 章到最后一章，按卷和 10-30 章段写清事件因果、能力/资源变化、反派更替、阶段结果、最终卷和大结局收束。
- 融合溯源：`fusion_traceability.md` 必须说明力量体系、主要人物关系和每卷剧情分别融合了哪些源书素材，哪些是保留、拼合或改造。
- 章节计划：`chapter_plan.csv` 必须从第 001 章逐章生成到大纲确定的大结局终章，不能只做前 200 章。字段必须包含：
  `chapter_no,volume,title,core_conflict,small_hook,power_usage,character_change,foreshadowing,ending_hook,mainline_progress,source_inspiration,status`
- `chapter_plan.csv` 每一章必须是“可直接写正文的具体情节”，不能只写方向词。`title` 只是内部工作标题，不是正文最终标题，不能为了固定模板造词；`core_conflict` 必须写清触发事件、地点/人物、阻碍、主角反制、本章结果；`small_hook` 必须是开场能立刻入戏的具体触发点；`ending_hook` 必须是章末具体画面/新动作/新威胁；`mainline_progress` 必须写清章后局面和下一章承接。
- 章纲里不要写“推进主线、压迫升级、继续调查、进入新地图、埋下伏笔、关系变化”这种空泛功能句；必须换成具体谁拿什么压主角、主角用什么办法拆局、最后拿到什么结果。
- 如果用户要求“重写章纲、详细章纲、具体情节事件、去 AI 味章纲”，这是要求 Agent 重写 `chapter_plan.csv` 和 `chapter_plan_parts`，不是要求直接写正文；重写后的每章必须包含“触发事件 -> 阻碍/对手 -> 主角反制 -> 爽点兑现 -> 章末承接”。
- “去 AI 味”在设定/章纲阶段的含义是去掉空泛总结腔、抽象设定腔和方向词，把章纲改成可执行事件链；不要在本步骤擅自输出正文。

完成后输出：融合设定包已完成，请用户确认后进入正文生产阶段。
"""


def prompt_chapters(
    project: Path,
    config: dict[str, Any],
    profile: dict[str, Any],
    provider: str,
    start: int,
    count: int,
) -> str:
    end = start + count - 1
    next_start = end + 1
    next_end = end + count
    rows = load_chapter_plan_rows(project / "03_new_novel" / "chapter_plan.csv", start, end)
    context = "\n\n".join(
        [
            "## novel_bible.md\n" + read_optional(project / "03_new_novel" / "novel_bible.md", 14000),
            "## style_guide.md\n" + read_optional(project / "03_new_novel" / "style_guide.md", 9000),
            "## power_system.md\n" + read_optional(project / "03_new_novel" / "power_system.md", 9000),
            "## power_state_ledger.md\n" + read_optional(project / "03_new_novel" / "power_state_ledger.md", 9000),
            "## worldbuilding.md\n" + read_optional(project / "03_new_novel" / "worldbuilding.md", 9000),
            "## character_table.csv\n" + read_optional(project / "03_new_novel" / "character_table.csv", 9000),
            "## volume_outline.md\n" + read_optional(project / "03_new_novel" / "volume_outline.md", 12000),
            "## full_story_outline.md\n" + read_optional(project / "03_new_novel" / "full_story_outline.md", 18000),
            f"## 第 {start:03d}-{end:03d} 章细纲\n" + markdown_table(rows),
            "## 最近摘要\n" + recent_summaries(project),
            "## 上一章尾段\n" + previous_draft_tail(project, start),
        ]
    )
    return f"""# 第 {start:03d}-{end:03d} 章正文生产

{provider_note(provider)}

项目目录：`{project}`
目标平台：{profile['display_name']}

## 平台风格

{platform_block(profile)}

## 本批上下文包

{context}

## 写作任务

请生成第 {start:03d}-{end:03d} 章正文。不要读取源书全文，不要重构设定，不要新增无边界能力。当前工作流默认完全融合授权复用，必须优先沿用并融合已经选定的爽点、故事链、事件顺序、人物功能、境界体系和设定骨架，再优化节奏与逻辑。正文表达按当前项目统一生成：人物名和专有人名必须替换；源文句子不要大幅改写，不要改变原句意思和基本句式，只做词语级近义替换、称谓替换、专名替换和少量衔接替换；保留原句语气、动作顺序、对白功能、信息量和爽点力度，不能整段逐字原样粘贴，也不能改成 AI 味解释腔。这是所有平台通用规则，不只针对飞卢。

{SOURCE_BACKBONE_CLONE_RULE}

{FAST_PAYOFF_NO_DOWNGRADE_RULE}

写作前先输出“本批爽点安排表”，字段：

- 章节号
- 标题
- 核心冲突
- 本章小爽点
- 中爽点位置
- 金手指使用方式
- 结尾钩子
- 主线推进

正文要求：

1. 每章 {profile['chapter_words']} 字。
2. 每章结构：铺垫 -> 冲突 -> 爽点兑现 -> 结尾钩子。
3. 每章至少 1 个小爽点；每 3-5 章至少 1 个中爽点。
4. 必须严格按本批章纲写：`core_conflict` 是本章事件主链，按“触发事件 -> 阻碍/对手 -> 主角反制 -> 本章结果”写完；`small_hook` 是开场触发；`ending_hook` 是章末钩子；`mainline_progress` 是本章必须完成的结果。
5. 如果某章细纲不够具体，先基于 novel_bible、continuity_ledger、上一章尾段和 full_story_outline 补成具体事件链，再写正文；不要只按标题自由发挥。
6. 主角主动推进，不能长期憋屈。
7. 反派有逻辑，但节奏上被主角反制。
8. 语言符合目标平台，不水日常，不连续长篇解释设定。
9. 禁止 AI 味自造词和硬造缩略词；优先使用当前题材网文里已有的成熟表达。读者需要猜意思的两字/三字压缩词，必须改成完整普通短语。
10. 必须遵守 `power_state_ledger.md`：涉及战斗、升级、测修为、公开压场时，写清主角当前修为/等级、当前小段或阶段、核心能力解锁状态、关键道具权限、敌我差距、胜负依据和代价；每 10 章更新一次战力台账。
11. 第 {end:03d} 章结尾必须钩住第 {next_start:03d} 章。

输出顺序：

1. 本批爽点安排表。
2. 第 {start:03d}-{end:03d} 章正文。
3. 每章 100-200 字摘要。
4. 角色状态变化。
5. 金手指/等级变化。
6. 新增伏笔。
7. 回收伏笔。
8. 下一批第 {next_start:03d}-{next_end:03d} 章承接提示。
"""


def prompt_review(project: Path, config: dict[str, Any], profile: dict[str, Any], provider: str, start: int, count: int) -> str:
    end = start + count - 1
    draft_text = draft_text_for_range(project, start, end)
    return f"""# 第 {start:03d}-{end:03d} 章审稿任务

{provider_note(provider)}

目标平台：{profile['display_name']}

## 平台风格

{platform_block(profile)}

## 待审稿正文

{draft_text}

## 力量体系参考

### power_system.md
{read_optional(project / "03_new_novel" / "power_system.md", 7000)}

### power_state_ledger.md
{read_optional(project / "03_new_novel" / "power_state_ledger.md", 7000)}

## 审稿维度

1. 平台节奏是否匹配。
2. 每章是否有冲突、爽点和结尾钩子。
3. 主角是否主动，是否出现憋屈拖沓。
4. 金手指是否符合既定规则。
5. 境界与战力台账是否清晰：是否写明主角当前修为/等级、当前小段或阶段、核心能力解锁状态、关键道具权限、敌我差距、胜负依据和越级代价。
6. 人物关系、伏笔、设定是否自洽。
7. 是否存在人物名/专名未替换、整段逐字原样粘贴，或把源句意思改偏、基本句式改散、改成 AI 味解释腔的问题；授权复用的故事链、事件顺序、境界体系和设定骨架，不要仅因与源书一致就判错。
8. 语言是否通顺自然： 重点找 AI 腔、硬造词、硬拼名词、读者看不懂的抽象句、生硬压缩句。
尤其检查硬造缩略词： 如“撕史、审史、禁关、封史、验史、破册、撕账、瞳震、唇启、眸闪、识扫”等。
尤其检查 AI 常用抽象词： 如“深入研究、见证、格局、赋能、解锁、核心要义、细致入微、不可磨灭、范式、协同”等。
修正要求： 识别出这些词后，必须给出普通读者一眼能懂的改法。例如：
“撕史” -> 改为“撕碎了记载这段历史的卷轴”或“抹除这段历史”。
“瞳震” -> 改为“瞳孔骤然收缩”或“满脸震惊”。
“赋能” -> 改为“给予力量”或“提供支持”。
“深入研究” -> 改为“仔细查看”或“钻研”。
9. 对话是否像真人说话：是否符合角色身份、当前情绪和利益目的，是否存在旁白腔解释设定、台词过长或不合逻辑。
10. 章节标题是否短促、贴合本章内容、有当前目标平台的目录钩子；不要为了四字标题牺牲通顺和准确。

输出：

- 问题清单，按严重程度排序。
- 逐章修改建议。
- 必须重写的段落说明。
- 单独列出“境界/战力台账问题”：指出哪一章没写清当前修为/等级、核心能力状态、关键道具权限、敌我差距、越级依据或代价，并给出补法。
- 单独列出“语句/词语不通顺清单”：引用原句或原词，说明问题，给出建议改法。
- 单独列出“标题问题清单”：说明标题是否不通顺、不贴内容或过于生硬，并给出建议标题。
- 可直接用于下一轮修复的 prompt。
"""


def prompt_archive(project: Path, config: dict[str, Any], profile: dict[str, Any], provider: str, start: int, count: int) -> str:
    end = start + count - 1
    summaries = recent_summaries(project, max_files=10)
    return f"""# 第 {start:03d}-{end:03d} 章阶段归档

{provider_note(provider)}

请基于已有正文摘要和项目设定，更新长期记忆。不要写新正文。

## 平台风格

{platform_block(profile)}

## 最近摘要

{summaries}

## 输出/更新

1. `03_new_novel/memory_rollup.md`：压缩到可长期携带的主线记忆。
2. `03_new_novel/continuity_ledger.md`：人物状态、势力状态、地点、道具、能力、未解决冲突。
3. `03_new_novel/power_state_ledger.md`：本阶段结束后的修为/等级、当前小段或阶段、核心能力解锁状态、关键道具权限、伤势/反噬代价、越级依据和下一突破门槛。
4. `03_new_novel/foreshadowing_ledger.md`：新增伏笔、已回收伏笔、计划回收章节。
5. `03_new_novel/character_table.csv`：角色关系和状态。
6. 下一阶段第 {end + 1:03d} 章之后的风险提醒。

要求：只记录后续写作真正需要的信息，不要复述大段正文。
"""


def command_make_prompt(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    config = load_project_config(project)
    profiles = load_profiles()
    profile = profiles[config["platform"]]
    provider = args.provider
    batch_size = args.count or config.get("batch_size") or profile["batch_chapters"]

    if args.stage == "decompose":
        text = prompt_decompose(project, config, profile, provider)
        name = f"{timestamp()}_01_decompose_{provider}.md"
    elif args.stage == "blueprint":
        text = prompt_blueprint(project, config, profile, provider)
        name = f"{timestamp()}_02_blueprint_{provider}.md"
    elif args.stage == "chapters":
        if not args.start:
            raise SystemExit("chapters 阶段需要 --start。")
        text = prompt_chapters(project, config, profile, provider, args.start, batch_size)
        end = args.start + batch_size - 1
        name = f"{timestamp()}_03_chapters_{args.start:04d}_to_{end:04d}_{provider}.md"
    elif args.stage == "review":
        if not args.start:
            raise SystemExit("review 阶段需要 --start。")
        text = prompt_review(project, config, profile, provider, args.start, batch_size)
        end = args.start + batch_size - 1
        name = f"{timestamp()}_04_review_{args.start:04d}_to_{end:04d}_{provider}.md"
    elif args.stage == "archive":
        if not args.start:
            raise SystemExit("archive 阶段需要 --start。")
        text = prompt_archive(project, config, profile, provider, args.start, batch_size)
        end = args.start + batch_size - 1
        name = f"{timestamp()}_05_archive_{args.start:04d}_to_{end:04d}_{provider}.md"
    else:
        raise SystemExit(f"未知阶段：{args.stage}")

    path = project / "04_prompts" / name
    write_text(path, text)
    print(f"已生成提示词：{path}")


def command_accept(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    src = Path(args.file).expanduser().resolve()
    if not src.exists():
        raise SystemExit(f"文件不存在：{src}")
    text = read_text(src)

    if args.kind == "analysis":
        target = project / "02_source_analysis" / f"accepted_analysis_{timestamp()}.md"
    elif args.kind == "blueprint":
        target = project / "03_new_novel" / f"accepted_blueprint_{timestamp()}.md"
    elif args.kind == "draft":
        if not args.start or not args.end:
            raise SystemExit("draft 归档需要 --start 和 --end。")
        target = project / "05_drafts" / f"ch_{args.start:04d}_to_{args.end:04d}.md"
    elif args.kind == "summary":
        if not args.start or not args.end:
            raise SystemExit("summary 归档需要 --start 和 --end。")
        target = project / "06_summaries" / f"summary_{args.start:04d}_to_{args.end:04d}.md"
    elif args.kind == "review":
        target = project / "07_reviews" / f"review_{timestamp()}.md"
    else:
        raise SystemExit(f"未知 kind：{args.kind}")

    write_text(target, text)
    print(f"已归档：{target}")


SIM_KEEP_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")


def normalize_for_similarity(text: str) -> str:
    return "".join(SIM_KEEP_RE.findall(text)).lower()


def source_paths(project: Path) -> list[Path]:
    index_path = project / "01_sources" / "source_index.json"
    if not index_path.exists():
        raise SystemExit("缺少 source_index.json，请先运行 ingest。")
    index = load_json(index_path)
    paths = []
    for book in index.get("books", []):
        for item in book.get("files", []):
            path = Path(item["path"])
            if path.exists():
                paths.append(path)
    return paths


def command_audit(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    draft = Path(args.draft).expanduser().resolve()
    if not draft.exists():
        raise SystemExit(f"正文文件不存在：{draft}")

    draft_norm = normalize_for_similarity(read_text(draft))
    if len(draft_norm) < args.window:
        raise SystemExit("正文太短，无法审查。")

    windows = []
    seen = set()
    for idx in range(0, len(draft_norm) - args.window + 1, args.step):
        snippet = draft_norm[idx : idx + args.window]
        if snippet not in seen:
            windows.append(snippet)
            seen.add(snippet)
        if len(windows) >= args.max_windows:
            break

    matches: list[dict[str, str]] = []
    for path in source_paths(project):
        source_norm = normalize_for_similarity(read_text(path))
        for snippet in windows:
            if snippet in source_norm:
                matches.append({"source": str(path), "snippet": snippet})
                if len(matches) >= args.max_matches:
                    break
        if len(matches) >= args.max_matches:
            break

    risk = "低"
    if len(matches) >= 20:
        risk = "高"
    elif len(matches) >= 5:
        risk = "中"

    report = [
        "# Similarity Audit",
        "",
        f"- 正文：`{draft}`",
        f"- 窗口长度：{args.window}",
        f"- 命中数：{len(matches)}",
        f"- 风险等级：{risk}",
        "",
        "## 说明",
        "",
        "这是长串精确正文重合检查，不代表完整版权判断。授权复用模式下，不把剧情链、人物功能段、升级段或境界骨架一致判为错误；若风险为中/高，只重写未替换人物名/专名或整段逐字原样粘贴的正文句子，重写时不要改变原句意思和基本句式，只做词语级近义替换、称谓替换、专名替换和少量衔接替换，避免 AI 味解释腔。",
        "",
        "## 命中样本",
        "",
    ]
    if matches:
        for item in matches[: args.max_matches]:
            report.append(f"- 来源：`{item['source']}`")
            report.append(f"  - 片段：`{item['snippet']}`")
    else:
        report.append("- 未发现长串精确重合。")

    out = project / "07_reviews" / f"similarity_audit_{timestamp()}.md"
    write_text(out, "\n".join(report) + "\n")
    print(f"相似度审查完成：{out}")
    print(f"风险等级：{risk}，命中数：{len(matches)}")


def draft_sort_key(path: Path) -> tuple[int, int, str]:
    match = re.search(r"ch_(\d+)_to_(\d+)", path.name)
    if not match:
        return (10**9, 10**9, path.name)
    return (int(match.group(1)), int(match.group(2)), path.name)


def command_export(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    config = load_project_config(project)
    drafts = sorted((project / "05_drafts").glob("ch_*_to_*.md"), key=draft_sort_key)
    if not drafts:
        raise SystemExit("没有找到正文草稿。")

    title = args.title or config.get("title") or config.get("name") or "未命名小说"
    parts = [f"# {title}", "", f"> 导出时间：{dt.datetime.now().isoformat(timespec='seconds')}", ""]
    for path in drafts:
        parts.append(read_text(path).strip())
        parts.append("")

    out = project / "08_exports" / f"{safe_name(title)}_full.md"
    write_text(out, "\n\n".join(parts).strip() + "\n")
    print(f"已导出：{out}")


def command_status(args: argparse.Namespace) -> None:
    project = resolve_project(args.project)
    config = load_project_config(project)
    profiles = load_profiles()
    profile = profiles[config["platform"]]
    source_index = project / "01_sources" / "source_index.json"
    drafts = sorted((project / "05_drafts").glob("ch_*_to_*.md"), key=draft_sort_key)
    summaries = sorted((project / "06_summaries").glob("summary_*.md"))
    prompts = sorted((project / "04_prompts").glob("*.md"))

    print(f"项目：{config['name']}")
    print(f"目录：{project}")
    print(f"平台：{profile['display_name']}")
    print(f"源书索引：{'已导入' if source_index.exists() else '未导入'}")
    print(f"提示词数量：{len(prompts)}")
    print(f"正文批次数：{len(drafts)}")
    print(f"摘要数量：{len(summaries)}")
    if drafts:
        print(f"最新正文：{drafts[-1]}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel_agent", description="本地网文写作 agent 工作流")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="创建一个新小说项目")
    p.add_argument("--name", required=True, help="项目名")
    p.add_argument("--platform", default="feilu", choices=sorted(load_profiles().keys()), help="平台风格")
    p.add_argument("--source-dir", default="", help="源书目录")
    p.add_argument("--project-dir", default="", help="自定义项目目录")
    p.add_argument("--title", default="", help="小说暂定名")
    p.add_argument("--target-chapters", type=int, default=0, help="可选参考章数；自动流程默认按约 200-300 万字和大纲节奏决定终章")
    p.add_argument("--batch-size", type=int, default=0, help="每批章节数，不填则按平台默认")
    p.set_defaults(func=command_init)

    p = sub.add_parser("start", help="一条命令创建项目、导入源书并生成第一步提示词")
    p.add_argument("--name", required=True, help="项目名")
    p.add_argument("--platform", default="feilu", choices=sorted(load_profiles().keys()), help="平台风格")
    p.add_argument("--source-dir", required=True, help="源书目录")
    p.add_argument("--project-dir", default="", help="自定义项目目录")
    p.add_argument("--title", default="", help="小说暂定名")
    p.add_argument("--target-chapters", type=int, default=0, help="可选参考章数；自动流程默认按约 200-300 万字和大纲节奏决定终章")
    p.add_argument("--batch-size", type=int, default=0, help="每批章节数，不填则按平台默认")
    p.add_argument("--provider", default="manus", choices=["manus", "chatgpt", "claude", "generic"])
    p.set_defaults(func=command_start)

    p = sub.add_parser("autopilot", help="自动持续运行：拆解源书、生成融合设定、分批写正文并在节点暂停")
    p.add_argument("--project", default="", help="已有项目目录")
    p.add_argument("--name", default="", help="新项目名；不传 --project 时必填")
    p.add_argument("--platform", default="feilu", choices=sorted(load_profiles().keys()), help="平台风格")
    p.add_argument("--source-dir", default="", help="源书目录；不传 --project 时必填")
    p.add_argument("--project-dir", default="", help="新项目目录")
    p.add_argument("--title", default="", help="小说暂定名")
    p.add_argument("--target-chapters", type=int, default=0, help="可选参考章数；最终终章由全书大纲按体量和节奏决定")
    p.add_argument("--batch-size", type=int, default=0, help="每个确认批次写几章；默认 30 章")
    p.add_argument("--provider", default="openai", choices=["openai", "anthropic", "openai-compatible", "sub2api", "deepseek", "mock"])
    p.add_argument("--engine", default="langgraph", choices=["langgraph", "builtin"], help="默认使用 GitHub 工具 LangGraph；未安装时可用 builtin")
    p.add_argument("--model", default="", help="模型名；不填则用环境变量或默认值")
    p.add_argument("--api-key", default="", help="API key；建议用环境变量而不是命令行明文")
    p.add_argument("--api-keys-file", default="", help="批量 API key 文件；一行一个 key")
    p.add_argument("--base-url", default="", help="OpenAI-compatible 或代理服务地址")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--source-mode", default="deep", choices=["sampled", "deep"], help="默认 deep 逐块深读源书；sampled 仅使用抽样卡")
    p.add_argument("--fusion-mode", default="licensed", choices=["strong", "conservative", "licensed"], help="默认 licensed 完全融合授权复用；strong 为保守强融合；conservative 为低相似模式")
    p.add_argument("--source-workers", type=int, default=3, help="源书单本拆解并发数")
    p.add_argument("--source-timeout", type=int, default=0, help="源书单本模型调用超时秒数；0 表示 sampled=900 秒，deep=3600 秒")
    p.add_argument("--source-retries", type=int, default=1, help="源书单本模型调用失败重试次数")
    p.add_argument("--max-source-chunks-per-book", type=int, default=8)
    p.add_argument("--chunk-chars", type=int, default=12000)
    p.add_argument("--max-chapters", type=int, default=0, help="本轮最多自动推进到第几章；0 表示按全书大纲推进到终章")
    p.add_argument("--review-every", type=int, default=30)
    p.add_argument("--archive-every", type=int, default=60)
    p.add_argument("--no-checkpoints", action="store_true", help="不中途等待确认，谨慎使用")
    p.add_argument("--max-tokens-analysis", type=int, default=4096)
    p.add_argument("--max-tokens-blueprint", type=int, default=0, help="0 表示不传 max_tokens 上限")
    p.add_argument("--max-tokens-chapter", type=int, default=9000)
    p.add_argument("--max-tokens-maintenance", type=int, default=6000)
    p.add_argument("--temperature", type=float, default=0.75)
    p.add_argument("--stream-preview", action="store_true", help="使用 OpenAI-compatible 流式返回，并把模型输出实时写入日志")
    p.set_defaults(func=command_autopilot)

    p = sub.add_parser("revise", help="按当前节点的微调要求自动修改设定/正文文件")
    p.add_argument("--project", required=True)
    p.add_argument("--note", required=True, help="微调要求")
    p.add_argument("--provider", default="sub2api", choices=["openai", "anthropic", "openai-compatible", "sub2api", "deepseek", "mock"])
    p.add_argument("--model", default="")
    p.add_argument("--api-key", default="")
    p.add_argument("--api-keys-file", default="")
    p.add_argument("--base-url", default="")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--max-tokens-blueprint", type=int, default=0, help="0 表示不传 max_tokens 上限")
    p.add_argument("--fusion-mode", default="licensed", choices=["strong", "conservative", "licensed"])
    p.add_argument("--stream-preview", action="store_true", help="使用 OpenAI-compatible 流式返回，并把模型输出实时写入日志")
    p.set_defaults(func=command_revise)

    p = sub.add_parser("power-rewrite", help="只回修已写章节里的战力境界描写，尽量不改剧情")
    p.add_argument("--project", required=True)
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--end", type=int, default=150)
    p.add_argument("--provider", default="sub2api", choices=["openai", "anthropic", "openai-compatible", "sub2api", "deepseek", "mock"])
    p.add_argument("--model", default="")
    p.add_argument("--api-key", default="")
    p.add_argument("--api-keys-file", default="")
    p.add_argument("--base-url", default="")
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--max-tokens-chapter", type=int, default=9000)
    p.add_argument("--stream-preview", action="store_true", help="流式显示每章回修输出")
    p.set_defaults(func=command_power_rewrite)

    p = sub.add_parser("trial-write", help="用已生成的章纲分段试写少量正文，不要求完整章纲")
    p.add_argument("--project", required=True)
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--end", type=int, default=3)
    p.add_argument("--provider", default="sub2api", choices=["openai", "anthropic", "openai-compatible", "sub2api", "deepseek", "mock"])
    p.add_argument("--model", default="")
    p.add_argument("--api-key", default="")
    p.add_argument("--api-keys-file", default="")
    p.add_argument("--base-url", default="")
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--max-tokens-chapter", type=int, default=9000)
    p.add_argument("--temperature", type=float, default=0.75)
    p.add_argument("--stream-preview", action="store_true", help="流式显示每章试写输出")
    p.set_defaults(func=command_trial_write)

    p = sub.add_parser("promote-trial", help="把试写稿转成正式正文，并从下一章继续写")
    p.add_argument("--project", required=True)
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--end", type=int, default=4)
    p.set_defaults(func=command_promote_trial)

    p = sub.add_parser("continue-outline", help="只继续补逐章章纲到指定章节，不要求一次生成全书")
    p.add_argument("--project", required=True)
    p.add_argument("--until", type=int, required=True, help="章纲连续补到第几章")
    p.add_argument("--provider", default="sub2api", choices=["openai", "anthropic", "openai-compatible", "sub2api", "deepseek", "mock"])
    p.add_argument("--model", default="")
    p.add_argument("--api-key", default="")
    p.add_argument("--api-keys-file", default="")
    p.add_argument("--base-url", default="")
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--fusion-mode", default="licensed", choices=["strong", "conservative", "licensed"])
    p.add_argument("--max-tokens-blueprint", type=int, default=0)
    p.add_argument("--stream-preview", action="store_true", help="流式显示章纲补全输出")
    p.set_defaults(func=command_continue_outline)

    p = sub.add_parser("reset-rebuild", help="归档当前产物，重新读取源书并从头生成拆解与设定包")
    p.add_argument("--project", required=True)
    p.add_argument("--source-dir", default="", help="不填则使用项目配置里的源书目录")
    p.add_argument("--provider", default="sub2api", choices=["openai", "anthropic", "openai-compatible", "sub2api", "deepseek", "mock"])
    p.add_argument("--engine", default="langgraph", choices=["langgraph", "builtin"])
    p.add_argument("--model", default="")
    p.add_argument("--api-key", default="")
    p.add_argument("--api-keys-file", default="")
    p.add_argument("--base-url", default="")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--batch-size", type=int, default=0)
    p.add_argument("--source-mode", default="deep", choices=["sampled", "deep"])
    p.add_argument("--fusion-mode", default="licensed", choices=["strong", "conservative", "licensed"])
    p.add_argument("--source-workers", type=int, default=3)
    p.add_argument("--source-timeout", type=int, default=0)
    p.add_argument("--source-retries", type=int, default=1)
    p.add_argument("--max-source-chunks-per-book", type=int, default=8)
    p.add_argument("--chunk-chars", type=int, default=12000)
    p.add_argument("--max-chapters", type=int, default=0)
    p.add_argument("--review-every", type=int, default=30)
    p.add_argument("--archive-every", type=int, default=60)
    p.add_argument("--no-checkpoints", action="store_true")
    p.add_argument("--max-tokens-analysis", type=int, default=4096)
    p.add_argument("--max-tokens-blueprint", type=int, default=0)
    p.add_argument("--max-tokens-chapter", type=int, default=9000)
    p.add_argument("--max-tokens-maintenance", type=int, default=6000)
    p.add_argument("--temperature", type=float, default=0.75)
    p.add_argument("--stream-preview", action="store_true")
    p.set_defaults(func=command_reset_rebuild)

    p = sub.add_parser("reset-writing", help="只归档正文/摘要/审稿，从现有设定和章纲第1章重新写")
    p.add_argument("--project", required=True)
    p.set_defaults(func=command_reset_writing)

    p = sub.add_parser("approve", help="确认 autopilot 当前暂停节点，允许继续")
    p.add_argument("--project", required=True)
    p.add_argument("--note", default="", help="确认说明，可选")
    p.set_defaults(func=command_approve)

    p = sub.add_parser("select-outline", help="选择 3 套候选大纲中的一套，作为后续完整设定包底稿")
    p.add_argument("--project", required=True)
    p.add_argument("--candidate", type=int, required=True, choices=[1, 2, 3])
    p.set_defaults(func=command_select_outline)

    p = sub.add_parser("agent-status", help="查看 autopilot 状态")
    p.add_argument("--project", required=True)
    p.set_defaults(func=command_agent_status)

    p = sub.add_parser("test-llm", help="测试模型接口连通性")
    p.add_argument("--provider", default="sub2api", choices=["openai", "anthropic", "openai-compatible", "sub2api", "deepseek", "mock"])
    p.add_argument("--model", default="")
    p.add_argument("--api-key", default="")
    p.add_argument("--api-keys-file", default="")
    p.add_argument("--base-url", default="")
    p.add_argument("--timeout", type=int, default=60)
    p.set_defaults(func=command_test_llm)

    p = sub.add_parser("ingest", help="导入并索引一批源书")
    p.add_argument("--project", required=True)
    p.add_argument("--source-dir", default="", help="源书目录；不填则用项目配置")
    p.set_defaults(func=command_ingest)

    p = sub.add_parser("make-prompt", help="生成给 Manus / ChatGPT / Claude 的任务提示词")
    p.add_argument("--project", required=True)
    p.add_argument("--stage", required=True, choices=["decompose", "blueprint", "chapters", "review", "archive"])
    p.add_argument("--provider", default="manus", choices=["manus", "chatgpt", "claude", "generic"])
    p.add_argument("--start", type=int, default=0, help="起始章节")
    p.add_argument("--count", type=int, default=0, help="本批章节数")
    p.set_defaults(func=command_make_prompt)

    p = sub.add_parser("accept", help="把模型输出归档到项目")
    p.add_argument("--project", required=True)
    p.add_argument("--kind", required=True, choices=["analysis", "blueprint", "draft", "summary", "review"])
    p.add_argument("--file", required=True)
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--end", type=int, default=0)
    p.set_defaults(func=command_accept)

    p = sub.add_parser("audit", help="审查正文与源书的长串精确重合")
    p.add_argument("--project", required=True)
    p.add_argument("--draft", required=True)
    p.add_argument("--window", type=int, default=48)
    p.add_argument("--step", type=int, default=24)
    p.add_argument("--max-windows", type=int, default=5000)
    p.add_argument("--max-matches", type=int, default=50)
    p.set_defaults(func=command_audit)

    p = sub.add_parser("export", help="合并所有正文草稿为整本 Markdown")
    p.add_argument("--project", required=True)
    p.add_argument("--title", default="")
    p.set_defaults(func=command_export)

    p = sub.add_parser("status", help="查看项目状态")
    p.add_argument("--project", required=True)
    p.set_defaults(func=command_status)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
