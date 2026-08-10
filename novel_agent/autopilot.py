from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import io
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .llm_client import LLMClient


FILE_BLOCK_RE = re.compile(
    r"<file\s+path=[\"']([^\"']+)[\"']\s*>\s*(.*?)\s*</file>",
    re.IGNORECASE | re.DOTALL,
)
ALT_FILE_BLOCK_RE = re.compile(
    r"<<<FILE\s*:?\s*path=[\"']([^\"']+)[\"']\s*>>>\s*(.*?)\s*(?:<<<\s*END[\s_-]*FILE\s*>>>|</FILE\s*>>>)",
    re.IGNORECASE | re.DOTALL,
)
FENCED_FILE_BLOCK_RE = re.compile(
    r"```(?:file|csv|md|markdown)?(?:\s+path=[\"']([^\"']+)[\"'])?[^\n]*\n(.*?)\n```",
    re.IGNORECASE | re.DOTALL,
)
BRACKET_FILE_BLOCK_RE = re.compile(
    r"\[FILE_START\s+path=[\"']?([^\"'\]\r\n]+)[\"']?\s*\]\s*(.*?)\s*\[FILE_END\s*\]",
    re.IGNORECASE | re.DOTALL,
)


SYSTEM_PROMPT = """你是一个本地长篇网文融合写作 agent。
你的目标不是从零原创，而是基于用户授权源书做借鉴融合、统一重组和连续成书。
你可以吸收题材、人设功能位、金手指逻辑、世界规则、冲突模型、爽点结构、剧情模块和章节节奏。
允许在授权复用模式下完整沿用并跨书融合源书的爽点、故事情节、剧情链、事件顺序、场景媒介、人物功能、境界体系和设定骨架；人物名和专有人名不能照搬，必须改名；原文句子不要大幅改写，不要改变原句意思和基本句式，只做词语级近义替换、称谓替换、专名替换和少量衔接替换；必须保留原句语气、动作顺序、对白功能、信息量和爽点力度，不能整段逐字原样粘贴，也不能改成 AI 味解释腔。
需要写文件时，必须严格使用 FILE_BUNDLE 协议。
"""


FILE_PROTOCOL = """\n\n## FILE_BUNDLE 输出协议

本次请只输出文件块，不要输出解释、寒暄或 Markdown 代码围栏。

格式：

<file path="相对项目目录/文件名.md">
文件内容
</file>

可以输出多个 file 块。path 必须是相对项目目录的路径，不能使用绝对路径，不能包含 `..`。
"""

FUSION_SYSTEM_OVERRIDE = """

当前项目的默认方法是“强融合源书设计”，不是为了追求陌生感而重新发明一套空泛大纲。
优先从用户提供的多本源书中选择经过验证的力量体系结构、能力机制、人物关系功能、
阶段冲突、升级节点、高潮模块和全书走向，再做跨书拼合、统一设定和平台化调节。
对于每个关键设计，应能说明来自哪些源书素材、保留了什么、与什么进行了组合。
可以保留或紧密改造设计层面的优点；不要机械地把好用结构全部换掉。
除非用户明确确认拥有相应改编或复用权利，不复制长段原文、标志性台词，
也不逐章复刻单本作品的一整条独特场景链。
"""
SYSTEM_PROMPT = SYSTEM_PROMPT + FUSION_SYSTEM_OVERRIDE

SOURCE_FUSION_DEFAULT_OVERRIDE = """

## 最高优先级：完全融合授权复用

当前两套本地 agent 的默认写法已经切换为“完全融合授权复用”。后续所有新书都不是从零原创，
而是先深读用户导入的源书，抽取并沿用可用的爽点、故事情节、剧情链、事件顺序、场景媒介、
关键转折、人物功能设定、人物关系、境界体系、设定骨架和全书升级走向，再跨书融合、统一命名、
统一背景、统一势力名、统一金手指名和承接逻辑。

如果用户选择任意平台（飞卢、番茄、起点或其他平台）并导入该平台排行榜/同类榜单源书，这批源书就是目标平台的
核心素材库。必须优先读取并融合这些源书对应该平台的爽点、整段剧情链、升级段落、人物功能段、节奏密度、
章末钩子和卖点结构；在逻辑合理的前提下进行跨书拼接，不要另起一套原创情节。

不要为了“原创感”凭空创造与源书素材无关的桥段。授权复用模式下，完整剧情链、人物功能、
境界体系和设定骨架是复用素材，不是禁用项。人物名、专有人名不能照搬，必须替换成新项目姓名；
势力名、地名、设定名、金手指名原则上也要按本书统一改写。源文句子不要大幅改写，不要改变原句意思和基本句式，
只做词语级近义替换、称谓替换、专名替换和少量衔接替换；必须保留原句语气、动作顺序、对白功能、信息量和爽点力度。
不得整段逐字完全照抄；不要改成 AI 味解释腔、
空泛形容腔或过度润色腔。
"""
SYSTEM_PROMPT = SYSTEM_PROMPT + SOURCE_FUSION_DEFAULT_OVERRIDE

SOURCE_BACKBONE_CLONE_RULE = """

## 最高优先级：源书主干复刻改名版

当前模式不是从零原创、不是魔改、不是换壳、不是只借一个设定重新写。每一本导入源书都必须先锁定并保留：
题材类型、时代背景、社会/职业生态、主角身份、核心金手指功能、主线目标、反派压迫链、地图/场景媒介、
关键剧情链、事件顺序、章节节奏、章末钩子和平台爽感。

绝对禁止：
- 把源书题材换壳，例如现代都市不得改成古代/玄幻/仙侠，玄幻不得改成都市商战，历史不得改成科幻。
- 改时代、改背景、改职业生态、改核心社会关系、改核心金手指逻辑。
- 削弱源书爽点密度、升级速度、打脸频率、金手指强度或收益兑现；允许在源书基础上更快、更大、更爽，但不允许降配。
- 把源书主线和核心剧情链换成另一套新故事。
- 为了“原创感”发明新世界观、新地图体系、新主线、新反派链，导致源书读感消失。
- 源书没有玄幻境界、宗门地图、上界结构时，不得硬造境界表、宗门、秘境、王朝、古族、域外等玄幻元素；现代都市源书的升级只能按源书已有的系统等级、账号等级、财富、权限、职业段位、技术/资源成长等方式保留。

允许的改动只有：
- 人物名、专有人名、地名、势力名、公司/门派/组织名等做替换，但场景功能和关系结构不变。
- 语句只做词语级近义替换、称谓替换、专名替换和少量衔接替换；不改变原意、动作顺序、对白功能、信息量和爽点力度。
- 多源融合时，以主源书为骨架，其他源书只嵌入同功能位的爽点、桥段、反派压迫方式或升级/经营模块，不得推翻主源书背景和主线。
- 单本源书时，候选大纲只能是同一源书主干的不同卖点强调/改名版本，不得生成三套全新故事。

所有大纲、设定包、章节计划和正文必须写出或遵守“主源书锚点”：题材、时代、背景、主角身份、金手指、主线事件顺序、不可改项。
"""
SYSTEM_PROMPT = SYSTEM_PROMPT + SOURCE_BACKBONE_CLONE_RULE

FEILU_FAST_PAYOFF_RULE = """

## 快爽平台硬规则：爽、快、少压抑

若目标平台或源书读感是飞卢、快爽升级文、无脑爽文或同类榜单文，必须保留源书的快节奏爽感：
- 主角可以遇到压力，但压力只服务于快速反杀、立刻打脸、升级兑现和奖励回收，不得连续多章苦大仇深、被动挨打、沉重宿命化。
- 开篇和前期必须快进冲突、快给金手指反馈、快出第一次强反杀；不要写成慢热正剧、苦难成长、压抑复仇或公式化命运叙事。
- 每章都要有明确小爽点或信息钩子，每 3-5 章有中爽点；反派压迫要直接，主角反制要狠，收益要看得见。
- 升级一定要快，打脸一定要快且爽；源书如果是高频升级/高频反杀节奏，新书必须跟随源书节奏，不能擅自拉长铺垫或改成慢燃。
- 爽点密度、升级速度、打脸频率、金手指反馈和收益兑现不得低于源书；可以在合理处放大、提前、加倍兑现，但不能削弱或稀释。
- 大纲生成时不得随意原创过多新主线；应以源书原有事件顺序、爽点密度、升级频率和打脸节奏为骨架，只做改名、轻微承接和合理融合。
- 不要把源书简单爽点“高级化”成抽象设定解释、宏大空话、心理散文或 AI 总结腔。
"""
SYSTEM_PROMPT = SYSTEM_PROMPT + FEILU_FAST_PAYOFF_RULE

SOURCE_BACKBONE_STRICT_BLOCK = SOURCE_BACKBONE_CLONE_RULE + "\n\n" + FEILU_FAST_PAYOFF_RULE

CHAPTER_DRAFT_SYSTEM_PROMPT = """你是长篇网文正文写手。
你只负责把用户给出的本书设定、章纲、前文尾段写成一章可读正文。
大纲只约束剧情、人物、战力、道具、伏笔和事件顺序；不要把大纲字段、设定条目、规则说明复述进正文。
正文要像小说现场：人物在场景里说话、行动、碰撞、拿结果。少解释，少总结，少心理散文。
只输出正文，不输出分析、说明、创作过程或修改报告。"""


DETAILED_CHAPTER_PLAN_RULE = """

## 章纲硬规则：必须可直接写正文
章节规划不是主题表、不是功能位表、不是“方向词”列表，而是后续正文的施工图。`chapter_plan.csv` 每一章都必须写成可执行的具体剧情事件。

每章至少包含：
- 触发事件：谁在什么地点拿什么事、证据、资源、命令、考核、敌人或事故压到主角面前。
- 阻碍/对手：具体人物、势力、规则、道具、误会、封锁、追杀、考核或舆论如何制造麻烦。
- 主角反制：主角本章具体做了什么动作、使用什么能力/资源/话术/证据，怎么拆局或反杀。
- 爽点兑现：本章拿到什么结果、奖励、证据、地位、资源、战力反馈、敌人代价或关系变化。
- 章末承接：下一章具体接哪件新动作、新敌人、新证据、新地图入口或新危机。

禁止写成：推进主线、继续调查、危机升级、关系变化、进入新阶段、铺垫后续、引出更大危机、主角成长、反派登场、爽点升级这类空泛句。若出现这类表达，必须补成具体事件链。

章纲重写请求不是让你写正文。只能重写 `chapter_plan.csv`、`chapter_plan_parts` 以及必要的 `continuity_ledger.md`、`foreshadowing_ledger.md`、`memory_rollup.md`，不得输出正文稿。
"""

ANTI_AI_OUTLINE_RULE = """

## 去 AI 味规则：作用于章纲和后续写作规则，不是直接写正文
用户要求“去 AI 味”时，含义是：让设定、章纲和后续正文提示更像成熟网文流程，而不是让当前步骤直接产出正文。

章纲与后续写作规则必须避免：
- 抽象总结腔：命运齿轮、格局打开、暗流涌动、情绪递进、压迫感升级但不写事件。
- AI 压缩词/自造词：读者需要停下来猜意思的两字/三字概念、硬造术语、硬凑四字标题。
- 空泛爽点词：打脸升级、反派压迫、主角成长、危机爆发但没有具体人物、动作和结果。
- 设定解释腔：角色用旁白口吻解释世界观、系统规则、境界逻辑，而不是在事件里自然体现。

必须改成：具体人物说具体话，具体事件推动具体动作，具体动作兑现具体收益，章末留下具体下一步。
"""

SOURCE_DERIVED_OUTLINE_RULE = """

## 源书证据驱动的大纲原则
大纲、卷纲和章纲阶段不要预设固定文风、固定标题格式或固定平台模板。目标平台只作为市场分类；真正的标题口味、章节颗粒度、开场方式、兑现节奏和章末承接，要从用户导入的源书拆解与源书风格画像中归纳。

生成大纲产物时：
- `title` 只是内部工作标题，用来定位本章事件，不是正文最终标题；不要为了满足固定字数、固定句式或固定“爆款标题模板”而造词。
- 章纲只写故事事实、人物动作、资源/能力边界、伏笔和结果，不写正文口吻，不写作者评价，不把“爽、快、打脸、压迫”等词当作产物本身。
- 如果源书画像没有明确证据，不要补一条看似专业的固定规则；保持为可执行的剧情事实卡。
- 可以借鉴 InkOS 式流程：先确定当前卷/章节节点、角色目标、阻碍、反制、结果和下一节点，再生成章级事实卡；不要把流程术语写进产物。
"""

SYSTEM_PROMPT = SYSTEM_PROMPT + DETAILED_CHAPTER_PLAN_RULE + "\n\n" + ANTI_AI_OUTLINE_RULE + "\n\n" + SOURCE_DERIVED_OUTLINE_RULE


CHAPTER_PLAN_HEADER = (
    "chapter_no,volume,title,core_conflict,small_hook,power_usage,character_change,"
    "foreshadowing,ending_hook,mainline_progress,source_inspiration,status"
)
CHAPTER_PLAN_CHUNK_SIZE = 10
CHAPTER_HARD_CHAR_LIMIT = 5000
CHAPTER_TARGET_CHAR_LIMIT = 4500
LANGUAGE_POLISH_TARGET_LIMIT = 4700


BLUEPRINT_OUTPUT_FILES = [
    "03_new_novel/novel_bible.md",
    "03_new_novel/style_guide.md",
    "03_new_novel/power_system.md",
    "03_new_novel/power_state_ledger.md",
    "03_new_novel/worldbuilding.md",
    "03_new_novel/character_table.csv",
    "03_new_novel/volume_outline.md",
    "03_new_novel/full_story_outline.md",
    "03_new_novel/fusion_traceability.md",
    "03_new_novel/chapter_plan.csv",
    "03_new_novel/continuity_ledger.md",
    "03_new_novel/foreshadowing_ledger.md",
    "03_new_novel/memory_rollup.md",
]

SOURCE_AGGREGATE_OUTPUT_FILES = [
    "02_source_analysis/source_bibles.md",
    "02_source_analysis/source_style_profile.md",
    "02_source_analysis/motif_library.csv",
    "02_source_analysis/character_pool.csv",
    "02_source_analysis/plot_pool.csv",
    "02_source_analysis/power_system_pool.csv",
    "02_source_analysis/fusion_opportunities.md",
    "02_source_analysis/source_risk_notes.md",
]

OUTLINE_CANDIDATE_FILES = [
    "03_new_novel/outline_candidates/outline_1.md",
    "03_new_novel/outline_candidates/outline_2.md",
    "03_new_novel/outline_candidates/outline_3.md",
]

OUTLINE_VARIANT_SPECS = [
    (
        1,
        "主干复刻快爽版",
        "以主源书原有题材、时代、背景、职业生态和事件顺序为骨架，爽点密度、升级速度、打脸频率、金手指反馈不得低于源书，只允许更快更爽，不另起新主线。",
    ),
    (
        2,
        "金手指等强放大版",
        "不改变主源书核心剧情链，不削弱金手指功能、规则和反馈频率；在源书原有强度上做更清楚、更直接、更大额的收益兑现。",
    ),
    (
        3,
        "支线融合增强版",
        "仍以主源书主干为准，其他源书只补同功能位的爽点、反派压迫、打脸桥段和支线模块；所有补强不得降低原书爽点密度、升级速度和金手指强度，不得改时代、改题材、改地图媒介或改核心职业生态。",
    ),
]

SOURCE_AGGREGATE_FILE_SPECS = [
    (
        "02_source_analysis/source_bibles.md",
        "源书素材总览",
        "Markdown。逐本保留可直接参与融合的具体设计：境界/能力规则、境界上限、世界/地图层级、人物关系、剧情节点、卷级走向、高潮设计和爽点结构；标注来源、可完整沿用部分、适合与哪本书拼合；最后给出全局融合结论。",
    ),
    (
        "02_source_analysis/source_style_profile.md",
        "源书风格画像",
        "Markdown。只根据源书拆解证据归纳标题口味、章节开场方式、场景切入速度、对白/旁白比例、单章事件颗粒度、章末承接方式、连续 3-10 章的小闭环和常见情绪温度；每条观察都标明来自哪本源书或哪类标题/片段。不要写成固定模板，不要发明平台规则，不要要求后续照抄具体句子。",
    ),
    (
        "02_source_analysis/motif_library.csv",
        "爽点/母题素材库",
        "CSV，表头为 motif_id,name,source_books,weight,stage,usage,risk_note。列出可反复使用的爽点、钩子、压迫链、反转模型。",
    ),
    (
        "02_source_analysis/character_pool.csv",
        "人物功能位素材池",
        "CSV，表头为 role_id,function_role,source_books,source_element,traits,relationship_use,growth_arc,keep_or_modify,fusion_target,avoid_copying。保留具体人物设计、人物功能、人物关系和角色弧线用于跨书融合；授权复用模式下可沿用人物设定功能，只统一姓名、背景和势力归属。",
    ),
    (
        "02_source_analysis/plot_pool.csv",
        "剧情模块素材池",
        "CSV，表头为 plot_id,module_name,source_books,source_element,conflict_model,setup,payoff,keep_or_modify,fusion_target,risk_note。记录值得完整沿用和融合的具体剧情构造、事件顺序、场景媒介、地图升级链、阶段 Boss、换地图触发器、最终 Boss 伏笔和全书走向节点，并说明如何与其他源书拼合。",
    ),
    (
        "02_source_analysis/power_system_pool.csv",
        "能力/金手指/力量体系素材池",
        "CSV，表头为 system_id,source_books,source_element,core_logic,realm_or_upgrade_path,limit,cost,payoff,keep_or_modify,fusion_target。优先保留好用的境界架构、能力机制、最高境界、突破资源、对应地图和可融合位置，再统一为新书体系。",
    ),
    (
        "02_source_analysis/fusion_opportunities.md",
        "融合机会清单",
        "Markdown。提出不少于三种跨书组合方案，明确主线、单线境界表、境界天花板、金手指、人物关系、势力链、宏大地图阶梯、阶段 Boss、换地图触发器、最终 Boss 埋线和终局分别复用、照搬、拼合或优化自哪些源书元素。",
    ),
    (
        "02_source_analysis/source_risk_notes.md",
        "源素材复用与融合清单",
        "Markdown。默认按完全融合授权复用整理源书素材：不要把完整剧情链列为禁用项；整理可沿用的具体故事链、事件顺序、场景媒介、人物功能、人物关系、境界体系和设定骨架；人物名和专有人名必须替换，背景、势力名、地名、设定名和承接细节按本书统一；源句只做词语级近义替换、称谓替换、专名替换和少量衔接替换，不改变原意和基本句式。",
    ),
]

SOURCE_AGGREGATE_CSV_PARTS = {
    "02_source_analysis/motif_library.csv": [
        "开篇钩子、身份反差、打脸与即时反馈类爽点",
        "成长、升级、资源、机缘、越阶与阶段奖励类爽点",
        "势力冲突、地图升级、情感张力、终局回收与长期连载母题",
    ],
    "02_source_analysis/character_pool.csv": [
        "主角底色、金手指承载者、同行天才、成长对照位",
        "亲缘、师承、盟友、复杂女性角色、关系张力与情感支点",
        "前中期敌人、压迫者、势力代理人、敌转友或竞争者",
        "高阶反派、幕后黑手、终局对手、世界级灾难与历史真相角色",
    ],
    "02_source_analysis/plot_pool.csv": [
        "开局到立稳脚跟阶段：起势、首个危机、初次翻盘与身份确立",
        "中前期阶段：资源争夺、宗门/家族/区域对抗与地图展开",
        "中后期阶段：大势力冲突、真相揭露、跨域大战与主线升级",
        "后期到结局阶段：终极矛盾、终局战争、伏笔回收与大结局",
    ],
    "02_source_analysis/power_system_pool.csv": [
        "开局能力、基础修炼、低中阶境界、获得资源与升级反馈",
        "中高阶境界、越境规则、职业/武学/特殊能力组合与限制代价",
        "顶层境界、世界规则、终局能力、最终战兑现与体系闭环",
    ],
}


class OutputQualityError(RuntimeError):
    def __init__(self, rel_path: str, reason: str, raw_path: Path | None = None):
        self.rel_path = rel_path
        self.reason = reason
        self.raw_path = raw_path
        raw_note = f"，原始结果已保存：{raw_path.name}" if raw_path else ""
        super().__init__(f"{rel_path} 生成结果异常：{reason}{raw_note}")

