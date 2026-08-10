from __future__ import annotations

import re
from dataclasses import dataclass


AI_LEXICON_ZH = (
    "总而言之",
    "综上所述",
    "总的来说",
    "概括而言",
    "不难看出",
    "值得注意的是",
    "需要指出的是",
    "至关重要的是",
    "在这个过程中",
    "从多个角度来看",
    "从长远来看",
    "从某种程度上说",
    "可以说",
    "不言而喻",
    "显得尤为重要",
    "具有重要意义",
    "发挥着重要作用",
    "起着至关重要的作用",
    "注入新的活力",
    "开启新的篇章",
    "迈向新的阶段",
    "展现出独特价值",
    "赋能",
    "助力",
    "深耕",
    "布局",
    "打造",
    "构建",
    "引领",
    "聚焦",
    "链接",
    "生态",
    "维度",
    "颗粒度",
    "底层逻辑",
    "顶层设计",
    "全方位",
    "多层次",
    "蓬勃发展",
    "与日俱增",
    "日新月异",
    "任重道远",
    "砥砺前行",
)

WEBNOVEL_AI_PATTERNS = (
    "真正的风暴才刚刚开始",
    "这一刻",
    "命运的齿轮",
    "拉开序幕",
    "才刚刚开始",
    "眼神又冷又沉",
    "像在看一个已经翻篇的东西",
    "不是记忆，是信息",
    "不是劝酒，是灌",
    "不是爆炸，是",
    "不是切进去的，是",
)

SOMBER_WEBNOVEL_PATTERNS = (
    "脸色阴沉",
    "眼神又冷又沉",
    "让人后背发凉",
    "声音不轻不重",
    "每一个字都咬得很清楚",
    "强撑着不让自己发抖",
    "像在看一个已经翻篇的东西",
    "沉默了三秒",
    "牙缝里挤出来",
)


@dataclass(frozen=True)
class StyleFinding:
    source: str
    label: str
    count: int
    examples: tuple[str, ...]
    advice: str


@dataclass(frozen=True)
class StyleScan:
    score: int
    level: str
    findings: tuple[StyleFinding, ...]


def scan_ai_style_hotspots(text: str, max_examples: int = 3) -> StyleScan:
    body = str(text or "")
    findings: list[StyleFinding] = []

    def add(source: str, label: str, matches: list[str], advice: str, weight: int = 1) -> None:
        clean = [_compact(m) for m in matches if _compact(m)]
        if not clean:
            return
        examples = tuple(dict.fromkeys(clean[:max_examples]))
        findings.append(StyleFinding(source, label, len(clean) * weight, examples, advice))

    add(
        "Humanizer-zh",
        "抽象判断短句：不是X，是Y",
        re.findall(r"不是[^。！？\n]{1,18}[，,。；;]?\s*是[^。！？\n]{1,22}", body),
        "改成角色动作、对白反应或现场结果，不要用压缩判断替代场景。",
        weight=2,
    )
    add(
        "Humanizer-zh",
        "AI/公文式包装词",
        [term for term in AI_LEXICON_ZH if term in body],
        "删掉宏大总结词，换成具体人、具体物件、具体动作和当场结果。",
    )
    add(
        "Humanizer-zh",
        "网文AI模板句",
        [term for term in WEBNOVEL_AI_PATTERNS if term in body],
        "这些句子容易显得像模型套模板，改成当前角色自己的说法和动作。",
        weight=2,
    )
    somber_hits = [term for term in SOMBER_WEBNOVEL_PATTERNS if term in body]
    if len(somber_hits) >= 3:
        add(
            "本地网文审计",
            "严肃深沉化口吻过密",
            somber_hits,
            "压力要服务快速打脸，不要连续写成正剧压抑感；改成更轻、更狠、更直接的爽文现场。",
            weight=2,
        )
    add(
        "Humanizer-zh",
        "解释腔提示词",
        re.findall(r"(?:他|她|宁川|主角)?(?:知道|意识到|明白|清楚)[^。！？\n]{0,28}", body),
        "少用作者替角色总结，改成角色看见、听见、说出口或直接做出来。",
    )
    add(
        "Humanizer-zh",
        "命运/格局式收束",
        re.findall(r"(?:真正的|更大的|新的)?(?:风暴|序幕|格局|命运|暗流)[^。！？\n]{0,28}", body),
        "章末钩子落到下一步动作、来电、任务、敌人或证据，不要落到抽象氛围。",
        weight=2,
    )

    paragraphs = [_compact(p) for p in re.split(r"\n\s*\n", body) if _compact(p)]
    short_runs: list[str] = []
    current: list[str] = []
    for paragraph in paragraphs:
        is_dialogue = paragraph.startswith(("“", "\"", "【", "["))
        if 4 <= len(paragraph) <= 18 and not is_dialogue and paragraph.endswith(("。", "！", "？")):
            current.append(paragraph)
        else:
            if len(current) >= 5:
                short_runs.extend(current[:max_examples])
            current = []
    if len(current) >= 5:
        short_runs.extend(current[:max_examples])
    add(
        "本地网文审计",
        "短句节奏过于机械",
        short_runs,
        "飞卢可以短句密，但不能连续像切片说明；穿插动作、对白、围观反应和更长的现场句。",
        weight=2,
    )

    quote_matches = re.findall(r"[“\"]([^”\"\n]{8,90})[”\"]", body)
    explain_dialogue = [
        q
        for q in quote_matches
        if any(word in q for word in ("情况", "规定", "规则", "因为", "所以", "这意味着", "必须", "需要"))
    ]
    add(
        "本地网文审计",
        "对白像功能说明",
        explain_dialogue,
        "对白先服务身份、情绪和压迫；设定信息拆到动作、账单、截图、围观议论里。",
    )

    score = sum(item.count for item in findings)
    if score >= 12:
        level = "high"
    elif score >= 5:
        level = "medium"
    elif score > 0:
        level = "low"
    else:
        level = "clean"
    return StyleScan(score=score, level=level, findings=tuple(findings))


def format_style_scan(scan: StyleScan, max_findings: int = 8) -> str:
    if not scan.findings:
        return "本地去AI味扫描未发现明显热区。"
    lines = [f"本地去AI味扫描：level={scan.level}, score={scan.score}"]
    for item in scan.findings[:max_findings]:
        examples = "；".join(item.examples)
        lines.append(f"- [{item.source}] {item.label} x{item.count}: {item.advice}")
        if examples:
            lines.append(f"  例：{examples}")
    return "\n".join(lines)


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
