from __future__ import annotations

import re
from dataclasses import dataclass

from .domain import Shot


@dataclass(slots=True)
class PromptIssue:
    severity: str
    code: str
    message: str


def check_shot(shot: Shot) -> list[PromptIssue]:
    issues: list[PromptIssue] = []
    if not shot.prompt:
        issues.append(PromptIssue("error", "missing_prompt", "缺少视频画面提示词"))
        return issues
    if len(shot.prompt) < 18:
        issues.append(PromptIssue("warning", "short_prompt", "提示词过短，可能缺少主体、动作或场景"))
    if not re.search(r"人物|角色|场景|画面|镜头|环境|城市|室内|室外", shot.prompt):
        issues.append(PromptIssue("warning", "missing_structure", "未检测到明确的主体/场景/镜头结构词"))
    risky = ("像某", "模仿", "某某明星", "迪士尼", "漫威", "DC", "原神", "火影", "克苏鲁", "漫威风", "迪士尼风")
    for token in risky:
        if token in shot.prompt:
            issues.append(PromptIssue("manual", "rights_reference", f"包含可能触发版权或风格模仿审核的词：{token}"))
    graphic = ("鲜血", "血液飞溅", "暗红色液体飞溅", "割喉", "刺入", "肢解", "断肢", "尸体", "喉咙暗红色液体")
    for token in graphic:
        if token in shot.prompt:
            issues.append(PromptIssue("manual", "graphic_violence", f"包含可能触发平台审核的具象伤害描写：{token}"))
    return issues


SAFE_REPLACEMENTS = {
    "约8岁的小女孩（渊）": "身高到成人腰部的年轻女孩（渊）",
    "小女孩（渊，8岁，": "身高到成人腰部的年轻女孩（渊，",
    "小女孩（渊）": "身高到成人腰部的年轻女孩（渊）",
    "克苏鲁": "不可名状的外神",
    "漫威风": "现代超级英雄电影质感",
    "迪士尼风": "明亮的家庭向动画电影质感",
    "UE5 Lumen": "电影级实时渲染光照",
    "Nanite高精度建筑": "高精度建筑几何细节",
    "暗红色液体飞溅": "暗红色能量粒子散开，不展示血腥特写",
    "喉咙暗红色液体": "颈侧暗红色能量痕迹",
    "鼻孔有干涸血迹": "面部有暗色污渍",
    "刀刃划过": "刀锋快速掠过目标身侧",
    "刺向旁边操作电脑的特工": "逼近旁边操作电脑的特工",
    "精准点在对方后颈脑干位置": "以精准能量手法解除对方颈后的寄生控制",
    "用高频刀刀背精准击打被寄生平民的后颈": "用高频刀释放定向蓝色脉冲解除被寄生平民的控制",
    "用麻醉枪射击另一个的后颈": "用非致伤麻醉装置协同控制另一个目标",
    "眼睛纯黑嘴部裂开": "眼睛呈纯黑色，面部出现非人化异变",
}


def safe_rewrite(prompt: str) -> tuple[str, list[str]]:
    """Make only deterministic, low-drift substitutions; never invent story facts."""
    rewritten = prompt
    changes: list[str] = []
    for source, target in SAFE_REPLACEMENTS.items():
        if source in rewritten:
            rewritten = rewritten.replace(source, target)
            changes.append(f"{source}->{target}")
    return rewritten, changes


def check_all(shots: list[Shot]) -> list[tuple[Shot, list[PromptIssue]]]:
    result = []
    for shot in shots:
        issues = check_shot(shot)
        if issues:
            result.append((shot, issues))
    return result