BLUEPRINT_FILE_SPECS = [
    (
        "03_new_novel/novel_bible.md",
        "新书总设定圣经",
        "Markdown。包含书名、15-30 字简介、100-180 字简介、核心卖点、主线目标、主角、反派压迫链、金手指、全书爽点承诺。",
    ),
    (
        "03_new_novel/style_guide.md",
        "源书派生风格记录",
        "Markdown。根据 source_style_profile.md 和源书素材证据整理本书的读感来源：标题口味、开场切入、对白/旁白比例、章末承接和节奏密度都必须说明来自哪些源书观察。不要写成固定文风命令，不要预设标题模板；正文最终文风以后续样书/源书参考为准。",
    ),
    (
        "03_new_novel/power_system.md",
        "力量体系/升级规则",
        "Markdown。必须覆盖完整境界划分、每境能力变化、晋升门槛、战力差距、越境规则、突破仪式感和杀敌反馈。",
    ),
    (
        "03_new_novel/power_state_ledger.md",
        "战力台账",
        "Markdown。持续记录主角当前修为/境界阶段、当前小段或等级、核心能力解锁状态、关键道具权限、越级依据、伤势/反噬代价和下一突破门槛；每 10 章归档时必须更新。",
    ),
    (
        "03_new_novel/worldbuilding.md",
        "世界观和势力体系",
        "Markdown。包含地图层级、势力层级、资源体系、规则冲突、主角活动范围扩张和终局舞台。",
    ),
    (
        "03_new_novel/character_table.csv",
        "人物表",
        "CSV，表头为 character_id,name,function_role,first_appearance,goal,secret,relationship_to_mc,growth_or_fall,power_level,status。",
    ),
    (
        "03_new_novel/volume_outline.md",
        "卷纲",
        "Markdown。8-12 卷，每卷写章节范围和事件因果链：本卷从哪件事起、谁压主角、主角如何拆局、资源/能力/关系如何变化、卷内高潮落到什么结果、下一卷接哪件具体事。不要写成固定文风规则或抽象阶段报告。",
    ),
    (
        "03_new_novel/full_story_outline.md",
        "全书大纲到大结局",
        "Markdown。必须覆盖第 001 章到目标总章数最后一章，按卷和 10-30 章段写清事件因果、反派更替、能力/资源变化、阶段结果、终局和大结局收束；不要只写“推进、升级、铺垫、爆发”这类报告词。",
    ),
    (
        "03_new_novel/fusion_traceability.md",
        "融合来源映射",
        "Markdown。逐项列出新书的主角起点、金手指、完整力量体系、核心人物关系、每卷主线/高潮/结局，分别借鉴了哪些源书素材，标明保留、跨书拼合或改造之处，以及未采用的原因。",
    ),
    (
        "03_new_novel/chapter_plan.csv",
        "完整逐章章节细纲",
        "CSV，必须从第 001 章一直规划到目标总章数最后一章。表头必须为 chapter_no,volume,title,core_conflict,small_hook,power_usage,character_change,foreshadowing,ending_hook,mainline_progress,source_inspiration,status。每一章必须是可直接写正文的具体情节事件链，写清触发事件、地点/人物/物证/道具、阻碍、主角反制、本章结果、收益兑现和章末承接；title 只是内部工作标题，不是正文最终标题；不得只写方向词、主题词、功能位或 AI 总结腔。",
    ),
    (
        "03_new_novel/continuity_ledger.md",
        "连续性账本",
        "Markdown。记录世界规则、人物关系、不能改写的硬设定、重要道具、势力状态和后续正文必须继承的信息。",
    ),
    (
        "03_new_novel/foreshadowing_ledger.md",
        "伏笔账本",
        "Markdown。列出伏笔编号、埋设章节、表层含义、真实用途、回收卷/章节和风险提醒。",
    ),
    (
        "03_new_novel/memory_rollup.md",
        "长期记忆摘要",
        "Markdown。给后续正文调用的高密度记忆，包含主线、人物、力量体系、当前世界状态、近期写作原则。",
    ),
]


@dataclass
class AutoOptions:
    source_mode: str = "deep"
    fusion_mode: str = "licensed"
    source_workers: int = 1
    source_timeout: int = 3600
    source_retries: int = 3
    max_source_chunks_per_book: int = 8
    chunk_chars: int = 12000
    batch_size: int = 30
    max_chapters: int = 0
    review_every: int = 30
    archive_every: int = 60
    stop_at_checkpoints: bool = True
    max_tokens_analysis: int = 4096
    max_tokens_blueprint: int = 0
    max_tokens_chapter: int = 9000
    max_tokens_maintenance: int = 6000
    temperature: float = 0.75
    stream_preview: bool = False


class StreamPreview:
    def __init__(self, label: str):
        self.label = label
        self.buffer = ""

    def __call__(self, chunk: str) -> None:
        text = chunk.replace("\r", "")
        if not text:
            return
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self._emit(line)
        if len(self.buffer) >= 500:
            self._emit(self.buffer)
            self.buffer = ""

    def flush(self) -> None:
        if self.buffer:
            self._emit(self.buffer)
            self.buffer = ""

    def _emit(self, text: str) -> None:
        if not text.strip():
            return
        try:
            print(f"[模型流:{self.label}] {text}", flush=True)
        except (BrokenPipeError, OSError):
            pass


class AutoPilot:
    def __init__(self, project: Path, llm: LLMClient, options: AutoOptions):
        from .cli import load_profiles, load_project_config

        self.project = project.resolve()
        self.llm = llm
        self.options = options
        self.config = load_project_config(self.project)
        self.profile = load_profiles()[self.config["platform"]]
        self.state_path = self.project / "00_config" / "agent_state.json"
        self._state_lock = threading.RLock()
        self._source_style_reference_cache: dict[tuple[int, int], str] = {}
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        from .cli import load_json, write_json

        if self.state_path.exists():
            try:
                if self.state_path.stat().st_size > 0:
                    state = load_json(self.state_path)
                    if isinstance(state, dict):
                        return state
            except Exception:
                corrupt = self.state_path.with_name(f"{self.state_path.name}.corrupt_{dt.datetime.now():%Y%m%d_%H%M%S}")
                try:
                    self.state_path.replace(corrupt)
                except OSError:
                    pass
        state = {
            "phase": "new",
            "current_chapter": 1,
            "activity": "等待启动",
            "awaiting": None,
            "approvals": [],
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        write_json(self.state_path, state)
        return state

    def _save_state(self) -> None:
        from .cli import write_json

        with self._state_lock:
            self.state["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
            write_json(self.state_path, self.state)

    def _log(self, message: str) -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        text = str(message)
        try:
            print(f"[{stamp}] {text}", flush=True)
        except (BrokenPipeError, OSError, UnicodeEncodeError):
            pass
        with self._state_lock:
            self.state["activity"] = text
            self._save_state()

    def _complete_with_preview(
        self,
        prompt: str,
        *,
        stream_label: str,
        llm: LLMClient | None = None,
        system: str = SYSTEM_PROMPT,
        max_tokens: int | None = 4096,
        temperature: float = 0.7,
    ) -> str:
        preview = StreamPreview(stream_label) if self.options.stream_preview else None
        try:
            try:
                return (llm or self.llm).complete(
                    prompt,
                    system=system,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stream_callback=preview,
                )
            except Exception as exc:
                if preview and self._should_retry_without_stream(exc):
                    self._log(f"{stream_label} 流式连接中断，自动切换为非流式请求重试一次")
                    preview.flush()
                    return (llm or self.llm).complete(
                        prompt,
                        system=system,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        stream_callback=None,
                    )
                raise
        finally:
            if preview:
                preview.flush()

    def _should_retry_without_stream(self, exc: Exception) -> bool:
        detail = str(exc).lower()
        return any(
            marker in detail
            for marker in (
                "http stream request failed",
                "ssleoferror",
                "unexpected_eof",
                "eof occurred",
                "connectionpool",
                "max retries exceeded",
                "connection aborted",
                "connection reset",
                "remote disconnected",
                "protocol",
            )
        )

    def _source_llm(self, start_index: int = 0) -> LLMClient:
        return self.llm.fork(
            timeout=self.options.source_timeout,
            retries=self.options.source_retries,
            start_index=start_index,
        )

    def run(self) -> str:
        self._log(
            f"启动 autopilot：phase={self.state.get('phase')}, "
            f"source_mode={self.options.source_mode}, max_chapters={self.options.max_chapters}"
        )
        awaiting = self.state.get("awaiting")
        if awaiting and self.options.stop_at_checkpoints and awaiting.get("type") != "source_failures":
            self._log(f"当前停在确认节点：{awaiting.get('type') or 'unknown'}")
            return self._awaiting_message(awaiting)

        phase = self.state.get("phase", "new")
        if phase == "completed" and self.resume_completed_if_target_extended():
            phase = self.state.get("phase", "writing")
        if phase == "new":
            self._log("阶段 1/3：开始源书拆解")
            self.decompose_sources()
            if self.options.stop_at_checkpoints and self.state.get("awaiting"):
                return self._awaiting_message(self.state["awaiting"])
            phase = self.state.get("phase")

        if phase == "decomposed":
            self._log("阶段 2/3：开始生成候选大纲或融合设定包")
            self.build_blueprint()
            if self.options.stop_at_checkpoints and self.state.get("awaiting"):
                return self._awaiting_message(self.state["awaiting"])
            phase = self.state.get("phase")

        if phase in {"outline_pending", "blueprint_pending", "writing"}:
            if phase == "outline_pending":
                self.state["awaiting"] = self.state.get("awaiting") or {
                    "type": "outline_selection",
                    "message": "三套候选大纲已生成，请先选择其中一套，再继续生成完整设定包。",
                    "target": "03_new_novel/outline_candidates",
                }
                self._save_state()
                self._log("候选大纲已生成，等待你选择方案")
                return self._awaiting_message(self.state["awaiting"])
            if phase == "blueprint_pending":
                self.state["awaiting"] = self.state.get("awaiting") or {
                    "type": "blueprint",
                    "message": "融合设定包已生成，请检查 03_new_novel 后 approve。",
                }
                self._save_state()
                self._log("融合设定包已生成，等待你检查后确认继续")
                return self._awaiting_message(self.state["awaiting"])
            self._log("阶段 3/3：开始分批写正文")
            self.write_until_limit()
            if self.options.stop_at_checkpoints and self.state.get("awaiting"):
                return self._awaiting_message(self.state["awaiting"])

        if self.state.get("phase") == "completed":
            self._log("本轮目标章节已完成")
            return "自动工作流已到达本次目标章节。"
        return f"自动工作流暂停，当前阶段：{self.state.get('phase')}"

    def resume_completed_if_target_extended(self) -> bool:
        current = int(self.state.get("current_chapter") or 1)
        max_chapter = int(self.options.max_chapters or 0) or self._target_chapter_count()
        if max_chapter <= 0 or current > max_chapter:
            return False
        self.state["phase"] = "writing"
        self.state["awaiting"] = None
        self._log(f"Target extended; resume writing from chapter {current:04d} to {max_chapter:04d}")
        self._save_state()
        return True

    def approve(self, note: str = "") -> str:
        awaiting = self.state.get("awaiting")
        if not awaiting:
            return "当前没有等待确认的节点。"
        self._log(f"确认节点通过：{awaiting.get('type') or 'unknown'}")
        if awaiting.get("type") == "outline_selection":
            self._save_state()
            return "当前是候选大纲选择节点，请先选择 1/2/3 其中一套，不能直接确认跳过。"
        self.state.setdefault("approvals", []).append(
            {
                "type": awaiting.get("type"),
                "note": note,
                "approved_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
        )
        if awaiting.get("type") == "blueprint":
            self.state["phase"] = "writing"
        self.state["awaiting"] = None
        self._save_state()
        return "已确认当前节点。再次运行 autopilot 会继续往下写。"

    def _awaiting_message(self, awaiting: dict[str, Any]) -> str:
        detail = awaiting.get("message") or "等待确认。"
        target = awaiting.get("target")
        if target:
            detail += f"\n需要查看/可修改：{self.project / target}"
        if awaiting.get("type") == "outline_selection":
            detail += f'\n选择方案后运行：.\\run.bat select-outline --project "{self.project}" --candidate 1'
        else:
            detail += f'\n确认后运行：.\\run.bat approve --project "{self.project}"'
        return detail

    def revise_current_checkpoint(self, note: str) -> str:
        note = note.strip()
        if not note:
            return "请先填写微调要求。"
        if self._is_chapter_plan_rewrite_request(note):
            return self.revise_chapter_plan(note)
        awaiting = self.state.get("awaiting")
        if not awaiting:
            phase = self.state.get("phase")
            current = int(self.state.get("current_chapter") or 1)
            draft_started = any((self.project / "05_drafts").glob("ch_*_to_*.md"))
            if phase == "writing" and current <= 1 and not draft_started:
                self._log("正文尚未开始，允许回到融合设定包节点应用微调")
                return self.revise_blueprint(note)
            return "当前没有等待确认的节点，不能应用节点微调。"
        checkpoint_type = awaiting.get("type") or "unknown"
        if checkpoint_type == "blueprint":
            return self.revise_blueprint(note)
        if checkpoint_type in {"batch", "review"}:
            return self.revise_draft_batch(note, awaiting)
        return f"当前节点 {checkpoint_type} 暂时只支持人工修改文件；蓝图节点支持自动微调。"

    def _is_chapter_plan_rewrite_request(self, note: str) -> bool:
        normalized = note.lower()
        plan_markers = ("章纲", "章节计划", "章节规划", "chapter_plan", "细纲", "剧情节点")
        detail_markers = ("重写", "重新写", "改写", "详细", "具体", "具体情节", "具体事件", "可执行", "无法依照章纲", "去ai", "去 ai", "ai味", "ai 味")
        return any(marker in normalized for marker in plan_markers) and any(marker in normalized for marker in detail_markers)

    def _append_revision_directive(self, note: str) -> Path:
        from .cli import read_optional, write_text

        path = self.project / "03_new_novel" / "revision_directive.md"
        old = read_optional(path, 20000)
        addition = f"""

## 章纲重写硬规则（{dt.datetime.now().isoformat(timespec='seconds')}）
用户要求：{note}

本次只让 agent 重写详细章纲和相关记忆文件，不直接写正文。

{DETAILED_CHAPTER_PLAN_RULE}

{ANTI_AI_OUTLINE_RULE}
"""
        write_text(path, (old.rstrip() + "\n" + addition.strip() + "\n").lstrip())
        return path

    def _archive_project_path(self, rel_path: str, backup_dir: Path) -> None:
        source = self.project / rel_path
        if not source.exists():
            return
        target = backup_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))

    def revise_chapter_plan(self, note: str) -> str:
        from .cli import read_optional, read_text, write_text

        if any((self.project / "05_drafts").glob("ch_*_to_*.md")):
            return "正文已经开始生成。重写全文章纲会影响已写正文，请先明确要求“仍然重写章纲并接受正文不一致风险”。"

        required = [
            "03_new_novel/novel_bible.md",
            "03_new_novel/volume_outline.md",
            "03_new_novel/full_story_outline.md",
            "03_new_novel/fusion_traceability.md",
        ]
        missing = [rel for rel in required if not read_optional(self.project / rel, 200).strip()]
        if missing:
            return "当前还缺少完整大纲/设定包，不能重写章纲。请先跑完候选大纲和融合设定包：" + "、".join(missing)

        target_chapters = self._target_chapter_count()
        parts_dir = self.project / "03_new_novel" / "chapter_plan_parts"
        covered: set[int] = set()
        if parts_dir.exists():
            for part in parts_dir.glob("ch_*.csv"):
                if "_raw_" in part.name:
                    continue
                covered.update(self._chapter_numbers_from_csv(read_text(part)))
        covered_prefix = self._covered_prefix_end({number for number in covered if 1 <= number <= target_chapters})
        resume_existing_parts = bool(covered) and not self._single_file_ready("03_new_novel/chapter_plan.csv")

        backup_dir: Path | None = None
        if resume_existing_parts:
            self._log(f"检测到未完成详细章纲，直接续跑：已覆盖 {len(covered)}/{target_chapters} 章，连续到第 {covered_prefix:03d} 章")
        else:
            backup_dir = self.project / "_backups" / f"chapter_plan_rewrite_{self._stamp()}"
            backup_dir.mkdir(parents=True, exist_ok=True)
            for rel in (
                "03_new_novel/chapter_plan.csv",
                "03_new_novel/chapter_plan_parts",
                "03_new_novel/continuity_ledger.md",
                "03_new_novel/foreshadowing_ledger.md",
                "03_new_novel/memory_rollup.md",
            ):
                self._archive_project_path(rel, backup_dir)

        directive_path = self._append_revision_directive(note)
        if backup_dir:
            self._log(f"开始重写详细章纲：旧章纲已归档到 {backup_dir.relative_to(self.project)}")
        self._log(f"章纲重写规则已写入 {directive_path.relative_to(self.project)}")

        self.state["phase"] = "decomposed"
        self.state["awaiting"] = None
        self.state["activity"] = "详细章纲重写中；若中断，下一次启动会从已有 chapter_plan_parts 续跑。"
        self._save_state()

        context = self._blueprint_source_context()
        path = self._generate_chapter_plan_file(context, target_chapters)

        # 后续记忆必须基于新版详细章纲重新生成，不能沿用旧占位或旧章纲记忆。
        for rel, content in (
            ("03_new_novel/continuity_ledger.md", "# Continuity Ledger\n\n状态：待生成\n"),
            ("03_new_novel/foreshadowing_ledger.md", "# Foreshadowing Ledger\n\n状态：待生成\n"),
            ("03_new_novel/memory_rollup.md", "# Memory Rollup\n\n状态：待生成\n"),
        ):
            if not (self.project / rel).exists():
                write_text(self.project / rel, content)

        self.state.setdefault("revisions", []).append(
            {
                "type": "chapter_plan_rewrite",
                "note": note,
                "files": [str(path.relative_to(self.project)).replace("\\", "/")],
                "backup": str(backup_dir) if backup_dir else "",
                "resumed_from_existing_parts": resume_existing_parts,
                "revised_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
        )
        self.state["phase"] = "decomposed"
        self.state["awaiting"] = None
        self.state["activity"] = "详细章纲已由 agent 分段重写；下一次启动会继续生成连续性账本、伏笔账本和长期记忆。"
        self._save_state()
        if backup_dir:
            return f"详细章纲重写完成：{path.relative_to(self.project)}。旧章纲已备份到：{backup_dir}"
        return f"详细章纲续跑完成：{path.relative_to(self.project)}。"

    def revise_draft_batch(self, note: str, awaiting: dict[str, Any]) -> str:
        from .cli import read_optional, read_text, write_text

        self._record_anti_ai_note(note)
        target_raw = str(awaiting.get("target") or "").replace("\\", "/")
        match = re.search(r"05_drafts/ch_(\d+)_to_(\d+)\.md$", target_raw)
        if not match:
            return "当前正文节点没有可识别的正文批次文件，不能自动重写。"
        start = int(match.group(1))
        end = int(match.group(2))
        draft_path = self.project / target_raw
        if not draft_path.exists():
            return f"正文批次文件不存在：{target_raw}"

        if self._draft_revision_is_review_only(note):
            self._log(f"按要求只重新审稿当前批次：第 {start:04d}-{end:04d} 章")
            self.review_batch(start, end)
            self.state["awaiting"] = {
                "type": "review",
                "target": target_raw,
                "message": f"第 {start:04d}-{end:04d} 章已重新审稿，请检查审稿意见和正文。",
            }
            self._save_state()
            return f"已重新审稿第 {start:04d}-{end:04d} 章，未改正文。"

        original = read_text(draft_path)
        review_text = self._review_text_for_range(start, end)
        header, chapters = self._split_draft_batch(original, start, end)
        if len(chapters) < (end - start + 1):
            self._log(f"正文批次解析到 {len(chapters)} 章，将按已识别章节重写：第 {start:04d}-{end:04d} 章")

        backup_dir = self.project / "05_drafts" / "_revisions"
        backup_path = backup_dir / f"ch_{start:04d}_to_{end:04d}_before_{self._stamp()}.md"
        write_text(backup_path, original)
        self._log(f"原正文已备份：{backup_path.relative_to(self.project)}")

        rewritten: list[str] = []
        previous_tail = self._previous_tail(start)
        for chapter_no in range(start, end + 1):
            plan = self._chapter_plan_row(chapter_no)
            old_chapter = chapters.get(chapter_no, "")
            if not old_chapter:
                self._log(f"第 {chapter_no:04d} 章未在批次文件中识别，按细纲补写")
            prompt = self._draft_revision_prompt(chapter_no, old_chapter, previous_tail, note, review_text)
            self._log(f"调用模型重写正文：第 {chapter_no:04d} 章")
            chapter_text = self._complete_with_preview(
                prompt,
                stream_label=f"重写正文{chapter_no:04d}",
                system=CHAPTER_DRAFT_SYSTEM_PROMPT,
                max_tokens=self.options.max_tokens_chapter,
                temperature=0.58,
            ).strip()
            chapter_text = self._strip_accidental_file_blocks(chapter_text)
            chapter_text = self._enforce_chapter_length(
                chapter_no,
                chapter_text,
                stream_label=f"压缩重写{chapter_no:04d}",
            )
            chapter_text = self._humanize_chapter_style(
                chapter_no,
                chapter_text,
                plan,
                stream_label=f"去AI味重写{chapter_no:04d}",
            )
            chapter_text = self._enforce_chapter_length(
                chapter_no,
                chapter_text,
                stream_label=f"去AI味压缩重写{chapter_no:04d}",
            )
            if not self._chapter_heading_title(chapter_text, chapter_no):
                chapter_text = self._enforce_short_chapter_title(chapter_no, chapter_text, plan)
            rewritten.append(chapter_text)
            previous_tail = chapter_text[-900:]
            self._log(f"完成重写正文：第 {chapter_no:04d} 章，约 {len(chapter_text)} 字符")

        new_header = header.strip() if header.strip() else f"# 第 {start:04d}-{end:04d} 章正文"
        write_text(draft_path, "\n\n".join([new_header] + rewritten).strip() + "\n")
        self._log(f"正文批次已重写：{draft_path.name}")

        self.summarize_batch(start, end)
        self.review_batch(start, end)
        self.state.setdefault("revisions", []).append(
            {
                "type": "draft",
                "note": note,
                "files": [target_raw],
                "backup": str(backup_path.relative_to(self.project)).replace("\\", "/"),
                "review": f"07_reviews/review_{start:04d}_to_{end:04d}.md",
                "revised_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
        )
        self.state["phase"] = "writing"
        self.state["awaiting"] = {
            "type": "review",
            "target": target_raw,
            "message": f"第 {start:04d}-{end:04d} 章已重写并重新审稿，请检查正文和审稿意见。",
        }
        self._save_state()
        return f"已重写第 {start:04d}-{end:04d} 章，并重新生成审稿意见。"

    def _enforce_chapter_length(self, chapter_no: int, chapter_text: str, stream_label: str) -> str:
        text = chapter_text.strip()
        if len(text) <= CHAPTER_HARD_CHAR_LIMIT:
            return text

        source = text
        for attempt in range(1, 3):
            target_limit = CHAPTER_TARGET_CHAR_LIMIT if attempt == 1 else 4200
            self._log(
                f"第 {chapter_no:04d} 章超出 {CHAPTER_HARD_CHAR_LIMIT} 字符："
                f"{len(source)} 字符，自动压缩到 {target_limit} 字符以内（第 {attempt}/2 次）"
            )
            prompt = f"""# 单章超长压缩重写

当前第 {chapter_no:04d} 章长度为 {len(source)} 字符，超过平台限制。

请把这一章压缩重写到 {target_limit} 字符以内，并且绝对不能超过 {CHAPTER_HARD_CHAR_LIMIT} 字符。

保留：
- 本章核心事件、人物行动、关键爽点、收益、伏笔和章末钩子。
- 已有主线承接、金手指规则、人物关系和后续必需信息。

删减/压缩：
- 重复动作、重复心理、重复环境描写。
- 解释性设定、过密旁白、同义反复句。
- 不影响本章推进的支线信息。

输出要求：
- 只输出第 {chapter_no:04d} 章完整正文。
- 不要解释，不要摘要，不要 FILE_BUNDLE。
- 语言保持中文网文正文风格，句子顺滑，降低 AI 味。

## 原章节

{source}
"""
            compressed = self._complete_with_preview(
                prompt,
                stream_label=stream_label,
                system=CHAPTER_DRAFT_SYSTEM_PROMPT,
                max_tokens=min(self.options.max_tokens_chapter, 7000),
                temperature=0.35,
            ).strip()
            compressed = self._strip_accidental_file_blocks(compressed)
            if len(compressed) <= CHAPTER_HARD_CHAR_LIMIT:
                self._log(f"第 {chapter_no:04d} 章已压缩到 {len(compressed)} 字符")
                return compressed.strip()
            source = compressed.strip()

        raise RuntimeError(
            f"第 {chapter_no:04d} 章两次压缩后仍超过 {CHAPTER_HARD_CHAR_LIMIT} 字符，"
            "已停止保存该批次；请使用更强的压缩要求重新运行。"
        )

    def _polish_chapter_language(
        self,
        chapter_no: int,
        chapter_text: str,
        plan: dict[str, str] | None = None,
        stream_label: str = "语言质检",
    ) -> str:
        from .cli import read_optional

        source = self._local_language_cleanup(chapter_text.strip())
        prompt = f"""# 单章语言质检润色：第 {chapter_no:04d} 章

任务：只对本章做语言顺滑、对话自然化、标题贴合度修正。不要改剧情，不要新增剧情，不要删掉关键事件。

## 本章细纲

{self._chapter_plan_text(plan or {})}

## 文风规则摘要

{read_optional(self.project / "03_new_novel" / "style_guide.md", 3500)}

## 必须修复的问题

1. 先在内部完成一轮开放式语言自检，不只检查固定词表。凡是读者需要停下来猜意思、像 AI 压缩出来、像设定词硬拼、像作者自己造词的表达，都必须改写。
2. 删除或改写 AI 味强的压缩词、自造词、抽象词，包括但不限于：尸位、封话、抢证、压差、战臂、总调印尾纹、削名范围、第二张嘴、撕史、审史、禁关、封史、验史、破册、撕账这类读者需要停下来猜的说法。
3. 优先使用当前题材网文里已经成熟、常见、读者不用猜的表达；不确定是不是常用词时，一律改成完整普通短语，不要为了显得高级而造新简称。
4. 严禁把复杂动作、制度、历史线、商业流程、证据链、能力机制压缩成两字/三字自造词；除非 style_guide 或设定包已明确定义，否则不要发明新术语。例如把“撕史”改成“撕开旧史伪证”，把“审史”改成“重审旧案”，把“禁关”改成“封锁关口”。
5. 对话要像真人在当前局势下说话：短、准、有情绪、有目的；不要旁白腔解释设定。
6. 叙述要顺，不要关键词堆叠；设定词可以保留，但首次或关键处要用上下文说清。
7. 章节标题要贴合本章内容，有当前目标平台的目录钩子；不要为了四字标题强行造词，通常 4-10 个汉字，必要时 12 个汉字。
8. 单章绝对不能超过 {CHAPTER_HARD_CHAR_LIMIT} 字符，尽量控制在 {LANGUAGE_POLISH_TARGET_LIMIT} 字符以内。

## 输出要求

- 只输出第 {chapter_no:04d} 章完整正文。
- 不要解释，不要摘要，不要列修改说明，不要 FILE_BUNDLE。
- 保留本章核心事件、人物行动、战斗结果、获得物、伏笔和章末钩子。

## 原章节

{source}
"""
        self._log(f"语言质检润色：第 {chapter_no:04d} 章")
        polished = self._complete_with_preview(
            prompt,
            stream_label=stream_label,
            max_tokens=min(self.options.max_tokens_chapter, 7000),
            temperature=0.25,
        ).strip()
        polished = self._strip_accidental_file_blocks(polished)
        polished = self._local_language_cleanup(polished)
        if not polished:
            return source
        return polished

    def _chapter_plan_text(self, plan: dict[str, str]) -> str:
        labels = {
            "chapter_no": "章节",
            "volume": "所在卷",
            "title": "标题",
            "core_conflict": "本章必须写出的具体事件链",
            "small_hook": "开场触发点",
            "power_usage": "能力/资源/证据使用",
            "character_change": "人物变化",
            "foreshadowing": "伏笔",
            "ending_hook": "章末必须落到的钩子",
            "mainline_progress": "本章必须完成的主线结果",
            "source_inspiration": "借鉴素材",
            "status": "状态",
        }
        lines = []
        for key, value in plan.items():
            if value:
                lines.append(f"- {labels.get(key, key)}: {value}")
        return "\n".join(lines) or "（未找到本章细纲，请保持原章节剧情功能。）"

    def _local_language_cleanup(self, text: str) -> str:
        replacements = {
            "尸位": "尸身位置",
            "封话": "封口",
            "一刀压差": "一刀压跪",
            "一条战臂": "一个能顶正面的硬手",
            "总调印尾纹": "总调印残纹",
            "削名范围": "削名刀划过的名单",
            "第二张嘴": "第二个活口",
            "撕史": "撕开旧史伪证",
            "审史": "重审旧案",
            "封史": "封存旧史",
            "验史": "查验旧史",
            "破册": "破开旧册",
            "撕账": "撕开黑账",
            "私开禁关": "私开边关禁门",
            "禁关": "边关禁门",
            "篡史": "篡改旧史",
            "黑账炸夜": "黑账炸开",
            "残账三线": "残账牵线",
            "出道抓人": "出口被围",
            "守碑地从": "红药入场",
            "关系口": "关系渠道",
            "资源口": "资源渠道",
            "信息口": "消息渠道",
            "证据口": "证据入口",
            "流程口": "流程漏洞",
            "权限口": "权限漏洞",
            "资本口": "资本渠道",
            "校霸口": "校霸那条线",
            "医院口": "医院那条线",
            "董事口": "董事会那条线",
            "任务口": "任务入口",
            "打脸口": "打脸机会",
            "爽点口": "爽点位置",
            "压场口": "压场机会",
            "收束口": "收尾节点",
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        text = re.sub(r"抢证(?!据)", "抢证据", text)
        return text

    def _enforce_short_chapter_title(
        self,
        chapter_no: int,
        chapter_text: str,
        plan: dict[str, str] | None = None,
    ) -> str:
        plan = plan or {}
        current_title = self._chapter_heading_title(chapter_text, chapter_no)
        title = (
            current_title
            if self._acceptable_chapter_title(current_title)
            else self._short_chapter_title(chapter_no, chapter_text, plan)
        )
        fixed_heading = f"第{chapter_no:04d}章 {title}"
        pattern = re.compile(rf"(?m)^\s*(?:#{{1,6}}\s*)?第\s*0*{chapter_no}\s*章[^\r\n]*")
        text = chapter_text.strip()
        if pattern.search(text):
            return pattern.sub(fixed_heading, text, count=1).strip()
        return f"{fixed_heading}\n\n{text}".strip()

    def _short_chapter_title(self, chapter_no: int, chapter_text: str, plan: dict[str, str]) -> str:
        candidates = [
            self._chapter_heading_title(chapter_text, chapter_no),
            plan.get("core_conflict", ""),
            plan.get("small_hook", ""),
            plan.get("ending_hook", ""),
        ]
        for candidate in candidates:
            clean = self._clean_short_title(candidate)
            if clean:
                return clean

        context = " ".join(str(value) for value in candidates if value) + " " + chapter_text[:1200]
        keyword_titles = [
            ("反杀", "当场反杀"),
            ("突破", "一夜破境"),
            ("越级", "越级斩敌"),
            ("妖兽", "妖血开路"),
            ("秘境", "秘境开门"),
            ("宗门", "宗门压来"),
            ("王朝", "王城来人"),
            ("天才", "天才低头"),
            ("祭坛", "祭坛见血"),
            ("追杀", "反追杀令"),
            ("试炼", "试炼翻盘"),
            ("宝药", "宝药有毒"),
            ("遗迹", "遗迹惊变"),
            ("禁地", "禁地放行"),
            ("血脉", "血脉觉醒"),
            ("功法", "古法入手"),
            ("地图", "新图开门"),
        ]
        for keyword, title in keyword_titles:
            if keyword in context:
                return title

        for candidate in candidates:
            fallback = self._fallback_short_title(candidate)
            if fallback:
                return fallback
        return "暗线浮出"

    def _acceptable_chapter_title(self, value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        if any(mark in text for mark in "《》〈〉【】[]，,。！？!?：:；;"):
            return False
        clean = re.sub(r"\s+", "", text)
        chinese_count = len(re.findall(r"[\u4e00-\u9fff]", clean))
        if not (4 <= chinese_count <= 12):
            return False
        bad_fragments = ("他", "我", "自己", "终于")
        if any(fragment in clean for fragment in bad_fragments):
            return False
        if clean.endswith(("从", "的", "了", "着", "在", "把", "被", "让", "要", "给")):
            return False
        return True

    def _chapter_heading_title(self, chapter_text: str, chapter_no: int) -> str:
        match = re.search(rf"(?m)^\s*(?:#{{1,6}}\s*)?第\s*0*{chapter_no}\s*章\s*([^\r\n]*)", chapter_text)
        return match.group(1).strip() if match else ""

    def _clean_short_title(self, value: str) -> str:
        text = str(value or "").strip()
        text = re.sub(r"^第\s*0*\d+\s*章\s*", "", text)
        text = re.sub(r"[《》〈〉#*【】\[\]（）()“”\"'：:，,。！？!?\s　、·—\-]+", "", text)
        for word in ("他", "我"):
            text = text.replace(word, "")
        if re.fullmatch(r"[\u4e00-\u9fff]{4,10}", text) and self._acceptable_chapter_title(text):
            return text
        return ""

    def _fallback_short_title(self, value: str) -> str:
        text = str(value or "")
        text = re.sub(r"第\s*0*\d+\s*章", "", text)
        for word in (
            "他",
            "我",
            "自己",
            "第一次",
            "终于",
            "当场",
            "今夜",
            "连夜",
        ):
            text = text.replace(word, "")
        parts = re.findall(r"[\u4e00-\u9fff]{2,}", text)
        joined = "".join(parts)
        if len(joined) >= 4:
            title = joined[:10]
            return title if self._acceptable_chapter_title(title) else joined[:6]
        if len(joined) >= 2:
            return (joined + "惊变")[:4]
        return ""

    def _draft_revision_is_review_only(self, note: str) -> bool:
        normalized = note.lower()
        review_markers = ("只审稿", "仅审稿", "重新审稿", "重审", "再审", "不改正文", "不重写", "不用重写")
        if not any(marker in normalized for marker in review_markers):
            return False
        rewrite_markers = ("润色", "改写", "修改正文", "降低ai", "ai味", "优化语句")
        if any(marker in normalized for marker in rewrite_markers):
            return False
        return "重写" not in normalized or "不重写" in normalized or "不用重写" in normalized

    def _review_text_for_range(self, start: int, end: int) -> str:
        from .cli import read_optional

        exact = self.project / "07_reviews" / f"review_{start:04d}_to_{end:04d}.md"
        if exact.exists():
            return read_optional(exact, 30_000)
        candidates = sorted((self.project / "07_reviews").glob("review_*_to_*.md"))
        overlaps: list[tuple[int, int, Path]] = []
        for path in candidates:
            match = re.search(r"review_(\d+)_to_(\d+)\.md$", path.name)
            if not match:
                continue
            review_start = int(match.group(1))
            review_end = int(match.group(2))
            if review_end < start or review_start > end:
                continue
            overlaps.append((review_start, review_end, path))
        if not overlaps:
            return "（暂无审稿报告；请按用户微调要求和原章节自行修订。）"
        chunks = []
        for review_start, review_end, path in overlaps[-3:]:
            chunks.append(
                f"## {path.name}（覆盖第 {review_start:04d}-{review_end:04d} 章）\n\n"
                f"{read_optional(path, 12_000)}"
            )
        return "\n\n".join(chunks)

    def _split_draft_batch(self, text: str, start: int, end: int) -> tuple[str, dict[int, str]]:
        pattern = re.compile(r"(?m)^(?:#{1,6}\s*)?第0*(\d{1,4})章[^\n]*$")
        matches = [match for match in pattern.finditer(text) if start <= int(match.group(1)) <= end]
        if not matches:
            return text, {}
        header = text[: matches[0].start()].strip()
        chapters: dict[int, str] = {}
        for index, match in enumerate(matches):
            chapter_no = int(match.group(1))
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            chapters[chapter_no] = text[match.start() : next_start].strip()
        return header, chapters

    def _remove_batch_power_ledger_block(self, text: str) -> str:
        return re.sub(
            r"\n## 本批战力台账（补充）[\s\S]*?\n## 本批写作依据",
            "\n## 本批写作依据",
            text,
            count=1,
        )

    def revise_power_range(self, start: int, end: int) -> str:
        from .cli import read_text, write_text

        start = max(1, int(start or 1))
        end = int(end or start)
        if end < start:
            return f"战力境界回修范围无效：第 {start:04d}-{end:04d} 章。"

        draft_dir = self.project / "05_drafts"
        batches: list[tuple[int, int, Path]] = []
        for path in sorted(draft_dir.glob("ch_*_to_*.md")):
            match = re.search(r"ch_(\d+)_to_(\d+)\.md$", path.name)
            if not match:
                continue
            batch_start = int(match.group(1))
            batch_end = int(match.group(2))
            if batch_end < start or batch_start > end:
                continue
            batches.append((batch_start, batch_end, path))

        if not batches:
            return f"没有找到覆盖第 {start:04d}-{end:04d} 章的正文批次文件。"

        self._log(f"开始战力境界自然回修：第 {start:04d}-{end:04d} 章；将按当前设定重写范围内战力境界表达")
        touched: list[str] = []
        rewritten_count = 0
        backup_dir = draft_dir / "_power_revisions"

        for batch_start, batch_end, draft_path in batches:
            overlap_start = max(start, batch_start)
            overlap_end = min(end, batch_end)
            original = read_text(draft_path)
            cleaned = self._remove_batch_power_ledger_block(original)
            header, chapters = self._split_draft_batch(cleaned, batch_start, batch_end)
            if not chapters:
                self._log(f"跳过无法解析章节的批次：{draft_path.name}")
                continue

            backup_path = backup_dir / f"{draft_path.stem}_before_power_{self._stamp()}.md"
            write_text(backup_path, original)
            self._log(f"已备份原批次：{backup_path.relative_to(self.project)}")

            rebuilt: list[str] = []
            previous_tail = self._previous_tail(batch_start)
            for chapter_no in range(batch_start, batch_end + 1):
                old_chapter = chapters.get(chapter_no, "").strip()
                if not old_chapter:
                    continue
                if not (overlap_start <= chapter_no <= overlap_end):
                    rebuilt.append(old_chapter)
                    previous_tail = old_chapter[-900:]
                    continue

                plan = self._chapter_plan_row(chapter_no)
                prompt = self._power_rewrite_prompt(chapter_no, old_chapter, previous_tail)
                self._log(f"调用模型回修战力境界：第 {chapter_no:04d} 章")
                chapter_text = self._complete_with_preview(
                    prompt,
                    stream_label=f"战力回修{chapter_no:04d}",
                    max_tokens=self.options.max_tokens_chapter,
                    temperature=0.35,
                ).strip()
                chapter_text = self._strip_accidental_file_blocks(chapter_text)
                chapter_text = self._enforce_short_chapter_title(chapter_no, chapter_text, plan)
                chapter_text = self._enforce_chapter_length(
                    chapter_no,
                    chapter_text,
                    stream_label=f"战力回修压缩{chapter_no:04d}",
                )
                chapter_text = self._enforce_short_chapter_title(chapter_no, chapter_text, plan)
                chapter_text = self._polish_chapter_language(
                    chapter_no,
                    chapter_text,
                    plan,
                    stream_label=f"语言质检战力回修{chapter_no:04d}",
                )
                chapter_text = self._enforce_chapter_length(
                    chapter_no,
                    chapter_text,
                    stream_label=f"质检压缩战力回修{chapter_no:04d}",
                )
                chapter_text = self._enforce_short_chapter_title(chapter_no, chapter_text, plan)
                rebuilt.append(chapter_text)
                previous_tail = chapter_text[-900:]
                rewritten_count += 1
                self._log(f"完成战力境界回修：第 {chapter_no:04d} 章，约 {len(chapter_text)} 字符")

            new_header = header.strip() if header.strip() else f"# 第{batch_start:04d}-{batch_end:04d}章正文"
            write_text(draft_path, "\n\n".join([new_header] + rebuilt).strip() + "\n")
            touched.append(str(draft_path.relative_to(self.project)).replace("\\", "/"))
            self._log(f"批次已写回：{draft_path.name}（回修第 {overlap_start:04d}-{overlap_end:04d} 章）")

        self.state.setdefault("revisions", []).append(
            {
                "type": "power_range",
                "start": start,
                "end": end,
                "files": touched,
                "revised_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
        )
        self._save_state()
        return f"已完成战力境界自然回修：第 {start:04d}-{end:04d} 章，共回修 {rewritten_count} 章。"

    def _power_stage_hint(self, chapter_no: int) -> str:
        return (
            "按当前项目的 power_system、power_state_ledger、chapter_plan 和 full_story_outline 判断本章定境；"
            "只补清当前境界/等级、能力状态、关键道具权限、敌我差距、胜负依据、代价和下一突破门槛。"
        )

    def _power_rewrite_prompt(self, chapter_no: int, old_chapter: str, previous_tail: str) -> str:
        from .cli import markdown_table, read_optional

        plan = self._chapter_plan_row(chapter_no)
        plan_text = markdown_table([plan]) if plan else "（未找到本章细纲。必须保持原章节剧情功能，不要改事件顺序。）"
        stage_hint = self._power_stage_hint(chapter_no)
        return f"""# 已写章节战力境界自然回修：第 {chapter_no:04d} 章

任务：在不改变原章节剧情、人物行为、对白大意、事件顺序、伏笔和章节结尾钩子的前提下，只回修“战力境界/升级升境/越级赢法”相关表达。

这不是重写新剧情。你必须保留原章故事走向，只把境界信息自然融入动作、压迫、突破反馈、伤势代价、旁人反应和战斗判断里。

## 核心规则

1. 如果用户把范围设为第0001章或第0002章，也允许按当前新设定回修；若未选中则不要额外改动。
2. 只输出第 {chapter_no:04d} 章完整正文，不要解释、不要修订说明、不要 FILE_BUNDLE。
3. 保留原章节标题；除非标题明显不通顺，否则不要改标题。
4. 必须保留原剧情事件、出场人物、场景推进、对白大意、战斗结果、获得物、伏笔和章末钩子。
5. 只在需要的地方自然补入境界信息：当前修为/等级、当前小段或阶段、核心能力解锁状态、关键道具权限、敌我差距、胜负依据和代价。
6. 境界描写要像网文正文，不要像资料卡。不要在章首塞“战力台账”，不要使用列表，不要生硬解释。
7. 每章通常补 2-5 处即可：战斗前压迫、出手时的力量差、破局瞬间、伤势代价、旁观者判断。不要满章堆设定。
8. 主角可以低境高战，但不能无成本乱杀。越级越大，必须写清靠什么赢：能力规则、资源积累、源书复用桥段中的破局办法、地形规则、对方破绽或以伤换胜。
9. 境界、能力和道具的获得顺序必须服从当前项目设定，不能临时新增无边界能力，也不能把未解锁能力提前写成常用能力。
10. 如果某些能力或状态属于秘密，普通配角不能无缘无故准确叫破。
11. 当前是完全融合授权复用：可保留并融合源书整段剧情链、爽点段、人物功能段、升级段和境界骨架；只统一姓名、背景、势力名、设定名和承接逻辑。
12. 语言要顺、要口语化、要符合当前目标平台男频爽文读感；不要自造难懂词，不要写读者看不懂的压缩句。严禁“撕史、审史、禁关、封史、验史、破册、撕账”这类未定义缩略词，必须改成“撕开旧史伪证、重审旧案、封锁关口、查验旧册、撕开黑账”等完整表达。
13. 单章绝对不能超过 5000 字符；如原章已接近上限，优先删掉重复心理、重复动作和解释性废话。

## 本章定境

{stage_hint}

## 定境硬节点

以当前项目的 power_system、power_state_ledger、full_story_outline 和 chapter_plan 为准；如果旧稿与当前设定冲突，按当前项目设定自然回修，不沿用旧项目专名、旧主角名或旧金手指名。

## 力量体系与当前台账

### power_system
{read_optional(self.project / "03_new_novel" / "power_system.md", 9000)}

### power_state_ledger
{read_optional(self.project / "03_new_novel" / "power_state_ledger.md", 9000)}

### continuity_ledger
{read_optional(self.project / "03_new_novel" / "continuity_ledger.md", 5000)}

## 本章细纲

{plan_text}

## 上一章尾段

{previous_tail}

## 原章节正文

{old_chapter}

## 输出

只输出第 {chapter_no:04d} 章完整正文。"""

    def _draft_revision_prompt(self, chapter_no: int, old_chapter: str, previous_tail: str, note: str, review_text: str) -> str:
        from .cli import read_optional

        plan = self._chapter_plan_row(chapter_no)
        plan_text = self._draft_plan_text(plan) if plan else "（未找到本章细纲，请保持原章节剧情功能，不要偏离当前批次主线。）"
        style_reference = self._source_style_reference(max_chars=7200, max_books=5)
        anti_ai_feedback = self._anti_ai_feedback()
        anti_ai_feedback_block = (
            "## 用户改稿与检测反馈\n\n"
            "下面内容用于让模型对比旧稿问题和用户认可稿的差异，只学习语气、对白自然度、段落松紧和标题口味；"
            "不作为剧情设定，不覆盖本章章纲。\n\n"
            f"{anti_ai_feedback}\n\n"
            if anti_ai_feedback
            else ""
        )
        return f"""# 正文批次自动重写：第 {chapter_no:04d} 章

用户微调/重写要求：
{note}

目标：重写当前章节。剧情事实、人物关系、金手指规则和后续承接按本书资料与本章章纲执行；标题口味、句子节奏、对白语气和正文读感参考本地样书与用户改稿反馈。

## 当前批次审稿报告

{review_text}

审稿报告只用于定位硬伤，不提供文风规则；如果它和本地样书/用户反馈冲突，以本地样书和用户反馈为准。

## 本地样书口味参考

下面内容只用于学习标题口味、开场速度、段落长度、对白比例和飞卢都市爽文读感；不要照抄其中人名、句子、标题和剧情。

{style_reference or "（未找到本地样书正文，本章按本书资料和章纲直接写。）"}

{anti_ai_feedback_block}
## 本书事实底座

### novel_bible
{read_optional(self.project / "03_new_novel" / "novel_bible.md", 2600)}

### character_table
{read_optional(self.project / "03_new_novel" / "character_table.csv", 1800)}

### worldbuilding
{read_optional(self.project / "03_new_novel" / "worldbuilding.md", 1400)}

### power_system
{read_optional(self.project / "03_new_novel" / "power_system.md", 1800)}

### power_state_ledger
{read_optional(self.project / "03_new_novel" / "power_state_ledger.md", 1600)}

### memory_rollup
{read_optional(self.project / "03_new_novel" / "memory_rollup.md", 1800)}

### continuity_ledger
{read_optional(self.project / "03_new_novel" / "continuity_ledger.md", 1600)}

## 本章事件底座

{plan_text}

## 上一章尾段/前文承接

{previous_tail}

## 原章节

{old_chapter or "（原章节缺失，请按细纲补写本章。）"}

## 输出要求

只输出第 {chapter_no:04d} 章完整正文，不要解释，不要摘要，不要 FILE_BUNDLE。

1. 章节标题格式为“第{chapter_no:04d}章 标题”；标题由你根据本章正文、样书目录口味和用户反馈重起，不要照抄章纲参考标题。
2. 章纲只负责剧情、人物、战力、道具、伏笔、结果和章末承接；正文写成小说场景，不要复述章纲字段。
3. 直接进场景，用人物行动、对话、围观反应和结果推进。
4. 金手指、战力、资源、证据链遵守本书资料，不临时新增无边界能力。
5. 旧稿可以大改；不要保留旧稿里不顺口、不像真人说话、像解释说明的句子。
6. 单章约 {self.profile['chapter_words']} 字，绝对不能超过 5000 字符。
"""

    def _blueprint_revision_scope(self, note: str) -> tuple[list[str], dict[str, int], str, int]:
        normalized = note.lower()
        power_markers = ("力量体系", "境界", "修炼", "等级", "升级", "战力", "越级", "突破")
        chapter_markers = ("章节", "章纲", "chapter_plan", "剧情节点", "前期", "中期", "后期", "终局")
        full_outline_markers = ("全文", "全书", "完整大纲", "大结局", "结局", "终章", "最后一章", "最终章")

        if any(marker in normalized for marker in full_outline_markers):
            rel_files = [
                "03_new_novel/novel_bible.md",
                "03_new_novel/volume_outline.md",
                "03_new_novel/full_story_outline.md",
                "03_new_novel/fusion_traceability.md",
                "03_new_novel/chapter_plan.csv",
                "03_new_novel/power_system.md",
                "03_new_novel/power_state_ledger.md",
                "03_new_novel/memory_rollup.md",
                "03_new_novel/foreshadowing_ledger.md",
            ]
            max_chars = {
                "03_new_novel/novel_bible.md": 9000,
                "03_new_novel/volume_outline.md": 9000,
                "03_new_novel/full_story_outline.md": 16000,
                "03_new_novel/fusion_traceability.md": 12000,
                "03_new_novel/chapter_plan.csv": 14000,
                "03_new_novel/power_system.md": 7000,
                "03_new_novel/power_state_ledger.md": 7000,
                "03_new_novel/memory_rollup.md": 7000,
                "03_new_novel/foreshadowing_ledger.md": 7000,
            }
            instruction = (
                "本次判断为全书规划/大结局方向微调。优先输出完整的 "
                "`03_new_novel/full_story_outline.md`，必要时同步输出 `03_new_novel/volume_outline.md`。"
                "不要一次性输出 800 行 `chapter_plan.csv`；如果需要章节级细纲，只补充关键终局章节或写明后续分批扩展规则。"
            )
            return rel_files, max_chars, instruction, min(self.options.max_tokens_blueprint, 9000)

        if any(marker in normalized for marker in power_markers):
            rel_files = [
                "03_new_novel/power_system.md",
                "03_new_novel/power_state_ledger.md",
                "03_new_novel/memory_rollup.md",
                "03_new_novel/continuity_ledger.md",
                "03_new_novel/novel_bible.md",
            ]
            if any(marker in normalized for marker in chapter_markers):
                rel_files.append("03_new_novel/volume_outline.md")
            max_chars = {
                "03_new_novel/power_system.md": 18000,
                "03_new_novel/power_state_ledger.md": 10000,
                "03_new_novel/memory_rollup.md": 8000,
                "03_new_novel/continuity_ledger.md": 8000,
                "03_new_novel/novel_bible.md": 8000,
                "03_new_novel/volume_outline.md": 7000,
            }
            instruction = (
                "本次判断为力量体系/境界等级微调。优先只输出完整的 "
                "`03_new_novel/power_system.md` 和 `03_new_novel/power_state_ledger.md`；如果需要让后续正文继承，再追加输出 "
                "`03_new_novel/memory_rollup.md` 或 `03_new_novel/continuity_ledger.md`。"
                "不要输出 `chapter_plan.csv`，除非用户明确要求逐章重排。"
            )
            return rel_files, max_chars, instruction, min(self.options.max_tokens_blueprint, 8000)

        rel_files = [
            "03_new_novel/novel_bible.md",
            "03_new_novel/style_guide.md",
            "03_new_novel/power_system.md",
            "03_new_novel/power_state_ledger.md",
            "03_new_novel/worldbuilding.md",
            "03_new_novel/character_table.csv",
            "03_new_novel/volume_outline.md",
            "03_new_novel/full_story_outline.md",
            "03_new_novel/fusion_traceability.md",
            "03_new_novel/chapter_plan.csv",
            "03_new_novel/continuity_ledger.md",
            "03_new_novel/foreshadowing_ledger.md",
            "03_new_novel/memory_rollup.md",
        ]
        max_chars = {
            "03_new_novel/chapter_plan.csv": 22000,
            "03_new_novel/novel_bible.md": 14000,
            "03_new_novel/power_system.md": 14000,
            "03_new_novel/power_state_ledger.md": 10000,
            "03_new_novel/full_story_outline.md": 18000,
            "03_new_novel/fusion_traceability.md": 12000,
        }
        instruction = (
            "优先修改 `03_new_novel/power_system.md`、`03_new_novel/power_state_ledger.md`、`03_new_novel/novel_bible.md`、"
            "`03_new_novel/memory_rollup.md`、`03_new_novel/continuity_ledger.md`。"
        )
        return rel_files, max_chars, instruction, self.options.max_tokens_blueprint

    def revise_blueprint(self, note: str) -> str:
        from .cli import read_optional, read_text, write_text

        rel_files, max_chars, scope_instruction, max_tokens = self._blueprint_revision_scope(note)
        context = []
        for rel in rel_files:
            context.append(f"## {rel}\n\n{read_optional(self.project / rel, max_chars.get(rel, 9000))}")
        before_texts = {
            rel: read_text(self.project / rel) if (self.project / rel).exists() else ""
            for rel in rel_files
        }

        prompt = f"""# 融合设定包节点微调

用户微调要求：
{note}

请在现有融合设定包基础上做增量修订，不要推翻整体主线，除非用户明确要求。
本次输出会直接覆盖对应项目文件，所以：

1. 只输出需要修改的完整文件，不要输出片段。
2. {scope_instruction}
3. 如果用户要求涉及章节推进，可把章节级执行规则写进 memory_rollup/continuity_ledger；只有能输出完整 CSV 时才修改 `chapter_plan.csv`。
4. 保持目标平台强爽节奏，改动要能被后续正文写作直接继承。
5. {self._fusion_policy_block()}
6. 用户要求“去 AI 味”时，只修改设定、章纲、记忆和后续写作规则，不要在本步骤输出正文。
7. {ANTI_AI_OUTLINE_RULE}

现有设定包：

{chr(10).join(context)}

{FILE_PROTOCOL}
"""
        self._log(f"开始应用节点微调：{note[:80]}")
        limit_text = f"{max_tokens} tokens" if max_tokens and max_tokens > 0 else "不设置 max_tokens 上限"
        self._log(f"微调范围：读取 {len(rel_files)} 个设定文件，输出上限 {limit_text}")
        self._log("微调调用模型中：将根据你的要求重写相关设定文件")
        try:
            result = self._complete_with_preview(
                prompt,
                stream_label="微调",
                max_tokens=max_tokens,
                temperature=0.45,
            )
        except Exception as exc:
            detail = str(exc).replace("\n", " ")[:260]
            self._log(f"微调失败：{detail}")
            self.state["activity"] = f"微调失败：{detail}"
            self.state["awaiting"] = {
                "type": "blueprint",
                "message": "微调模型调用失败，设定包没有被修改。请缩小微调范围后重试，或确认当前设定继续写正文。",
                "target": "03_new_novel/novel_bible.md",
            }
            self._save_state()
            raise
        self._log("微调模型已返回，正在解析并写入文件")
        written = self._write_file_bundle(result)
        changed_files = []
        if not written:
            target = self.project / "03_new_novel" / f"revision_raw_{self._stamp()}.md"
            write_text(target, result.strip() + "\n")
            self._log(f"微调输出未识别为文件包，已保存原始结果：{target.name}")
        else:
            self._log(f"微调已写入 {len(written)} 个文件")
            for path in written:
                rel = str(path.relative_to(self.project)).replace("\\", "/")
                before = before_texts.get(rel, "")
                after = read_text(path)
                changed_files.append(
                    {
                        "file": rel,
                        "before_chars": len(before),
                        "after_chars": len(after),
                        "delta_chars": len(after) - len(before),
                        "before_lines": len(before.splitlines()),
                        "after_lines": len(after.splitlines()),
                    }
                )

        report_rel = ""
        if written:
            report_path = self.project / "03_new_novel" / f"revision_report_{self._stamp()}.md"
            report_rel = str(report_path.relative_to(self.project)).replace("\\", "/")
            lines = [
                "# 微调效果报告",
                "",
                f"- 时间：{dt.datetime.now().isoformat(timespec='seconds')}",
                f"- 微调要求：{note}",
                f"- 更新文件数：{len(changed_files)}",
                "",
                "## 本次实际改动",
                "",
                "| 文件 | 字数变化 | 行数变化 |",
                "| --- | ---: | ---: |",
            ]
            for item in changed_files:
                lines.append(
                    f"| `{item['file']}` | {item['before_chars']} -> {item['after_chars']} "
                    f"({item['delta_chars']:+d}) | {item['before_lines']} -> {item['after_lines']} |"
                )
            lines += [
                "",
                "## 你现在怎么检查",
                "",
                "1. 先看 `03_new_novel/novel_bible.md`：确认主线、卖点和主角路线有没有按要求变化。",
                "2. 再看 `03_new_novel/power_system.md`：确认等级、境界、升级爽点是否清楚。",
                "3. 最后扫 `03_new_novel/memory_rollup.md` 和 `continuity_ledger.md`：确认后续正文会继承这次微调。",
                "4. 如果满意，在控制台点“确认并继续”；不满意，继续输入新的微调要求再点“应用微调”。",
                "",
            ]
            write_text(report_path, "\n".join(lines))
            self._log(f"微调效果报告已生成：{report_rel}")

        self.state.setdefault("revisions", []).append(
            {
                "type": "blueprint",
                "note": note,
                "files": [str(path.relative_to(self.project)) for path in written],
                "changed_files": changed_files,
                "report": report_rel,
                "revised_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
        )
        self.state["phase"] = "blueprint_pending"
        self.state["awaiting"] = {
            "type": "blueprint",
            "target": "03_new_novel/novel_bible.md",
            "message": "微调已应用到融合设定包。请再次检查 03_new_novel，确认后继续写正文。",
        }
        self._save_state()
        if written:
            files = ", ".join(path.name for path in written)
            return f"微调完成，已更新：{files}"
        return "微调完成，但未自动覆盖文件；请查看 revision_raw 文件。"

    def _fusion_policy_block(self) -> str:
        policy = self.config.get("policy") or {}
        policy_text = " ".join(str(value) for value in policy.values())
        licensed = self.options.fusion_mode == "licensed" or "授权复用" in policy_text or "完全融合" in policy_text
        if licensed:
            return (
                "融合方式：完全融合授权复用。用户表示对输入素材拥有相应复用或改编权限；"
                "爽点、故事情节、剧情链、事件顺序、场景媒介、关键转折、人物功能设定、"
                "人物关系、境界体系和设定骨架都可以完整照搬并跨书融合。人物名和专有人名不能照搬，必须改名；"
                "背景、势力名、地名、设定名、金手指名和承接细节按本书统一。源句只做词语级近义替换、称谓替换、"
                "专名替换和少量衔接替换，不改变原意和基本句式，不改成 AI 味解释腔。若旧文件曾把完整故事链列为风险，"
                "应将其重新视为授权复用候选。"
            ) + "\n\n" + SOURCE_BACKBONE_STRICT_BLOCK
        if self.options.fusion_mode == "conservative":
            return (
                "融合方式：保守提炼。提取源书的结构、节奏、人物功能和冲突模型后重新组织，"
                "避免保留具有强识别性的具体组合。"
            ) + "\n\n" + SOURCE_BACKBONE_STRICT_BLOCK
        return (
            "融合方式：强融合（默认）。大纲必须建立在源书素材上，不要为了显得原创而替换掉好用设计。"
            "优先挑选多本源书中优秀的境界体系、能力机制、人物关系、关键剧情模块、阶段高潮和全书走向，"
            "通过跨书拼合形成统一方案；每个关键设计标注来源和保留/拼合/改造方式。"
            "可紧密借鉴设计层内容；未明确授权时，不复制长段原文、标志性台词或单本作品完整独特场景链。"
        ) + "\n\n" + SOURCE_BACKBONE_STRICT_BLOCK

    def _platform_identity_brief(self) -> str:
        return (
            f"平台：{self.profile['display_name']}\n"
            f"市场定位：{self.profile['positioning']}\n"
            f"单章字数参考：{self.profile['chapter_words']}\n"
            "说明：这里的目标平台只用于判断题材市场和章节体量，不作为标题模板、固定文风或固定章纲句式。"
        )

    def _source_reading_brief(self) -> str:
        return (
            f"{self._platform_identity_brief()}\n\n"
            "读取源书时，请把源书本身当作风格和结构证据：抽取标题口味、章节开场方式、场景切入速度、"
            "对白/旁白比例、单章事件颗粒度、章末承接方式和连续数章的小闭环。"
            "只记录有源书依据的观察，不要提前写死后续大纲必须套用的固定规则。"
        )

    def _source_style_profile_text(self, max_chars: int = 9000) -> str:
        from .cli import read_optional

        text = read_optional(self.project / "02_source_analysis" / "source_style_profile.md", max_chars)
        if text.strip():
            return text
        return "（尚未生成 source_style_profile.md；本次只能从 source_bibles、motif_library、plot_pool 等源书素材中临时归纳，不得套固定平台模板。）"

    def _outline_evidence_block(self) -> str:
        return (
            "## 目标平台分类\n\n"
            f"{self._platform_identity_brief()}\n\n"
            "## 源书风格画像\n\n"
            f"{self._source_style_profile_text()}\n\n"
            "## 大纲生成边界\n\n"
            f"{SOURCE_DERIVED_OUTLINE_RULE}"
        )

    def _source_chain_analysis_requirement(self) -> str:
        policy = self.config.get("policy") or {}
        policy_text = " ".join(str(value) for value in policy.values())
        if self.options.fusion_mode == "licensed" or "授权复用" in policy_text or "完全融合" in policy_text:
            return (
                "授权复用剧情链候选：列出可完整沿用和跨书融合的具体故事链、事件顺序、"
                "场景媒介、关键转折、人物功能、人物关系、境界体系和设定骨架；标明必须替换的人物名/专有人名，"
                "以及需要统一的背景、势力名、地名、设定名、金手指名和承接细节。不要输出“不应整链复刻”清单。"
            )
        return "默认按完全融合授权复用整理：列出可完整沿用和跨书融合的具体故事链、事件顺序、人物功能、升级段、境界体系和设定骨架。"

    def _chapter_source_reuse_rule(self) -> str:
        policy = self.config.get("policy") or {}
        policy_text = " ".join(str(value) for value in policy.values())
        if self.options.fusion_mode == "licensed" or "授权复用" in policy_text or "完全融合" in policy_text:
            return (
                "当前为完全融合授权复用模式：正文必须优先照搬并融合选定源书的爽点、故事情节、剧情链、事件顺序、"
                "场景媒介、人物功能设定、人物关系、关键转折、境界体系和设定骨架；人物名和专有人名必须替换，"
                "背景、势力名、地名、设定名和正文表达按当前项目统一。源句只做词语级近义替换、称谓替换、"
                "专名替换和少量衔接替换，不改变原意和基本句式，不改成 AI 味解释腔。"
                "不得改题材、改时代、改职业生态、改核心剧情链；若源书是飞卢或快爽文，必须快压迫、快反杀、快兑现。"
            )
        return "正文必须优先沿用并跨书融合源书素材池中的爽点、故事链、事件顺序、人物功能、升级段和境界骨架；人物名和专有人名必须替换；源句只做词语级近义替换、称谓替换、专名替换和少量衔接替换，不改变原意和基本句式。不得改题材、改时代、改职业生态、改核心剧情链；若源书是飞卢或快爽文，必须快压迫、快反杀、快兑现。"

    def _chapter_source_material(self, plan: dict[str, str], limit: int = 9000) -> str:
        plan_blob = "\n".join(str(value) for value in plan.values() if value)
        source_ids = []
        seen = set()
        for match in re.finditer(
            r"\b(?:MOTIF_GROW_\d+|MOTIF_[A-Z_]+_\d+|PSP2-\d+|P2-\d+|PS\d+|P\d+|M\d+|R\d+|CP\d+|E\d+)\b",
            plan_blob,
        ):
            value = match.group(0)
            if value not in seen:
                seen.add(value)
                source_ids.append(value)
        if not source_ids:
            return "本章细纲未标注素材编号；写作时必须从本章冲突反推源书桥段，不得凭空原创。"

        pools = [
            ("02_source_analysis/plot_pool.csv", "plot_id", "剧情模块"),
            ("02_source_analysis/motif_library.csv", "motif_id", "爽点母题"),
            ("02_source_analysis/power_system_pool.csv", "system_id", "力量体系"),
            ("02_source_analysis/character_pool.csv", "role_id", "人物功能位"),
        ]
        sections: list[str] = []
        for rel_path, id_column, label in pools:
            path = self.project / rel_path
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    for row in csv.DictReader(handle):
                        row_id = (row.get(id_column) or "").strip()
                        if row_id not in seen:
                            continue
                        values = [f"{key}={value}" for key, value in row.items() if value]
                        sections.append(f"### {label} {row_id}\n" + "\n".join(values))
            except Exception as exc:  # noqa: BLE001 - bad source pool should not stop writing.
                sections.append(f"### {label}读取失败\n{rel_path}: {exc}")

        if not sections:
            return "本章细纲标注了素材编号，但素材池未查到对应条目；必须优先按 source_inspiration 的源书桥段写，不得凭空原创。"
        text = "\n\n".join(sections)
        return text[:limit]

    def _story_planning_brief(self) -> str:
        chapter_words = self._average_chapter_words()
        low = max(100, round(2_000_000 / chapter_words / 25) * 25)
        high = max(low, round(3_000_000 / chapter_words / 25) * 25)
        return (
            f"目标总体量约 200-300 万字。按当前平台单章约 {chapter_words} 字估算，"
            f"通常落在约 {low}-{high} 章。请根据融合后的主线、卷数和高潮密度自行确定计划总章数，"
            "并在候选/全书大纲开头明确写出 `计划总章数：N章`。章数不是固定 800，"
            "也不能只规划前 200 章；选定方案后必须能够展开到大结局终章。"
        )

    def decompose_sources(self) -> None:
        from .cli import load_json, safe_name

        index_path = self.project / "01_sources" / "source_index.json"
        if not index_path.exists():
            raise RuntimeError("缺少源书索引，请先运行 ingest 或 start。")
        index = load_json(index_path)
        books = index.get("books", [])
        per_book_dir = self.project / "02_source_analysis" / "per_book"
        chunk_dir = self.project / "02_source_analysis" / "chunk_notes"
        per_book_dir.mkdir(parents=True, exist_ok=True)
        chunk_dir.mkdir(parents=True, exist_ok=True)

        self._log(
            f"源书拆解开始：{len(books)} 本/分组，模式={self.options.source_mode}；"
            "必须全部拆解完成后才会进入融合"
        )
        if (self.state.get("awaiting") or {}).get("type") == "source_failures":
            self.state["awaiting"] = None
            self._save_state()
        max_rounds = max(2, self.options.source_retries + 2)
        for round_no in range(1, max_rounds + 1):
            pending: list[tuple[int, dict[str, Any], str, Path]] = []
            for idx, book in enumerate(books, start=1):
                safe = safe_name(book["title"])
                target = per_book_dir / f"{safe}.md"
                if target.exists() and target.stat().st_size > 200:
                    continue
                pending.append((idx, book, safe, target))

            if not pending:
                break
            workers = max(1, min(self.options.source_workers, len(pending)))
            self._log(
                f"源书拆解轮次 {round_no}/{max_rounds}：待处理 {len(pending)} 本，并发 {workers}，"
                f"单本超时 {self.options.source_timeout} 秒，单次调用重试 {self.options.source_retries} 次"
            )
            if workers == 1:
                for idx, book, safe, target in pending:
                    try:
                        self._decompose_one_source(idx, len(books), book, safe, target, chunk_dir)
                    except Exception as exc:  # noqa: BLE001 - unresolved sources are retried by round.
                        self._record_source_failure(idx, len(books), book, safe, exc)
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                    future_map = {
                        executor.submit(self._decompose_one_source, idx, len(books), book, safe, target, chunk_dir): (
                            idx,
                            book,
                            safe,
                        )
                        for idx, book, safe, target in pending
                    }
                    for future in concurrent.futures.as_completed(future_map):
                        idx, book, safe = future_map[future]
                        try:
                            future.result()
                        except Exception as exc:  # noqa: BLE001 - unresolved sources are retried by round.
                            self._record_source_failure(idx, len(books), book, safe, exc)

        completed_count = len([p for p in per_book_dir.glob("*.md") if p.stat().st_size > 200])
        if completed_count < len(books):
            missing_titles = [
                book["title"]
                for book in books
                if not (per_book_dir / f"{safe_name(book['title'])}.md").exists()
                or (per_book_dir / f"{safe_name(book['title'])}.md").stat().st_size <= 200
            ]
            self.state["phase"] = "new"
            self.state["awaiting"] = {
                "type": "source_failures",
                "target": "02_source_analysis/failed_books",
                "message": (
                    f"仍有 {len(missing_titles)} 本源书拆解失败，已阻止进入融合，避免遗漏素材。"
                    "点击“启动 / 继续”会只重试缺失源书。"
                ),
            }
            self._save_state()
            self._log(
                f"源书拆解未齐：{completed_count}/{len(books)}；不会聚合。"
                f"未完成：{'、'.join(missing_titles[:6])}"
            )
            raise RuntimeError("源书拆解未全部完成，流程已暂停在缺失源书重试阶段。")

        self._log("开始聚合源书素材池")
        self._aggregate_source_analysis(per_book_dir)
        self._log("源书素材池聚合完成")
        self.state["phase"] = "decomposed"
        self._save_state()

    def _decompose_one_source(
        self,
        idx: int,
        total: int,
        book: dict[str, Any],
        safe: str,
        target: Path,
        chunk_dir: Path,
    ) -> None:
        from .cli import read_optional, write_text

        self._log(f"拆解源书 {idx}/{total}：{book['title']}")
        worker_llm = self._source_llm(idx - 1)
        card_path = self.project / "01_sources" / "source_cards" / f"{safe}.md"
        card = read_optional(card_path, 20000)
        chunk_notes = ""
        if self.options.source_mode == "deep":
            chunk_notes = self._deep_decompose_book(book, safe, chunk_dir, worker_llm)

        prompt = self._book_decompose_prompt(book["title"], card, chunk_notes)
        self._log(f"调用模型生成单本拆解：{book['title']}")
        result = self._complete_with_preview(
            prompt,
            llm=worker_llm,
            stream_label=f"源书{idx}/{total}:{book['title'][:18]}",
            max_tokens=self.options.max_tokens_analysis,
            temperature=0.45,
        )
        write_text(target, result.strip() + "\n")
        failure_path = self.project / "02_source_analysis" / "failed_books" / f"{safe}.md"
        if failure_path.exists():
            failure_path.unlink()
        self._log(f"完成单本拆解 {idx}/{total}：{target.name}")

    def _record_source_failure(self, idx: int, total: int, book: dict[str, Any], safe: str, exc: Exception) -> None:
        from .cli import write_text

        fail_dir = self.project / "02_source_analysis" / "failed_books"
        fail_dir.mkdir(parents=True, exist_ok=True)
        detail = str(exc).replace("\n", " ")[:1000]
        write_text(
            fail_dir / f"{safe}.md",
            f"# 源书拆解失败\n\n- 序号：{idx}/{total}\n- 书名：{book['title']}\n- 时间：{dt.datetime.now().isoformat(timespec='seconds')}\n\n```text\n{detail}\n```\n",
        )
        self._log(f"源书拆解失败 {idx}/{total}：{book['title']}；将仅对此书继续重试。{detail[:220]}")

    def _deep_decompose_book(self, book: dict[str, Any], safe: str, chunk_root: Path, llm: LLMClient | None = None) -> str:
        from .cli import read_text, write_text

        out_dir = chunk_root / safe
        out_dir.mkdir(parents=True, exist_ok=True)
        notes: list[str] = []
        chunk_no = 0
        for item in book.get("files", []):
            if chunk_no >= self.options.max_source_chunks_per_book:
                break
            path = Path(item["path"])
            if not path.exists():
                continue
            text = read_text(path)
            for start in range(0, len(text), self.options.chunk_chars):
                if chunk_no >= self.options.max_source_chunks_per_book:
                    break
                chunk = text[start : start + self.options.chunk_chars]
                if len(chunk.strip()) < 300:
                    continue
                chunk_no += 1
                note_path = out_dir / f"chunk_{chunk_no:04d}.md"
                if note_path.exists() and note_path.stat().st_size > 100:
                    self._log(f"复用深读笔记：{book['title']} chunk {chunk_no}")
                    notes.append(read_text(note_path))
                    continue
                self._log(f"深读源书片段：{book['title']} chunk {chunk_no}")
                prompt = f"""# 源书分块拆解

书名/分组：{book['title']}
块号：{chunk_no}

请只做素材拆解，不写新小说。抽取：

- 当前片段发生了什么。
- 可融合剧情模块。
- 可融合爽点结构。
- 人物功能位。
- 金手指/能力/规则信息。
- 冲突模型。
- 节奏和钩子。
- 标题口味、章节开场方式、场景切入速度、对白/旁白比例、章末承接方式；只记录源书现象，不转成固定模板。
- 值得在融合中保留或紧密改造的境界、人物关系、剧情节点和全书走向，并标注具体理由。
- 需要替换的人物名/专有人名、需要统一改写的势力名/地名/设定名，以及可做词语级近义替换的句式样本。

融合原则：
{self._fusion_policy_block()}

源片段：

{chunk}
"""
                note = self._complete_with_preview(
                    prompt,
                    llm=llm,
                    stream_label=f"深读:{book['title'][:16]}:{chunk_no}",
                    max_tokens=self.options.max_tokens_analysis,
                    temperature=0.35,
                )
                write_text(note_path, note.strip() + "\n")
                notes.append(note)
                self._log(f"完成深读片段：{book['title']} chunk {chunk_no}")
        return "\n\n".join(notes[-self.options.max_source_chunks_per_book :])

    def _book_decompose_prompt(self, title: str, card: str, chunk_notes: str) -> str:
        from .cli import platform_block

        return f"""# 单本源书融合拆解

目标平台分类和源书读取原则：

{self._source_reading_brief()}

源书/分组：{title}

## 源书索引卡

{card}

## 深读分块笔记

{chunk_notes or "（当前为 sampled 模式，仅使用索引卡和抽样内容。）"}

## 任务

请输出这本书对新书可用的融合素材。不是从零原创，也不是复述原书。

融合原则：
{self._fusion_policy_block()}

必须包含：

1. 题材定位与核心卖点。
2. 可融合的人设与关系设计，明确哪些部分值得保留或与其他书拼合。
3. 可融合的境界体系、金手指/能力逻辑，保留好用的具体升级结构。
4. 可融合的世界规则或势力模型。
5. 可融合的剧情模块和高潮模型。
6. 可融合的爽点结构和章节节奏。
7. 源书标题口味、开场方式、单章事件颗粒度、对白/旁白比例、章末承接方式；只写观察和证据，不写固定模板。
8. 原书中值得继承的故事走向/阶段路线，以及可参与跨书组合的位置。
9. 适合目标平台的统一调整建议。
10. {self._source_chain_analysis_requirement()}
"""

    def _aggregate_source_analysis(self, per_book_dir: Path) -> None:
        from .cli import platform_block

        context, count = self._source_analysis_context(per_book_dir)
        if not context:
            raise RuntimeError("没有可用于聚合的单本拆解结果。")

        for index, (rel_path, title, requirements) in enumerate(SOURCE_AGGREGATE_FILE_SPECS, start=1):
            if self._single_file_ready(rel_path):
                self._log(f"素材池聚合 {index}/{len(SOURCE_AGGREGATE_FILE_SPECS)}：跳过已生成 {Path(rel_path).name}")
                continue
            self._log(f"素材池聚合 {index}/{len(SOURCE_AGGREGATE_FILE_SPECS)}：生成 {Path(rel_path).name}")
            if rel_path in SOURCE_AGGREGATE_CSV_PARTS:
                path = self._generate_aggregate_csv_parts(rel_path, title, requirements, context, count)
                self._log(f"素材池文件已写入 {index}/{len(SOURCE_AGGREGATE_FILE_SPECS)}：{path.name}")
                continue
            prompt = f"""# 源书素材池聚合：{title}

目标平台分类和源书读取原则：

{self._source_reading_brief()}

下面是 {count} 本/组源书的逐本拆解摘要。请只生成一个目标文件：`{rel_path}`。

输出要求：
{requirements}

写作边界：
- {self._fusion_policy_block()}
- 这一步是构建可直接供大纲调用的融合素材池，不是复述原文，也不是从零发明替代方案。
- 记录具体可复用设计及来源书名；素材池应能看出“取了哪本书的什么好设计，准备与哪本书组合”。
- 如果当前目标文件是源书风格画像，只归纳源书证据，不要写固定标题模板、固定文风规则或后续必须照抄的句式。
- 只输出目标文件正文；不要解释。若你使用 FILE_BUNDLE，只能包含 `path="{rel_path}"` 这一个文件块。

逐本拆解摘要：

{context}
"""
            path = self._generate_single_file_with_retries(
                rel_path,
                prompt,
                stream_label=f"素材池:{Path(rel_path).name}",
                max_tokens=self.options.max_tokens_blueprint,
                temperature=0.45,
            )
            self._log(f"素材池文件已写入 {index}/{len(SOURCE_AGGREGATE_FILE_SPECS)}：{path.name}")

        missing = [rel for rel in SOURCE_AGGREGATE_OUTPUT_FILES if not self._single_file_ready(rel)]
        if missing:
            self._log(f"素材池聚合不完整，缺少 {len(missing)} 个文件；不会继续生成设定包：{', '.join(missing[:5])}")
            self.state["phase"] = "new"
            self.state["awaiting"] = None
            self._save_state()
            raise RuntimeError("素材池聚合不完整，请重新运行“启动 / 继续”。")

    def _generate_aggregate_csv_parts(
        self,
        rel_path: str,
        title: str,
        requirements: str,
        context: str,
        source_count: int,
    ) -> Path:
        from .cli import platform_block

        focuses = SOURCE_AGGREGATE_CSV_PARTS[rel_path]
        stem = Path(rel_path).stem
        parts: list[Path] = []
        for part_no, focus in enumerate(focuses, start=1):
            part_rel = f"02_source_analysis/aggregate_parts/{stem}_part_{part_no}.csv"
            if self._single_file_ready(part_rel, min_size=200):
                self._log(f"{Path(rel_path).name} 分块 {part_no}/{len(focuses)}：跳过已生成")
                parts.append(self.project / part_rel)
                continue
            prompt = f"""# 源书素材池分块聚合：{title} - 分块 {part_no}/{len(focuses)}

目标平台分类和源书读取原则：

{self._source_reading_brief()}

最终目标文件：`{rel_path}`
当前临时分块文件：`{part_rel}`
当前分块只负责：{focus}

最终文件格式要求：
{requirements}

融合原则：
- {self._fusion_policy_block()}
- 当前只生成这一类素材，避免和其他分块重复。
- 输出 12-24 条高价值素材；宁可挑精华，不要为了数量写低价值重复项。
- 第一行必须是最终目标 CSV 的完整表头；只输出当前 CSV 内容，不要解释。若使用 FILE_BUNDLE 外壳，必须包含完整闭合标签。

下面是 {source_count} 本/组源书的逐本拆解摘要：

{context}
"""
            self._log(f"{Path(rel_path).name} 分块调用模型：{part_no}/{len(focuses)} - {focus}")
            path = self._generate_single_file_with_retries(
                part_rel,
                prompt,
                stream_label=f"素材池:{stem}:分块{part_no}",
                max_tokens=self.options.max_tokens_blueprint,
                temperature=0.42,
            )
            parts.append(path)
            self._log(f"{Path(rel_path).name} 分块完成：{part_no}/{len(focuses)}")

        merged = self._merge_aggregate_csv_parts(rel_path, parts)
        target = self._write_single_file_output(rel_path, merged)
        self._log(f"{Path(rel_path).name} 已合并 {len(parts)} 个分块")
        return target

    def _merge_aggregate_csv_parts(self, rel_path: str, parts: list[Path]) -> str:
        from .cli import read_text

        expected_header = self._expected_csv_header(rel_path)
        if not expected_header:
            raise RuntimeError(f"没有找到 CSV 表头配置：{rel_path}")
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        header = next(csv.reader([expected_header]))
        writer.writerow(header)
        seen: set[tuple[str, ...]] = set()
        for path in parts:
            text = self._normalize_csv_body(rel_path, read_text(path))
            reader = csv.reader(io.StringIO(text))
            rows = list(reader)
            if not rows or rows[0] != header:
                raise OutputQualityError(rel_path, f"分块表头不正确：{path.name}", path)
            for row in rows[1:]:
                if len(row) != len(header) or not any(cell.strip() for cell in row):
                    continue
                key = tuple(cell.strip() for cell in row)
                if key in seen:
                    continue
                seen.add(key)
                writer.writerow(row)
        return output.getvalue()

    def build_blueprint(self) -> None:
        from .cli import load_json, platform_block, safe_name

        index_path = self.project / "01_sources" / "source_index.json"
        if index_path.exists():
            books = load_json(index_path).get("books", [])
            per_book_dir = self.project / "02_source_analysis" / "per_book"
            missing = [
                book["title"]
                for book in books
                if not (per_book_dir / f"{safe_name(book['title'])}.md").exists()
                or (per_book_dir / f"{safe_name(book['title'])}.md").stat().st_size <= 200
            ]
            if missing:
                self.state["phase"] = "new"
                self.state["awaiting"] = {
                    "type": "source_failures",
                    "target": "02_source_analysis/failed_books",
                    "message": (
                        f"发现仍有 {len(missing)} 本源书未完成拆解，已阻止继续生成大纲。"
                        "点击“启动 / 继续”会补拆缺失源书。"
                    ),
                }
                self._log(f"生成大纲前检查失败：仍缺 {len(missing)} 本源书拆解，不会进入融合")
                self._save_state()
                raise RuntimeError("源书未拆解完整，不能生成融合大纲；请重新运行以补拆缺失源书。")

        if self.options.stop_at_checkpoints and not self.state.get("selected_outline_candidate"):
            self.generate_outline_candidates()
            self.state["phase"] = "outline_pending"
            self.state["awaiting"] = {
                "type": "outline_selection",
                "target": "03_new_novel/outline_candidates",
                "message": "三套候选大纲已生成。请查看候选大纲，选择一套后再继续生成完整设定包。",
            }
            self._save_state()
            return

        self._log("开始生成融合设定包、卷纲和章节细纲")
        context = self._blueprint_source_context()
        target_chapters = self._target_chapter_count()
        scope_note = self._story_planning_brief()
        for index, (rel_path, title, requirements) in enumerate(BLUEPRINT_FILE_SPECS, start=1):
            if self._single_file_ready(rel_path):
                self._log(f"融合设定包 {index}/{len(BLUEPRINT_FILE_SPECS)}：跳过已生成 {Path(rel_path).name}")
                continue
            self._log(f"融合设定包 {index}/{len(BLUEPRINT_FILE_SPECS)}：生成 {Path(rel_path).name}")
            if rel_path == "03_new_novel/chapter_plan.csv":
                path = self._generate_chapter_plan_file(context, target_chapters)
                self._log(f"融合设定文件已写入 {index}/{len(BLUEPRINT_FILE_SPECS)}：{path.name}")
                continue
            prompt = f"""# 融合设定包生成：{title}

目标平台：{self.profile['display_name']}
全书体量：{scope_note}

## 大纲依据

{self._outline_evidence_block()}

## 素材池摘要

{context}

## 当前目标文件

只生成一个文件：`{rel_path}`

输出要求：
{requirements}

统一创作边界：
- {self._fusion_policy_block()}
- 这是源书强融合大纲，不是从零原创大纲，也不是原文摘要；关键设定必须有素材池依据。
- 必须先锁定主源书锚点：题材、时代、背景、职业生态、主角身份、金手指、主线事件顺序、爽点密度、升级频率和打脸节奏；这些锚点不得被改写。
- 优先保留并跨书重组源书中效果好的能力/等级规则、人物关系、剧情模块和故事走向，而不是主动替换为无来源的新设定；源书没有玄幻境界时不得硬造境界体系。
- `fusion_traceability.md` 必须让你能检查每项主要设计究竟借鉴、拼合了哪些源书。
- 全书设计必须能一路写到大结局；`full_story_outline.md` 要根据节奏自行确定计划总章数，并覆盖第 001 章到最后一章。
- `chapter_plan.csv` 必须生成第 001 章到大纲计划终章的完整逐章细纲，后续正文会按它执行。
- 只输出目标文件正文；不要解释。若你使用 FILE_BUNDLE，只能包含 `path="{rel_path}"` 这一个文件块。
"""
            path = self._generate_single_file_with_retries(
                rel_path,
                prompt,
                stream_label=f"设定:{Path(rel_path).name}",
                max_tokens=self.options.max_tokens_blueprint,
                temperature=0.65,
            )
            self._log(f"融合设定文件已写入 {index}/{len(BLUEPRINT_FILE_SPECS)}：{path.name}")

        missing = [rel for rel in BLUEPRINT_OUTPUT_FILES if not self._single_file_ready(rel)]
        if missing:
            self._log(f"融合设定包不完整，缺少 {len(missing)} 个文件；不会进入确认节点：{', '.join(missing[:5])}")
            self.state["phase"] = "decomposed"
            self.state["awaiting"] = None
            self._save_state()
            raise RuntimeError("融合设定包生成不完整，请重新运行“启动 / 继续”。")
        self.state["phase"] = "blueprint_pending"
        self.state["awaiting"] = {
            "type": "blueprint",
            "target": "03_new_novel/novel_bible.md",
            "message": "融合设定包和章节规划已生成。请检查/微调 03_new_novel，确认后继续写正文。",
        }
        self._save_state()

    def _generate_chapter_plan_file(self, context: str, target_chapters: int | str | None) -> Path:
        from .cli import platform_block, read_optional, read_text, write_text

        target_total = self._target_chapter_count(target_chapters)
        scope_note = self._story_scope_note(target_total)
        parts_dir = self.project / "03_new_novel" / "chapter_plan_parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        outline_context = "\n\n".join(
            [
                "## revision_directive.md\n" + read_optional(self.project / "03_new_novel" / "revision_directive.md", 12000),
                "## novel_bible.md\n" + read_optional(self.project / "03_new_novel" / "novel_bible.md", 9000),
                "## power_system.md\n" + read_optional(self.project / "03_new_novel" / "power_system.md", 9000),
                "## worldbuilding.md\n" + read_optional(self.project / "03_new_novel" / "worldbuilding.md", 9000),
                "## character_table.csv\n" + read_optional(self.project / "03_new_novel" / "character_table.csv", 9000),
                "## volume_outline.md\n" + read_optional(self.project / "03_new_novel" / "volume_outline.md", 12000),
                "## full_story_outline.md\n" + read_optional(self.project / "03_new_novel" / "full_story_outline.md", 14000),
                "## fusion_traceability.md\n" + read_optional(self.project / "03_new_novel" / "fusion_traceability.md", 10000),
            ]
        )
        part_paths: list[Path] = []
        covered: set[int] = set()
        for path in sorted(parts_dir.glob("ch_*.csv")):
            if "_raw_" in path.name:
                continue
            numbers = set(self._chapter_numbers_from_csv(read_text(path)))
            numbers = {number for number in numbers if 1 <= number <= target_total}
            if not numbers:
                continue
            part_paths.append(path)
            covered.update(numbers)
        if covered:
            self._log(
                f"章节规划复用已有有效行：已覆盖 {len(covered)}/{target_total} 章，"
                f"当前连续到第 {self._covered_prefix_end(covered):03d} 章"
            )

        ranges = self._chapter_plan_missing_ranges(covered, 1, target_total, CHAPTER_PLAN_CHUNK_SIZE)
        while ranges:
            start, end = ranges.pop(0)
            rel_path = f"03_new_novel/chapter_plan_parts/ch_{start:03d}_{end:03d}.csv"
            if self._single_file_ready(rel_path):
                self._log(f"章节规划分段 {start:03d}-{end:03d}：跳过已生成")
                path = self.project / rel_path
                if path not in part_paths:
                    part_paths.append(path)
                covered.update(self._chapter_numbers_from_csv(read_text(path)))
                continue
            previous_tail = self._chapter_plan_tail_before(part_paths, start)
            prompt = f"""# 分段生成章节规划：第 {start:03d}-{end:03d} 章

目标平台：{self.profile['display_name']}
全书体量：{scope_note}

## 大纲依据

{self._outline_evidence_block()}

## 全局素材池摘要

{context}

## 当前新书设定

{outline_context}

## 上一段章节规划末尾

{previous_tail or "（这是第一段。）"}

## 当前目标

只生成一个 CSV 文件：`{rel_path}`

必须输出第 {start:03d}-{end:03d} 章，共 {end - start + 1} 行章节数据，不能少章、不能跳章。
第一行必须严格是：
{CHAPTER_PLAN_HEADER}

要求：
{DETAILED_CHAPTER_PLAN_RULE}

{ANTI_AI_OUTLINE_RULE}

- 只写 CSV，不要解释，不要 Markdown 列表。
- 每章必须一行，chapter_no 至少三位数字，例如 001；第 1000 章起自然写四位数字。
- title 字段只是内部工作标题，用来帮助定位本章事件；可以参考源书风格画像里的目录口味，但不要预设固定字数、固定句式或固定爆款标题模板，也不要沿用旧项目专名。正文阶段会根据样书口味和正文内容重新起最终标题。
- 章节规划必须按主源书原有事件顺序、爽点密度、升级频率、打脸节奏和场景媒介推进；不得把现代都市改成古代/玄幻/仙侠，不得把原职业生态改成宗门王朝，不得把源书主线换成新故事。
- 升级、打脸、反杀、金手指反馈和收益兑现要跟随源书快爽节奏；密度和强度不得低于源书，必要时可以更快、更大、更爽。不要自己加长压抑铺垫，不要把简单爽点写成苦大仇深或沉重宿命。
- 每行必须包含主冲突、小钩子、能力使用、人物变化、伏笔、章末钩子和主线推进。
- 章纲必须是“可直接写正文的具体情节”，不能只写方向、主题或功能。每章至少写清：
  1) 谁在什么地点触发了什么事；
  2) 主角本章具体做了什么动作/决策；
  3) 反派或阻碍具体怎么压过来；
  4) 本章爽点如何兑现，主角拿到什么结果、证据、资源、地位或线索；
  5) 章末钩子下一章具体接哪件事。
- `core_conflict` 必须写成一条可执行事件链，建议 50-120 字，例如“某人拿某份文书/证据当众压主角，主角先用某规则拆出漏洞，再逼某人交出某物”。不要写“主角面对更大危机、继续推进主线、反派压迫升级”这类空话。
- `small_hook` 要写本章开局能立刻入戏的触发点；`ending_hook` 要写章末具体画面或新动作；`mainline_progress` 写章后局面：谁拿到什么、谁失去什么、下一章为什么必须接上，不要写“本章完成/推进某某线”。
- `power_usage` 不要写成“作用是/限制是/主要靠”的说明句。写可见事实：能力/资源/证据有没有出场，出场时压住哪个眼前问题，没有出场时用哪件物证、哪通电话、哪笔账、哪次现场动作解决问题。
- 字段写法不是关键词任务，不要为了通过检查硬塞固定词。请像给正文作者交接剧情一样自然描述：`ending_hook` 写章末读者能看到的具体画面、人物动作、物件变化、通知/命令/来人/新发现；`mainline_progress` 写本章已经造成的实际变化，以及下一章为什么必须接着写。
- 每一行章纲都必须能让正文作者不再猜剧情：至少包含“触发事件 -> 阻碍/对手 -> 主角反制 -> 本章结果 -> 章末承接”五段信息。不要只写“调查旧案、进入新地图、压迫升级、主角成长、反派登场”。
- `core_conflict` 里必须出现具体人物/势力/地点/物证/道具中的至少两类，并写清它们怎么推动本章事件；如果只是一句抽象剧情功能，必须重写。
- 连续 10 章要组成小闭环：第 1-3 章抛出问题，第 4-7 章拆局反制，第 8-10 章兑现阶段爽点并留下下一段钩子；不能每章孤立写成同一种“遭遇压迫再反击”。
- `source_inspiration` 写素材池编号、源书简称或“源书A+源书B”的融合标签，便于追溯借鉴来源；不要粘贴原文。
- status 统一写 done。
- 第 {end:03d} 章必须能自然衔接下一段。
{"- 这是全书最后一段，必须写出最终战、终局收束和大结局落点。" if end == target_total else ""}
"""
            self._log(f"章节规划分段调用模型：第 {start:03d}-{end:03d} 章")
            try:
                path = self._generate_single_file_with_retries(
                    rel_path,
                    prompt,
                    stream_label=f"章节规划{start:03d}-{end:03d}",
                    max_tokens=self.options.max_tokens_blueprint,
                    temperature=0.55,
                )
            except OutputQualityError as exc:
                if start >= end:
                    raise
                midpoint = (start + end) // 2
                self._log(
                    f"章节规划分段第 {start:03d}-{end:03d} 章连续格式异常：{exc.reason}；"
                    f"自动缩小为 {start:03d}-{midpoint:03d} 与 {midpoint + 1:03d}-{end:03d} 重试"
                )
                ranges = [(start, midpoint), (midpoint + 1, end)] + ranges
                continue
            returned = set(self._chapter_numbers_from_csv(read_text(path))) & set(range(start, end + 1))
            if not returned:
                raise OutputQualityError(rel_path, f"章节分段没有返回第 {start:03d}-{end:03d} 章中的有效数据行", path)
            if path not in part_paths:
                part_paths.append(path)
            covered.update(returned)
            missing_in_segment = set(range(start, end + 1)) - covered
            if missing_in_segment:
                self._log(
                    f"章节规划分段仅完成 {len(returned)}/{end - start + 1} 行："
                    f"第 {start:03d}-{end:03d} 章；将自动拆小补齐缺失章节"
                )
                ranges = (
                    self._chapter_plan_missing_ranges(covered, start, end, max(3, CHAPTER_PLAN_CHUNK_SIZE // 2))
                    + ranges
                )
            else:
                self._log(f"章节规划分段完成：第 {start:03d}-{end:03d} 章")

        merged = self._merge_chapter_plan_parts(part_paths, target_total)
        target = self.project / "03_new_novel" / "chapter_plan.csv"
        write_text(target, merged)
        body = self._normalize_csv_body("03_new_novel/chapter_plan.csv", merged)
        if not self._chapter_plan_complete(body, target_total):
            numbers = self._chapter_numbers_from_csv(body)
            max_no = max(numbers) if numbers else 0
            issue = f"章节规划不完整：当前到第 {max_no:03d} 章，目标第 {target_total:03d} 章"
        else:
            issue = self._chapter_plan_detail_issue(self._valid_chapter_rows(body))
        if issue:
            raise OutputQualityError("03_new_novel/chapter_plan.csv", issue, target)
        return target

    def _target_chapter_count(self, value: int | str | None = None) -> int:
        if value is not None and str(value).strip():
            try:
                explicit = int(value)
            except (TypeError, ValueError):
                explicit = 0
            if explicit > 0:
                return explicit
        planned = self._planned_chapter_count_from_outline()
        if planned:
            return planned
        # Before an outline selects its ending, use the word-count target only as
        # a progress estimate. The model must decide the actual terminal chapter.
        return self._estimate_chapters_from_word_goal()

    def _planned_chapter_count_from_outline(self) -> int | None:
        from .cli import read_optional

        text = "\n\n".join(
            [
                read_optional(self.project / "03_new_novel" / "full_story_outline.md", 80000),
                read_optional(self.project / "03_new_novel" / "volume_outline.md", 80000),
            ]
        )
        if "状态：待生成" in text:
            text = text.replace("状态：待生成", "")
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
        return max(plausible) if plausible else None

    def _estimate_chapters_from_word_goal(self) -> int:
        chapter_words = self._average_chapter_words()
        target_words = 2_500_000
        estimate = max(100, round(target_words / chapter_words))
        return int(round(estimate / 25) * 25)

    def _average_chapter_words(self) -> int:
        raw = str(self.profile.get("chapter_words") or "2500")
        values = [int(value) for value in re.findall(r"\d+", raw)]
        if not values:
            return 2500
        if len(values) == 1:
            return max(1000, values[0])
        return max(1000, round(sum(values[:2]) / 2))

    def _story_scope_note(self, planned_chapters: int) -> str:
        chapter_words = self._average_chapter_words()
        low = round(planned_chapters * chapter_words * 0.85 / 10000, 1)
        high = round(planned_chapters * chapter_words * 1.15 / 10000, 1)
        return (
            f"目标约 200-300 万字；当前按大纲节奏规划到第 {planned_chapters:03d} 章，"
            f"按平台单章约 {chapter_words} 字估算约 {low}-{high} 万字。"
        )

    def _chapter_plan_missing_ranges(
        self,
        covered: set[int],
        start: int,
        end: int,
        chunk_size: int = CHAPTER_PLAN_CHUNK_SIZE,
    ) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        current = start
        while current <= end:
            if current in covered:
                current += 1
                continue
            segment_start = current
            segment_end = current
            while (
                segment_end + 1 <= end
                and segment_end + 1 not in covered
                and segment_end - segment_start + 1 < chunk_size
            ):
                segment_end += 1
            ranges.append((segment_start, segment_end))
            current = segment_end + 1
        return ranges

    def _covered_prefix_end(self, covered: set[int]) -> int:
        current = 0
        while current + 1 in covered:
            current += 1
        return current

    def _chapter_plan_tail_before(self, paths: list[Path], start: int, lines: int = 5) -> str:
        from .cli import read_text

        rows: dict[int, list[str]] = {}
        for path in paths:
            for number, row in self._valid_chapter_rows(read_text(path)).items():
                if number < start:
                    rows[number] = row
        selected = [rows[number] for number in sorted(rows)[-lines:]]
        if not selected:
            return ""
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerows(selected)
        return output.getvalue().strip()

    def _chapter_plan_tail(self, path: Path, lines: int = 5) -> str:
        from .cli import read_text

        rows = self._valid_chapter_rows(read_text(path))
        selected = [rows[number] for number in sorted(rows)[-lines:]]
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerows(selected)
        return output.getvalue().strip()

    def _validate_chapter_part(self, path: Path, start: int, end: int) -> None:
        from .cli import read_text

        text = self._normalize_csv_body("03_new_novel/chapter_plan.csv", read_text(path))
        rows = [line for line in text.splitlines() if line.strip()]
        numbers = self._chapter_numbers_from_csv(text)
        expected = set(range(start, end + 1))
        if not rows or rows[0] != CHAPTER_PLAN_HEADER:
            raise OutputQualityError(str(path.relative_to(self.project)).replace("\\", "/"), "章节分段 CSV 表头不正确", path)
        if not expected.issubset(set(numbers)):
            missing = sorted(expected - set(numbers))
            raise OutputQualityError(
                str(path.relative_to(self.project)).replace("\\", "/"),
                f"章节分段缺少章节：{missing[:8]}",
                path,
            )
        detail_issue = self._chapter_plan_detail_issue({no: row for no, row in self._valid_chapter_rows(text).items() if start <= no <= end})
        if detail_issue:
            raise OutputQualityError(str(path.relative_to(self.project)).replace("\\", "/"), detail_issue, path)

    def _merge_chapter_plan_parts(self, part_paths: list[Path], target_total: int) -> str:
        from .cli import read_text

        rows_by_no: dict[int, list[str]] = {}
        for path in part_paths:
            for number, row in self._valid_chapter_rows(read_text(path)).items():
                if 1 <= number <= target_total:
                    rows_by_no[number] = row
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(next(csv.reader([CHAPTER_PLAN_HEADER])))
        writer.writerows(rows_by_no[number] for number in range(1, target_total + 1) if number in rows_by_no)
        return output.getvalue()

    def _chapter_numbers_from_csv(self, text: str) -> list[int]:
        return sorted(self._valid_chapter_rows(text))

    def _valid_chapter_rows(self, text: str) -> dict[int, list[str]]:
        body = self._normalize_csv_body("03_new_novel/chapter_plan.csv", text)
        expected_header = next(csv.reader([CHAPTER_PLAN_HEADER]))
        reader = csv.reader(io.StringIO(body))
        try:
            header = next(reader)
        except StopIteration:
            return {}
        if header != expected_header:
            return {}
        rows: dict[int, list[str]] = {}
        for raw_row in reader:
            row = self._repair_chapter_row(raw_row, len(expected_header))
            if not row:
                continue
            raw_no = row[0].strip()
            if not re.fullmatch(r"\d{1,4}", raw_no):
                continue
            number = int(raw_no)
            if not all(cell.strip() for cell in row[1:]):
                continue
            rows[number] = row
        return rows

    def _chapter_plan_detail_issue(self, rows: dict[int, list[str]]) -> str:
        if not rows:
            return ""

        vague_phrases = (
            "推进主线",
            "推动剧情",
            "制造悬念",
            "反派压迫升级",
            "进一步成长",
            "关系变化",
            "铺垫后续",
            "承接上文",
            "展开冲突",
            "爽点升级",
            "引出后续",
            "进入新阶段",
            "进入新地图",
            "完成过渡",
            "形成铺垫",
            "继续追查",
            "情绪递进",
            "命运交锋",
            "格局打开",
            "暗流涌动",
            "压迫升级",
            "世界观展开",
            "人物弧光",
            "完成铺垫",
            "形成闭环",
            "阶段高潮",
            "铺开格局",
            "新敌人登场",
        )
        generic_placeholders = (
            "某人",
            "某份",
            "某个",
            "某处",
            "某物",
            "某势力",
            "某组织",
            "某敌人",
            "某线索",
            "某任务",
            "某资源",
            "某规则",
            "相关线索",
            "关键线索",
            "重要人物",
            "神秘人物",
            "更大危机",
            "新的危机",
        )
        report_tone_phrases = (
            "本章完成",
            "本章主要",
            "本章延续",
            "本章抛出",
            "本章无",
            "主要靠",
            "作用是",
            "限制是",
            "正式进入",
            "正式开启",
            "进一步",
            "开始意识到",
            "心态从",
            "变成意识到",
            "身份升级",
            "信息投放",
            "任务落地",
            "读者看到",
            "用于",
        )
        for number, row in sorted(rows.items()):
            if len(row) < 12:
                return f"第 {number:03d} 章字段不足，不能作为正文细纲"
            core = row[3].strip()
            small_hook = row[4].strip()
            power = row[5].strip()
            ending = row[8].strip()
            progress = row[9].strip()
            for field_name, value, min_len in (
                ("core_conflict", core, 55),
                ("small_hook", small_hook, 14),
                ("power_usage", power, 18),
                ("ending_hook", ending, 16),
                ("mainline_progress", progress, 30),
            ):
                if len(value) < min_len:
                    return f"第 {number:03d} 章 {field_name} 过短，缺少可写正文的具体情节"
            joined = "；".join((core, small_hook, power, ending, progress))
            if any(phrase in joined for phrase in vague_phrases) and len(core) < 75:
                return f"第 {number:03d} 章章纲偏空泛，不能只写方向，需要补具体事件、动作和结果"
            if any(token in joined for token in generic_placeholders):
                return f"第 {number:03d} 章章纲仍有泛称占位词，需要写成具体人物、地点、证据、道具或事件"
            report_hits = [phrase for phrase in report_tone_phrases if phrase in joined]
            if len(report_hits) >= 2:
                return f"第 {number:03d} 章章纲像报告说明，不要写“{report_hits[0]}”这类功能说明；改成看得见的事件、动作和结果"
        return ""

    def _repair_chapter_row(self, row: list[str], expected_len: int) -> list[str] | None:
        if len(row) == expected_len:
            return row
        if len(row) < expected_len or expected_len != 12:
            return None
        if self._looks_like_source_inspiration(row[-3]) and self._looks_like_source_inspiration(row[-2]):
            repaired = row[:10] + ["；".join(cell.strip() for cell in row[10:-1] if cell.strip())] + [row[-1]]
        else:
            overflow = len(row) - expected_len
            repaired = row[:4] + ["；".join(cell.strip() for cell in row[4 : 5 + overflow] if cell.strip())] + row[5 + overflow :]
        return repaired if len(repaired) == expected_len else None

    def _looks_like_source_inspiration(self, value: str) -> bool:
        return bool("《" in value or re.search(r"(?:^|[+；\s])(?:M|P|R|PS)\d", value, re.IGNORECASE))

    def generate_outline_candidates(self) -> None:
        from .cli import platform_block

        context = self._blueprint_source_context()
        planning_brief = self._story_planning_brief()
        self._log("开始生成三套候选大纲：完成后会暂停等待你选择")
        for index, title, angle in OUTLINE_VARIANT_SPECS:
            rel_path = f"03_new_novel/outline_candidates/outline_{index}.md"
            if self._single_file_ready(rel_path):
                self._log(f"候选大纲 {index}/3：跳过已生成 outline_{index}.md")
                continue
            prompt = f"""# 候选大纲生成：方案 {index} - {title}

目标平台：{self.profile['display_name']}
全书体量规则：{planning_brief}

## 大纲依据

{self._outline_evidence_block()}

## 本方案侧重点

{angle}

## 源书融合素材池

{context}

## 源书主干复刻硬规则

{SOURCE_BACKBONE_STRICT_BLOCK}

## 输出要求

只生成一个文件：`{rel_path}`

文件必须包含：

1. 方案名称和一句话卖点。
2. 新书核心钩子、主角目标、金手指/能力主规则。
3. 主角起点、主要人物关系、反派压迫链。
4. 8-12 卷全书卷纲，每卷用“起因 -> 阻碍 -> 主角动作 -> 结果 -> 下一卷承接”的事件链写，不写抽象阶段报告。
5. 必须写到最终卷和大结局，说明终局敌人、最终战、主角完成什么蜕变。
6. 源书融合映射：境界体系、能力规则、人物关系、各卷关键剧情和最终走向分别来自哪些源书元素，标明保留/拼合/改造。
7. 源书风格画像应用说明：只说明本方案的标题口味、卷级节奏和章级颗粒度来自哪些源书观察；不要写成固定文风规则。
8. 用 5 条以内说明本方案优点、风险和适合怎么微调。

边界：
- 这是候选大纲，不写正文，不生成 `chapter_plan.csv`。
- {self._fusion_policy_block()}
- 三套方案不是三套新故事，只能是同一主源书主干的不同复刻强化版本；必须保留主源书题材、时代、背景、职业生态、主角身份、金手指逻辑、事件顺序、爽点密度、升级频率和打脸节奏。所有方案的爽点密度、升级速度、打脸频率、金手指强度和收益兑现不得低于源书，允许更快更大更爽。
- 多源融合时，其他源书只补同功能位桥段，不得推翻主源书主线；单本源书时，只能做改名、轻微承接和节奏强化，不得自由原创新大纲。
- 只输出目标文件正文；不要解释。若你使用 FILE_BUNDLE，只能包含 `path="{rel_path}"` 这一个文件块。
"""
            self._log(f"候选大纲 {index}/3 调用模型：{title}")
            path = self._generate_single_file_with_retries(
                rel_path,
                prompt,
                stream_label=f"候选大纲{index}",
                max_tokens=self.options.max_tokens_blueprint,
                temperature=0.72,
            )
            self._log(f"候选大纲已写入 {index}/3：{path.name}")

    def select_outline_candidate(self, candidate: int) -> str:
        from .cli import read_text

        if candidate not in {1, 2, 3}:
            return "候选大纲编号只能是 1、2 或 3。"
        rel_path = f"03_new_novel/outline_candidates/outline_{candidate}.md"
        path = self.project / rel_path
        if not path.exists() or path.stat().st_size < 200:
            return f"候选大纲 {candidate} 还没有生成完整，请先运行“启动 / 继续”。"
        text = read_text(path)
        if "状态：待生成" in text:
            return f"候选大纲 {candidate} 仍是占位内容，请先运行“启动 / 继续”。"
        self.state["selected_outline_candidate"] = candidate
        self.state["selected_outline_path"] = rel_path
        self.state["phase"] = "decomposed"
        self.state["awaiting"] = None
        self.state.setdefault("approvals", []).append(
            {
                "type": "outline_selection",
                "candidate": candidate,
                "note": f"选择候选大纲 {candidate}",
                "approved_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
        )
        self._log(f"已选择候选大纲 {candidate}，下一次启动将扩展成完整融合设定包")
        self._save_state()
        return f"已选择候选大纲 {candidate}。现在点“启动 / 继续”，Agent 会基于这套方案生成完整设定包。"

    def write_until_limit(self) -> None:
        current = int(self.state.get("current_chapter") or 1)
        requested_max = int(self.options.max_chapters or 0) or self._target_chapter_count()
        available_plan = self._available_chapter_plan_prefix()
        if current > requested_max:
            self._log(f"当前章节 {current} 已超过本轮目标 {requested_max}")
            self.state["phase"] = "completed"
            self.state["awaiting"] = None
            self._save_state()
            return
        if available_plan < current:
            message = (
                f"当前已生成章纲只连续到第 {available_plan:04d} 章，"
                f"正式正文下一章是第 {current:04d} 章；请先继续生成后续章纲。"
            )
            self._log(message)
            self.state["phase"] = "writing"
            self.state["awaiting"] = {
                "type": "chapter_plan_needed",
                "target": "03_new_novel/chapter_plan_parts",
                "message": message,
            }
            self._save_state()
            return

        max_chapter = min(requested_max, available_plan)

        self._log(f"正文写作开始：从第 {current:04d} 章写到第 {max_chapter:04d} 章")
        while current <= max_chapter:
            end = min(current + self.options.batch_size - 1, max_chapter)
            self._log(f"开始正文批次：第 {current:04d}-{end:04d} 章")
            self.write_batch(current, end)
            self.state["current_chapter"] = end + 1
            self.state["phase"] = "writing"

            self.summarize_batch(current, end)
            checkpoint_type = "batch"
            message = f"第 {current:04d}-{end:04d} 章已生成并归档，请检查正文。"

            if self.options.review_every and end % self.options.review_every == 0:
                self._log(f"开始审稿当前批次：第 {current:04d}-{end:04d} 章")
                self.review_batch(current, end)
                checkpoint_type = "review"
                message = f"第 {current:04d}-{end:04d} 章审稿已完成，请检查审稿意见和正文。"

            if self.options.archive_every and end % self.options.archive_every == 0:
                self._log(f"开始阶段归档：第 {max(1, end - self.options.archive_every + 1):04d}-{end:04d} 章")
                self.archive_stage(max(1, end - self.options.archive_every + 1), end)
                checkpoint_type = "archive"
                message = f"第 {max(1, end - self.options.archive_every + 1):04d}-{end:04d} 章阶段归档已完成，请检查长期记忆。"

            if self.options.stop_at_checkpoints:
                self._log(message)
                self.state["awaiting"] = {
                    "type": checkpoint_type,
                    "target": f"05_drafts/ch_{current:04d}_to_{end:04d}.md",
                    "message": message,
                }
                self._save_state()
                return

            current = end + 1

        if max_chapter < requested_max:
            message = (
                f"正文已写到当前可用章纲末尾第 {max_chapter:04d} 章；"
                f"后续目标到第 {requested_max:04d} 章，请继续补章纲后再写。"
            )
            self._log(message)
            self.state["phase"] = "writing"
            self.state["awaiting"] = {
                "type": "chapter_plan_needed",
                "target": "03_new_novel/chapter_plan_parts",
                "message": message,
            }
        else:
            self._log("正文写作已完成本轮目标")
            self.state["phase"] = "completed"
            self.state["awaiting"] = None
        self._save_state()

    def write_batch(self, start: int, end: int, *, trial: bool = False) -> Path:
        from .cli import read_optional, write_text

        if trial:
            draft_path = self.project / "05_drafts" / f"trial_ch_{start:04d}_to_{end:04d}_{self._stamp()}.md"
        else:
            draft_path = self.project / "05_drafts" / f"ch_{start:04d}_to_{end:04d}.md"
        if not trial and draft_path.exists() and draft_path.stat().st_size > 500:
            self._log(f"跳过已存在正文批次：{draft_path.name}")
            return draft_path

        chapters = []
        previous_tail = self._previous_tail(start)
        for chapter_no in range(start, end + 1):
            plan = self._chapter_plan_row(chapter_no)
            if not plan:
                available = self._available_chapter_plan_prefix()
                raise RuntimeError(
                    f"第 {chapter_no:04d} 章章纲尚未生成，不能试写正文；当前已连续生成到第 {available:04d} 章。"
                )
            prompt = self._chapter_prompt(chapter_no, plan, previous_tail)
            self._log(f"调用模型写正文：第 {chapter_no:04d} 章")
            chapter_text = self._complete_with_preview(
                prompt,
                stream_label=f"正文第{chapter_no:04d}章",
                system=CHAPTER_DRAFT_SYSTEM_PROMPT,
                max_tokens=self.options.max_tokens_chapter,
                temperature=self.options.temperature,
            ).strip()
            chapter_text = self._strip_accidental_file_blocks(chapter_text)
            chapter_text = self._enforce_chapter_length(
                chapter_no,
                chapter_text,
                stream_label=f"压缩正文{chapter_no:04d}",
            )
            chapter_text = self._humanize_chapter_style(
                chapter_no,
                chapter_text,
                plan,
                stream_label=f"去AI味正文{chapter_no:04d}",
            )
            chapter_text = self._enforce_chapter_length(
                chapter_no,
                chapter_text,
                stream_label=f"去AI味压缩正文{chapter_no:04d}",
            )
            if not self._chapter_heading_title(chapter_text, chapter_no):
                chapter_text = self._enforce_short_chapter_title(chapter_no, chapter_text, plan)
            chapters.append(chapter_text)
            previous_tail = chapter_text[-900:]
            self._log(f"完成正文：第 {chapter_no:04d} 章，约 {len(chapter_text)} 字符")

        header = [
            f"# {'试写' if trial else '第'} {start:04d}-{end:04d} 章正文",
            "",
            f"> 自动生成时间：{dt.datetime.now().isoformat(timespec='seconds')}",
            "> 这是基于当前已生成章纲的试写稿，不会推进正式正文进度。" if trial else "",
            "",
            "## 本批写作依据",
            "",
            read_optional(self.project / "03_new_novel" / "novel_bible.md", 2000),
            "",
        ]
        write_text(draft_path, "\n\n".join(header + chapters).strip() + "\n")
        self._log(f"正文批次已保存：{draft_path.name}")
        return draft_path

    def write_trial_batch(self, start: int, end: int) -> str:
        if start < 1:
            raise RuntimeError("试写起始章不能小于 1。")
        if end < start:
            raise RuntimeError("试写结束章不能小于起始章。")
        if end - start + 1 > 10:
            raise RuntimeError("试写一次最多 10 章。建议先试 1-3 章，满意后再扩大。")
        missing = [number for number in range(start, end + 1) if not self._chapter_plan_row(number)]
        if missing:
            available = self._available_chapter_plan_prefix()
            raise RuntimeError(
                f"试写范围里有章纲未生成：{', '.join(f'{number:04d}' for number in missing[:8])}；"
                f"当前已连续生成到第 {available:04d} 章。"
            )
        self._log(f"基于已生成章纲试写正文：第 {start:04d}-{end:04d} 章")
        path = self.write_batch(start, end, trial=True)
        return f"试写完成：{path.relative_to(self.project)}"

    def promote_trial_range(self, start: int, end: int) -> str:
        from .cli import read_text, write_text

        if start < 1:
            raise RuntimeError("起始章不能小于 1。")
        if end < start:
            raise RuntimeError("结束章不能小于起始章。")
        draft_dir = self.project / "05_drafts"
        trial_files = sorted(draft_dir.glob("trial_ch_*_to_*.md"), key=lambda path: path.stat().st_mtime)
        if not trial_files:
            raise RuntimeError("没有找到试写稿 trial_ch_*.md。")

        chapters: dict[int, str] = {}
        used_files: list[str] = []
        for path in trial_files:
            text = read_text(path)
            _, found = self._split_draft_batch(text, start, end)
            if not found:
                continue
            for number, chapter_text in found.items():
                if start <= number <= end:
                    chapters[number] = chapter_text
                    if path.name not in used_files:
                        used_files.append(path.name)

        missing = [number for number in range(start, end + 1) if number not in chapters]
        if missing:
            raise RuntimeError("试写稿缺少章节：" + "、".join(f"第 {number:04d} 章" for number in missing))

        target = draft_dir / f"ch_{start:04d}_to_{end:04d}.md"
        if target.exists() and target.stat().st_size > 0:
            backup = draft_dir / "_trial_promote_backups" / f"{target.stem}_{self._stamp()}{target.suffix}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)

        header = [
            f"# 第 {start:04d}-{end:04d} 章正文",
            "",
            f"> 由试写稿转正式：{dt.datetime.now().isoformat(timespec='seconds')}",
            f"> 来源试写稿：{', '.join(used_files)}",
            "",
        ]
        body = "\n\n".join(header + [chapters[number].strip() for number in range(start, end + 1)]).strip() + "\n"
        write_text(target, body)

        self.state["phase"] = "writing"
        self.state["current_chapter"] = max(int(self.state.get("current_chapter") or 1), end + 1)
        self.state["awaiting"] = None
        self.state.setdefault("revisions", []).append(
            {
                "type": "promote_trial",
                "start": start,
                "end": end,
                "target": str(target.relative_to(self.project)).replace("\\", "/"),
                "sources": used_files,
                "promoted_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
        )
        self._save_state()
        self._log(f"试写稿已转为正式正文：{target.name}；下一章从第 {end + 1:04d} 章开始")
        return f"已转正式正文：{target.relative_to(self.project)}；下一章从第 {end + 1:04d} 章开始。"

    def continue_chapter_plan_until(self, target_chapter: int) -> str:
        if target_chapter < 1:
            raise RuntimeError("章纲目标章节不能小于 1。")
        current_prefix = self._available_chapter_plan_prefix()
        if target_chapter <= current_prefix:
            return f"当前章纲已经连续到第 {current_prefix:04d} 章，不需要补到第 {target_chapter:04d} 章。"
        self._log(f"继续补章纲：当前连续到第 {current_prefix:04d} 章，目标补到第 {target_chapter:04d} 章")
        previous_awaiting = self.state.get("awaiting")
        if (previous_awaiting or {}).get("type") == "chapter_plan_needed":
            self.state["awaiting"] = None
            self._save_state()
        context = self._blueprint_source_context()
        path = self._generate_chapter_plan_file(context, target_chapter)
        new_prefix = self._available_chapter_plan_prefix()
        self.state["phase"] = self.state.get("phase") or "decomposed"
        self.state["awaiting"] = (
            previous_awaiting
            if previous_awaiting and previous_awaiting.get("type") != "chapter_plan_needed"
            else None
        )
        self.state.setdefault("revisions", []).append(
            {
                "type": "continue_chapter_plan",
                "target_chapter": target_chapter,
                "prefix_after": new_prefix,
                "file": str(path.relative_to(self.project)).replace("\\", "/"),
                "continued_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
        )
        self._save_state()
        return f"章纲已补到连续第 {new_prefix:04d} 章。"

    def _chapter_prompt(self, chapter_no: int, plan: dict[str, str], previous_tail: str) -> str:
        from .cli import read_optional

        plan_text = self._draft_plan_text(plan)
        style_reference = self._source_style_reference(max_chars=7200, max_books=5)
        anti_ai_feedback = self._anti_ai_feedback()
        anti_ai_feedback_block = (
            "## 用户改稿与检测反馈\n\n"
            "下面内容用于让模型对比“机器稿”和“用户认可稿”的差异，只学习语气、顺序、对白自然度和段落松紧；"
            "不作为剧情设定，不覆盖本章章纲。\n\n"
            f"{anti_ai_feedback}\n\n"
            if anti_ai_feedback
            else ""
        )
        return f"""# 自动写作：第 {chapter_no:04d} 章

目标平台：{self.profile['display_name']}

## 本地样书口味参考

下面内容只用于学习标题口味、开场速度、段落长度、对白比例和飞卢都市爽文读感；不要照抄其中人名、句子、标题和剧情。

{style_reference or "（未找到本地样书正文，本章按本书资料和章纲直接写。）"}

{anti_ai_feedback_block}
## 本书资料

### novel_bible
{read_optional(self.project / "03_new_novel" / "novel_bible.md", 2200)}

### character_table
{read_optional(self.project / "03_new_novel" / "character_table.csv", 1600)}

### power_system
{read_optional(self.project / "03_new_novel" / "power_system.md", 1600)}

### power_state_ledger
{read_optional(self.project / "03_new_novel" / "power_state_ledger.md", 1400)}

### worldbuilding
{read_optional(self.project / "03_new_novel" / "worldbuilding.md", 1200)}

### volume_outline
{read_optional(self.project / "03_new_novel" / "volume_outline.md", 1400)}

### full_story_outline
{read_optional(self.project / "03_new_novel" / "full_story_outline.md", 1800)}

### memory_rollup
{read_optional(self.project / "03_new_novel" / "memory_rollup.md", 1400)}

### continuity_ledger
{read_optional(self.project / "03_new_novel" / "continuity_ledger.md", 1400)}

## 本章章纲

{plan_text or "（未找到本章细纲，请根据卷纲和上下文补齐，但不要改设定。）"}

## 上一章尾段

{previous_tail}

## 写作要求

只输出第 {chapter_no:04d} 章正文，不要输出解释、摘要、质检报告。

1. 章节标题格式为“第{chapter_no:04d}章 标题”；标题由你根据样书口味和本章正文重起，不要照抄章纲内部参考标题。
2. 单章约 {self.profile['chapter_words']} 字，绝对不能超过 5000 字符。
3. 章纲只负责剧情、人物、战力、道具、伏笔、结果和章末承接；正文写成小说场景，不要复述章纲字段。
4. 直接进场景，用人物行动、对话、围观反应和结果推进，不写说明文。
5. 金手指、战力、资源、证据链必须遵守本书资料，不临时新增无边界能力。
6. 文风按当前题材的飞卢都市神豪网文正文自然输出。
7. 避免“不是X，是Y”这类抽象判断短句，改成具体动作、对话和现场反应。
8. 章末留下下一章能直接接上的具体钩子。
"""

    def _draft_plan_text(self, plan: dict[str, str]) -> str:
        if not plan:
            return "（未找到本章细纲，请根据卷纲和上下文补齐，但不要改设定。）"
        labels = {
            "chapter_no": "章节号",
            "volume": "所在卷",
            "core_conflict": "本章事件主链",
            "small_hook": "开场触发点",
            "power_usage": "能力/资源使用边界",
            "character_change": "人物状态变化",
            "foreshadowing": "伏笔",
            "ending_hook": "章末承接",
            "mainline_progress": "本章必须完成的结果",
        }
        skip = {"title", "source_inspiration", "status"}
        lines = []
        for key, value in plan.items():
            if key in skip or not value:
                continue
            lines.append(f"- {labels.get(key, key)}: {value}")
        return "\n".join(lines)

    def _anti_ai_feedback(self, max_chars: int = 5000) -> str:
        paths = [
            self.project / "10_inbox" / "anti_ai_feedback.md",
            self.project / "00_config" / "anti_ai_feedback.md",
        ]
        chunks: list[str] = []
        for path in paths:
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                continue
            if text:
                chunks.append(text)
        return "\n\n".join(chunks)[:max_chars]

    def _record_anti_ai_note(self, note: str) -> None:
        text = str(note or "").strip()
        if not text:
            return
        markers = ("朱雀", "AI", "ai", "疑似", "检测", "人味", "文风", "样书", "标题", "不通顺", "不像人")
        if not any(marker in text for marker in markers):
            return
        target = self.project / "10_inbox" / "anti_ai_feedback.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        try:
            old = target.read_text(encoding="utf-8", errors="ignore") if target.exists() else ""
            addition = f"\n\n## UI 反馈追加：{stamp}\n\n{text}\n"
            target.write_text((old.rstrip() + addition).strip() + "\n", encoding="utf-8")
            self._log(f"已记录朱雀/文风反馈：{target.relative_to(self.project)}")
        except OSError as exc:
            self._log(f"朱雀/文风反馈记录失败：{exc}")

    def _humanize_chapter_style(
        self,
        chapter_no: int,
        chapter_text: str,
        plan: dict[str, str] | None = None,
        stream_label: str = "去AI味",
    ) -> str:
        from .humanizer import format_style_scan, scan_ai_style_hotspots

        text = chapter_text.strip()
        scan = scan_ai_style_hotspots(text)
        self._log(f"本地去AI味扫描：第 {chapter_no:04d} 章 level={scan.level} score={scan.score}")
        if scan.level in {"clean", "low"}:
            return text

        scan_report = format_style_scan(scan)
        self._log("去AI味热区：" + "；".join(item.label for item in scan.findings[:4]))
        plan_text = self._draft_plan_text(plan or {})
        style_reference = self._source_style_reference(max_chars=9000, max_books=5)
        anti_ai_feedback = self._anti_ai_feedback(max_chars=4200)
        prompt = f"""# 单章网文去AI味重写：第 {chapter_no:04d} 章

目标：保留原章节的剧情事实、事件顺序、人物关系、金手指/资源规则、伏笔和章末承接，只重写标题口味、句子节奏、对白语气、段落松紧和现场感。

## Humanizer-zh 本地规则层

识别中文 AI 写作模式、解释腔、模板句、过度规整表达和严肃深沉化口吻。
本步骤不是逐词替换，而是在不改变剧情底座的前提下，对整章的措辞、承接、转折、对白和段落松紧做自然化重写。

## 本地样书口味参考

下面内容只用于学习标题口味、开场速度、段落长度、对白比例和飞卢都市爽文读感；不要照抄其中人名、句子、标题和剧情。

{style_reference or "（未找到本地样书正文，本章按本书资料和章纲直接写。）"}

## 用户改稿与检测反馈

{anti_ai_feedback or "（暂无额外反馈。）"}

## 本地去AI味扫描热区

{scan_report}

## 本章事件底座

{plan_text}

## 原章节

{text}

## 输出要求

只输出第 {chapter_no:04d} 章完整正文，不要解释，不要摘要，不要报告。

1. 标题格式为“第{chapter_no:04d}章 标题”；标题根据样书目录口味和本章冲突重起，不照抄章纲标题。
2. 保留本章所有关键剧情和结果，不新增大事件，不改主角能力边界。
3. 把扫描热区改成更像真人写的网文现场：动作、对白、围观反应、物件、账单、截图、来电、系统提示都可以承担信息。
4. 对话先像角色在当场说话，再承担信息；不要让角色替作者解释设定。
5. 句子长短要有变化，不要把整章修成整齐短句，也不要写成总结散文。
"""
        rewritten = self._complete_with_preview(
            prompt,
            stream_label=stream_label,
            system=CHAPTER_DRAFT_SYSTEM_PROMPT,
            max_tokens=self.options.max_tokens_chapter,
            temperature=max(self.options.temperature, 0.66),
        ).strip()
        rewritten = self._strip_accidental_file_blocks(rewritten)
        if len(rewritten) < 800:
            self._log(f"去AI味重写返回过短，保留原章：第 {chapter_no:04d} 章")
            return text
        after_scan = scan_ai_style_hotspots(rewritten)
        self._log(
            f"去AI味扫描复查：第 {chapter_no:04d} 章 score {scan.score} -> {after_scan.score}"
        )
        return rewritten.strip()

    def _source_style_reference(self, max_chars: int = 7200, max_books: int = 5) -> str:
        cache_key = (max_chars, max_books)
        if cache_key in self._source_style_reference_cache:
            return self._source_style_reference_cache[cache_key]

        from .cli import load_json, read_text

        candidates = self._style_candidate_paths(load_json)
        if not candidates:
            self._source_style_reference_cache[cache_key] = ""
            return ""

        per_book_chars = max(900, max_chars // max(1, max_books))
        parts: list[str] = []
        used_names: list[str] = []
        for path in candidates[:max_books]:
            try:
                text = read_text(path, min(260000, max(90000, per_book_chars * 28)))
            except OSError:
                continue
            section = self._style_book_section(path, text, per_book_chars)
            if not section:
                continue
            parts.append(section)
            used_names.append(path.name)

        reference = "\n\n---\n\n".join(parts).strip()
        if len(reference) > max_chars:
            reference = reference[:max_chars].rstrip()
        if used_names:
            self._log("本地多本样书锚定：" + "；".join(used_names))
        self._source_style_reference_cache[cache_key] = reference
        return reference

    def _style_candidate_paths(self, load_json_func: Any, limit: int = 40) -> list[Path]:
        paths: list[Path] = []
        index_path = self.project / "01_sources" / "source_index.json"
        if index_path.exists():
            try:
                index = load_json_func(index_path)
                for book in index.get("books", []):
                    for item in book.get("files", []):
                        path = Path(str(item.get("path", "")))
                        if path.is_file():
                            paths.append(path)
            except Exception:
                pass

        roots: list[Path] = []
        source_dir = self.config.get("source_dir")
        if source_dir:
            roots.append(Path(str(source_dir)))
        if len(self.project.parents) >= 3:
            roots.append(
                self.project.parents[2]
                / "dist_fanqie"
                / "fanqie_oneclick_outputs"
                / "fanqie_txt_20260524_151410"
                / "books"
                / "飞卢"
            )
        roots.append(Path(r"D:\kuakedownload\【飞卢小说】去重合集[2300文件6.41G]"))

        seen = {str(path).lower() for path in paths}
        scored: list[tuple[int, int, Path]] = []
        for root in roots:
            if not root.exists():
                continue
            try:
                files = list(root.rglob("*.txt"))
            except OSError:
                continue
            for path in files:
                key = str(path).lower()
                if key in seen:
                    continue
                seen.add(key)
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                score = self._style_book_score(path.name)
                if score <= 0:
                    continue
                scored.append((score, size, path))

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        for _score, _size, path in scored[:limit]:
            paths.append(path)
        deduped: list[Path] = []
        seen.clear()
        seen_books: set[str] = set()
        for path in paths:
            key = str(path).lower()
            book_key = self._style_book_identity(path.name)
            if key in seen or book_key in seen_books or not path.is_file():
                continue
            seen.add(key)
            seen_books.add(book_key)
            deduped.append(path)
        return deduped[:limit]

    def _style_book_identity(self, name: str) -> str:
        text = Path(name).stem.lower()
        text = re.sub(r"[\s　]+", "", text)
        text = re.sub(r"[\(\（]\d+[\)\）]$", "", text)
        text = re.sub(r"(全本网|全本|校对版|已读校|完结|至\d+章|1---\d+|۞\d+|⊙.*)$", "", text)
        return text or Path(name).stem.lower()

    def _style_book_score(self, name: str) -> int:
        text = name.lower()
        positive = {
            "神豪": 30,
            "都市": 22,
            "系统": 18,
            "任务": 14,
            "首富": 14,
            "败家": 14,
            "奖励": 12,
            "富": 8,
            "豪门": 8,
            "校花": 8,
            "签到": 8,
            "抽奖": 8,
            "投资": 8,
        }
        negative = {
            "火影": 16,
            "海贼": 16,
            "洪荒": 14,
            "综漫": 14,
            "玄幻": 10,
            "武侠": 10,
            "moba": 10,
            "lol": 10,
            "nba": 8,
            "军阀": 8,
        }
        score = sum(weight for word, weight in positive.items() if word in text)
        score -= sum(weight for word, weight in negative.items() if word in text)
        return score

    def _style_book_section(self, path: Path, text: str, per_book_chars: int) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
        if not text.strip():
            return ""
        title_matches = list(re.finditer(r"(?m)^\s*第\s*(?:\d+|[一二三四五六七八九十百千万两]+)\s*章[^\n]{0,48}", text))
        titles = [m.group(0).strip() for m in title_matches[:14]]
        starts = [m.start() for m in title_matches[:30]]
        if not starts:
            starts = [0]
        sample_starts = [starts[0]]
        if len(starts) > 5:
            sample_starts.append(starts[min(4, len(starts) - 1)])
        if len(starts) > 12:
            sample_starts.append(starts[min(11, len(starts) - 1)])

        sample_budget = max(260, per_book_chars // max(1, len(sample_starts)))
        samples = []
        for index, start in enumerate(dict.fromkeys(sample_starts), start=1):
            samples.append(f"片段{index}：\n{text[start:start + sample_budget].strip()}")
        title_block = "\n".join(titles)
        return (
            f"样书锚定文件：{path.name}\n\n"
            f"目录标题口味：\n{title_block or '（未识别目录标题）'}\n\n"
            f"正文片段口味：\n" + "\n\n".join(samples)
        ).strip()

    def summarize_batch(self, start: int, end: int) -> None:
        from .cli import read_optional, write_text

        draft = read_optional(self.project / "05_drafts" / f"ch_{start:04d}_to_{end:04d}.md", 45000)
        self._log(f"开始更新摘要和长期记忆：第 {start:04d}-{end:04d} 章")
        prompt = f"""# 批次归档更新

请基于第 {start:04d}-{end:04d} 章正文，按 FILE_BUNDLE 协议更新摘要和长期记忆。

## 正文

{draft}

## 现有长期记忆

### character_table.csv
{read_optional(self.project / "03_new_novel" / "character_table.csv", 8000)}

### continuity_ledger.md
{read_optional(self.project / "03_new_novel" / "continuity_ledger.md", 8000)}

### power_state_ledger.md
{read_optional(self.project / "03_new_novel" / "power_state_ledger.md", 8000)}

### foreshadowing_ledger.md
{read_optional(self.project / "03_new_novel" / "foreshadowing_ledger.md", 8000)}

### memory_rollup.md
{read_optional(self.project / "03_new_novel" / "memory_rollup.md", 8000)}

请输出：

<file path="06_summaries/summary_{start:04d}_to_{end:04d}.md">每章 100-200 字摘要、角色变化、能力变化、新增/回收伏笔、下一批承接</file>
<file path="03_new_novel/character_table.csv">更新后的完整角色表</file>
<file path="03_new_novel/continuity_ledger.md">更新后的连续性账本</file>
<file path="03_new_novel/power_state_ledger.md">更新后的战力台账：必须包含本批结束后的修为/等级、当前小段或阶段、核心能力解锁状态、关键道具权限、伤势/反噬代价、越级依据和下一突破门槛</file>
<file path="03_new_novel/foreshadowing_ledger.md">更新后的伏笔账本</file>
<file path="03_new_novel/memory_rollup.md">更新后的压缩长期记忆</file>
{FILE_PROTOCOL}
"""
        result = self._complete_with_preview(
            prompt,
            stream_label=f"摘要{start:04d}-{end:04d}",
            max_tokens=self.options.max_tokens_maintenance,
            temperature=0.35,
        )
        if not self._write_file_bundle(result):
            write_text(self.project / "06_summaries" / f"summary_{start:04d}_to_{end:04d}.md", result.strip() + "\n")
            self._log(f"摘要原始结果已保存：第 {start:04d}-{end:04d} 章")
        else:
            self._log(f"摘要和长期记忆已更新：第 {start:04d}-{end:04d} 章")

    def review_batch(self, start: int, end: int) -> None:
        from .cli import prompt_review, write_text

        self._log(f"调用模型审稿：第 {start:04d}-{end:04d} 章")
        prompt = prompt_review(self.project, self.config, self.profile, "auto-agent", start, end - start + 1)
        result = self._complete_with_preview(
            prompt,
            stream_label=f"审稿{start:04d}-{end:04d}",
            max_tokens=self.options.max_tokens_maintenance,
            temperature=0.25,
        )
        write_text(self.project / "07_reviews" / f"review_{start:04d}_to_{end:04d}.md", result.strip() + "\n")
        self._log(f"审稿结果已保存：第 {start:04d}-{end:04d} 章")

    def archive_stage(self, start: int, end: int) -> None:
        from .cli import prompt_archive, write_text

        self._log(f"调用模型阶段归档：第 {start:04d}-{end:04d} 章")
        prompt = prompt_archive(self.project, self.config, self.profile, "auto-agent", start, end - start + 1)
        prompt += FILE_PROTOCOL
        result = self._complete_with_preview(
            prompt,
            stream_label=f"阶段归档{start:04d}-{end:04d}",
            max_tokens=self.options.max_tokens_maintenance,
            temperature=0.25,
        )
        if not self._write_file_bundle(result):
            write_text(self.project / "07_reviews" / f"archive_{start:04d}_to_{end:04d}.md", result.strip() + "\n")
            self._log(f"阶段归档原始结果已保存：第 {start:04d}-{end:04d} 章")
        else:
            self._log(f"阶段归档已更新：第 {start:04d}-{end:04d} 章")

    def _chapter_plan_row(self, chapter_no: int) -> dict[str, str]:
        rows = self._chapter_plan_rows_by_no()
        return rows.get(chapter_no, {})

    def _chapter_plan_rows_by_no(self) -> dict[int, dict[str, str]]:
        from .cli import read_text

        header = next(csv.reader([CHAPTER_PLAN_HEADER]))
        rows_by_no: dict[int, dict[str, str]] = {}
        path = self.project / "03_new_novel" / "chapter_plan.csv"
        if path.exists():
            text = read_text(path)
            if "状态：待生成" not in text:
                for number, row in self._valid_chapter_rows(text).items():
                    rows_by_no[number] = dict(zip(header, row))
        parts_dir = self.project / "03_new_novel" / "chapter_plan_parts"
        if parts_dir.exists():
            for part in sorted(parts_dir.glob("ch_*.csv")):
                if "_raw_" in part.name:
                    continue
                for number, row in self._valid_chapter_rows(read_text(part)).items():
                    rows_by_no[number] = dict(zip(header, row))
        return rows_by_no

    def _available_chapter_plan_prefix(self) -> int:
        return self._covered_prefix_end(set(self._chapter_plan_rows_by_no()))

    def _previous_tail(self, start: int) -> str:
        from .cli import previous_draft_tail

        return previous_draft_tail(self.project, start)

    def _source_analysis_context(self, per_book_dir: Path, chars_per_book: int = 4500) -> tuple[str, int]:
        from .cli import read_text

        chunks = []
        for index, path in enumerate(sorted(per_book_dir.glob("*.md")), start=1):
            if path.stat().st_size <= 0:
                continue
            text = read_text(path).strip()
            if not text:
                continue
            truncated = text[:chars_per_book]
            if len(text) > chars_per_book:
                truncated += f"\n\n...[该单本拆解已截断，原文件约 {len(text)} 字符]..."
            chunks.append(f"## {index}. {path.stem}\n\n{truncated}")
        return "\n\n".join(chunks), len(chunks)

    def _blueprint_source_context(self) -> str:
        from .cli import read_optional

        parts = [
            "## 已选择的候选大纲\n"
            + read_optional(self.project / str(self.state.get("selected_outline_path") or ""), 18000)
            if self.state.get("selected_outline_path")
            else "## 已选择的候选大纲\n（尚未选择候选大纲。）",
            "## source_bibles.md\n" + read_optional(self.project / "02_source_analysis" / "source_bibles.md", 14000),
            "## source_style_profile.md\n" + read_optional(self.project / "02_source_analysis" / "source_style_profile.md", 9000),
            "## motif_library.csv\n" + read_optional(self.project / "02_source_analysis" / "motif_library.csv", 9000),
            "## character_pool.csv\n" + read_optional(self.project / "02_source_analysis" / "character_pool.csv", 9000),
            "## plot_pool.csv\n" + read_optional(self.project / "02_source_analysis" / "plot_pool.csv", 9000),
            "## power_system_pool.csv\n" + read_optional(self.project / "02_source_analysis" / "power_system_pool.csv", 9000),
            "## fusion_opportunities.md\n" + read_optional(self.project / "02_source_analysis" / "fusion_opportunities.md", 12000),
            "## source_risk_notes.md\n" + read_optional(self.project / "02_source_analysis" / "source_risk_notes.md", 9000),
        ]
        return "\n\n".join(parts)

    def _write_single_file_output(self, rel_path: str, text: str) -> Path:
        from .cli import write_text

        rel = rel_path.replace("\\", "/").strip()
        if rel.startswith("/") or ".." in Path(rel).parts:
            raise RuntimeError(f"拒绝写入非法路径：{rel_path}")
        target = (self.project / rel).resolve()
        if not str(target).lower().startswith(str(self.project).lower()):
            raise RuntimeError(f"拒绝写入项目外路径：{rel_path}")

        body = self._normalize_single_file_output(text).strip()
        if rel_path.endswith(".csv"):
            body = self._normalize_csv_body(rel_path, body)
        if self._chapter_part_range(rel_path):
            body = self._canonicalize_chapter_part_csv(rel_path, body)
        issue = self._single_file_issue(rel_path, text, body)
        if issue:
            raw_path = target.with_name(f"{target.stem}_raw_{self._stamp()}{target.suffix or '.md'}")
            write_text(raw_path, text.strip() + "\n")
            raise OutputQualityError(rel_path, issue, raw_path)
        write_text(target, body + "\n")
        return target

    def _chapter_part_range(self, rel_path: str) -> tuple[int, int] | None:
        match = re.search(r"03_new_novel/chapter_plan_parts/ch_(\d{3,4})_(\d{3,4})\.csv$", rel_path)
        return (int(match.group(1)), int(match.group(2))) if match else None

    def _canonicalize_chapter_part_csv(self, rel_path: str, body: str) -> str:
        segment = self._chapter_part_range(rel_path)
        if not segment:
            return body
        start, end = segment
        rows = {
            number: row
            for number, row in self._valid_chapter_rows(body).items()
            if start <= number <= end
        }
        if not rows:
            return body
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(next(csv.reader([CHAPTER_PLAN_HEADER])))
        writer.writerows(rows[number] for number in sorted(rows))
        return output.getvalue().strip()

    def _generate_single_file_with_retries(
        self,
        rel_path: str,
        prompt: str,
        *,
        stream_label: str,
        max_tokens: int | None,
        temperature: float,
    ) -> Path:
        attempts = max(2, self.options.source_retries + 2)
        last_error: Exception | None = None
        current_prompt = prompt
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                self._log(f"{Path(rel_path).name} 第 {attempt}/{attempts} 次重试：已根据上次失败原因修正提示词")
                time.sleep(min(8, 2 * attempt))
            try:
                result = self._complete_with_preview(
                    current_prompt,
                    stream_label=stream_label if attempt == 1 else f"{stream_label}:重试{attempt}",
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                return self._write_single_file_output(rel_path, result)
            except OutputQualityError as exc:
                last_error = exc
                self._log(f"自动诊断：{Path(rel_path).name} 返回异常，问题={exc.reason}")
                current_prompt = self._retry_prompt(prompt, rel_path, exc.reason, attempt + 1)
                if attempt >= attempts:
                    raise
            except Exception as exc:  # noqa: BLE001 - retry normalized model/proxy/empty-output failures.
                last_error = exc
                detail = str(exc).replace("\n", " ")[:220]
                self._log(f"自动诊断：{Path(rel_path).name} 调用异常，问题={detail}")
                current_prompt = self._retry_prompt(prompt, rel_path, f"上游调用异常：{detail}", attempt + 1)
                if attempt >= attempts:
                    raise
        raise RuntimeError(f"{rel_path} 生成失败：{last_error}") from last_error

    def _single_file_ready(self, rel_path: str, min_size: int = 500) -> bool:
        from .cli import read_text

        path = self.project / rel_path
        try:
            if not path.is_file() or path.stat().st_size < min_size:
                return False
            text = read_text(path)
        except OSError:
            return False
        if "状态：待生成" in text:
            return False
        if rel_path == "03_new_novel/chapter_plan.csv":
            return self._chapter_plan_complete(text, self._target_chapter_count())
        part_match = re.search(r"03_new_novel/chapter_plan_parts/ch_(\d{3,4})_(\d{3,4})\.csv$", rel_path)
        if part_match:
            try:
                self._validate_chapter_part(path, int(part_match.group(1)), int(part_match.group(2)))
                return True
            except OutputQualityError:
                return False
        return True

    def _single_file_issue(self, rel_path: str, raw_text: str, body: str) -> str:
        raw = raw_text.strip()
        if not raw:
            return "模型返回为空"
        chapter_part_rows = self._valid_chapter_rows(body) if self._chapter_part_range(rel_path) else {}
        has_complete_wrapper = bool(
            FILE_BLOCK_RE.search(raw) or ALT_FILE_BLOCK_RE.search(raw) or BRACKET_FILE_BLOCK_RE.search(raw)
        )
        if (
            not has_complete_wrapper
            and re.match(r"^\s*(?:FILE_BUNDLE\s*)?<file\s+path=", raw, re.IGNORECASE)
            and not re.search(r"</file>", raw, re.IGNORECASE)
            and not chapter_part_rows
        ):
            return "FILE_BUNDLE 没有正常结束，文件块未闭合"
        if (
            not has_complete_wrapper
            and re.match(r"^\s*(?:FILE_BUNDLE\s*)?<<<FILE\s*:?\s*path=", raw, re.IGNORECASE)
            and not re.search(r"(<<<\s*END[\s_-]*FILE\s*>>>|</FILE\s*>>>)", raw, re.IGNORECASE)
            and not chapter_part_rows
        ):
            return "FILE_BUNDLE 没有正常结束，文件块未闭合"
        if (
            not has_complete_wrapper
            and re.match(r"^\s*(?:\[FILE_BUNDLE\]\s*)?\[FILE_START\s+path=", raw, re.IGNORECASE)
            and not re.search(r"\[FILE_END\s*\]", raw, re.IGNORECASE)
            and not chapter_part_rows
        ):
            return "FILE_BUNDLE 没有正常结束，文件块未闭合"
        if len(body) < 80:
            return f"内容过短，仅 {len(body)} 字符"
        if "状态：待生成" in body:
            return "仍是占位内容"
        if rel_path.endswith(".csv"):
            rows = [line for line in body.splitlines() if line.strip()]
            min_rows = 2
            expected_header = self._expected_csv_header(rel_path)
            if expected_header and (not rows or rows[0] != expected_header):
                return "CSV 首行不是要求的表头"
            if self._chapter_part_range(rel_path):
                if not chapter_part_rows:
                    return "章节分段没有可恢复的有效数据行"
                return self._chapter_plan_detail_issue(chapter_part_rows)
            if rel_path.endswith("chapter_plan.csv"):
                target_total = self._target_chapter_count()
                if not self._chapter_plan_complete(body, target_total):
                    numbers = self._chapter_numbers_from_csv(body)
                    max_no = max(numbers) if numbers else 0
                    return f"章节规划不完整：当前到第 {max_no:03d} 章，目标第 {target_total:03d} 章"
                detail_issue = self._chapter_plan_detail_issue(self._valid_chapter_rows(body))
                if detail_issue:
                    return detail_issue
                min_rows = target_total + 1
            elif rel_path.endswith("character_table.csv"):
                min_rows = 6
            elif rel_path.startswith("02_source_analysis/"):
                min_rows = 8
            if len(rows) < min_rows:
                return f"CSV 行数不足：{len(rows)}/{min_rows}"
            if "," not in rows[0]:
                return "CSV 首行不像表头"
        else:
            min_chars = 500
            if "outline_candidates" in rel_path:
                min_chars = 1200
            if rel_path.endswith("full_story_outline.md"):
                min_chars = 2000
            if len(body) < min_chars:
                return f"Markdown 内容过短：{len(body)}/{min_chars} 字符"
        return ""

    def _chapter_plan_complete(self, text: str, target_total: int) -> bool:
        body = self._normalize_csv_body("03_new_novel/chapter_plan.csv", text)
        rows = [line for line in body.splitlines() if line.strip()]
        if not rows or rows[0] != CHAPTER_PLAN_HEADER:
            return False
        numbers = set(self._chapter_numbers_from_csv(body))
        return all(number in numbers for number in range(1, target_total + 1))

    def _retry_prompt(self, prompt: str, rel_path: str, reason: str, attempt: int) -> str:
        return f"""{prompt}

## 自动诊断后的重试要求

上一次生成 `{rel_path}` 没有通过本地校验。
失败位置/原因：{reason}
当前是第 {attempt} 次尝试。

请修正：
- 必须输出完整、可落盘的 `{rel_path}` 正文。
- 不要只输出空白、标题、表头或占位语。
- 如果是 CSV，必须包含表头和足够多的数据行；字段用英文逗号分隔。
- 如果是章节规划 CSV，不要为了迎合检查硬塞固定关键词；按自然中文把每章的触发事件、阻碍、反制、结果和章末承接写具体。
- 如果是 Markdown，必须写出完整结构，不要中途停止。
- 不要解释失败原因，不要输出道歉，只重新输出目标文件正文。
"""

    def _normalize_single_file_output(self, text: str) -> str:
        body = text.strip()
        match = FILE_BLOCK_RE.search(body)
        alt_match = ALT_FILE_BLOCK_RE.search(body)
        fenced_match = FENCED_FILE_BLOCK_RE.search(body)
        bracket_match = BRACKET_FILE_BLOCK_RE.search(body)
        if match:
            body = match.group(2).strip()
        elif alt_match:
            body = alt_match.group(2).strip()
        elif fenced_match:
            body = fenced_match.group(2).strip()
        elif bracket_match:
            body = bracket_match.group(2).strip()
        else:
            body = re.sub(r"^\s*<file\s+path=[\"'][^\"']+[\"']\s*>\s*", "", body, flags=re.IGNORECASE | re.DOTALL)
            body = re.sub(r"\s*</file>\s*$", "", body, flags=re.IGNORECASE)
            body = re.sub(r"^\s*FILE_BUNDLE\s*", "", body, flags=re.IGNORECASE)
            body = re.sub(r"\s*FILE_BUNDLE\s*$", "", body, flags=re.IGNORECASE)
            body = re.sub(r"^\s*\[FILE_BUNDLE\]\s*", "", body, flags=re.IGNORECASE)
            body = re.sub(r"^\s*\[FILE_START\s+path=[^\]]+\]\s*", "", body, flags=re.IGNORECASE)
            body = re.sub(r"\s*\[FILE_END\s*\]\s*(?:\[/FILE_BUNDLE\])?\s*$", "", body, flags=re.IGNORECASE)
            body = re.sub(r"^\s*```[^\n]*\n", "", body)
            body = re.sub(r"\n```\s*$", "", body)

        fence = re.match(r"^```[^\n]*\n(.*)\n```\s*$", body, re.DOTALL)
        if fence:
            body = fence.group(1).strip()
        return body

    def _expected_csv_header(self, rel_path: str) -> str | None:
        expected_headers = {
            "02_source_analysis/motif_library.csv": "motif_id,name,source_books,weight,stage,usage,risk_note",
            "02_source_analysis/character_pool.csv": "role_id,function_role,source_books,source_element,traits,relationship_use,growth_arc,keep_or_modify,fusion_target,avoid_copying",
            "02_source_analysis/plot_pool.csv": "plot_id,module_name,source_books,source_element,conflict_model,setup,payoff,keep_or_modify,fusion_target,risk_note",
            "02_source_analysis/power_system_pool.csv": "system_id,source_books,source_element,core_logic,realm_or_upgrade_path,limit,cost,payoff,keep_or_modify,fusion_target",
            "03_new_novel/character_table.csv": "character_id,name,function_role,first_appearance,goal,secret,relationship_to_mc,growth_or_fall,power_level,status",
            "03_new_novel/chapter_plan.csv": "chapter_no,volume,title,core_conflict,small_hook,power_usage,character_change,foreshadowing,ending_hook,mainline_progress,source_inspiration,status",
        }
        if self._chapter_part_range(rel_path):
            return CHAPTER_PLAN_HEADER
        part_match = re.match(r"02_source_analysis/aggregate_parts/([^/]+)_part_\d+\.csv$", rel_path)
        if part_match:
            rel_path = f"02_source_analysis/{part_match.group(1)}.csv"
        return expected_headers.get(rel_path)

    def _normalize_csv_body(self, rel_path: str, body: str) -> str:
        expected = self._expected_csv_header(rel_path)
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if not lines:
            return body
        if expected:
            for index, line in enumerate(lines):
                if line == expected:
                    return "\n".join(lines[index:])
            first_field = expected.split(",", 1)[0] + ","
            for index, line in enumerate(lines):
                if line.startswith(first_field):
                    return "\n".join(lines[index:])
            if self._chapter_part_range(rel_path):
                for index, line in enumerate(lines):
                    if re.match(r"^\d{1,4}\s*,", line):
                        return expected + "\n" + "\n".join(lines[index:])
        return "\n".join(lines)

    def _write_file_bundle(self, text: str) -> list[Path]:
        from .cli import write_text

        written = []
        for match in list(FILE_BLOCK_RE.finditer(text)) + list(ALT_FILE_BLOCK_RE.finditer(text)) + list(BRACKET_FILE_BLOCK_RE.finditer(text)):
            rel = match.group(1).strip().replace("\\", "/")
            body = match.group(2).strip() + "\n"
            if rel.startswith("/") or ".." in Path(rel).parts:
                continue
            target = (self.project / rel).resolve()
            if not str(target).lower().startswith(str(self.project).lower()):
                continue
            write_text(target, body)
            written.append(target)
        for match in FENCED_FILE_BLOCK_RE.finditer(text):
            raw_rel = match.group(1)
            if not raw_rel:
                continue
            rel = raw_rel.strip().replace("\\", "/")
            body = match.group(2).strip() + "\n"
            if rel.startswith("/") or ".." in Path(rel).parts:
                continue
            target = (self.project / rel).resolve()
            if not str(target).lower().startswith(str(self.project).lower()):
                continue
            write_text(target, body)
            written.append(target)
        return written

    def _strip_accidental_file_blocks(self, text: str) -> str:
        match = FILE_BLOCK_RE.search(text)
        if match:
            return match.group(2).strip()
        alt_match = ALT_FILE_BLOCK_RE.search(text)
        if alt_match:
            return alt_match.group(2).strip()
        fenced_match = FENCED_FILE_BLOCK_RE.search(text)
        if fenced_match:
            return fenced_match.group(2).strip()
        bracket_match = BRACKET_FILE_BLOCK_RE.search(text)
        if bracket_match:
            return bracket_match.group(2).strip()
        return text

    def _stamp(self) -> str:
        return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def build_auto_options(args: argparse.Namespace, config_batch_size: int = 30) -> AutoOptions:
    source_timeout = args.source_timeout or (3600 if args.source_mode == "deep" else 900)
    return AutoOptions(
        source_mode=args.source_mode,
        fusion_mode=args.fusion_mode,
        source_workers=args.source_workers,
        source_timeout=source_timeout,
        source_retries=args.source_retries,
        max_source_chunks_per_book=args.max_source_chunks_per_book,
        chunk_chars=args.chunk_chars,
        batch_size=args.batch_size or config_batch_size,
        max_chapters=args.max_chapters,
        review_every=args.review_every,
        archive_every=args.archive_every,
        stop_at_checkpoints=not args.no_checkpoints,
        max_tokens_analysis=args.max_tokens_analysis,
        max_tokens_blueprint=args.max_tokens_blueprint,
        max_tokens_chapter=args.max_tokens_chapter,
        max_tokens_maintenance=args.max_tokens_maintenance,
        temperature=args.temperature,
        stream_preview=args.stream_preview,
    )
