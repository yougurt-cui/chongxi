"""Deterministic multi-label router for comment pipelines."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

CHOICE_STRONG_RE = re.compile(
    r"选.{0,8}粮|粮.{0,8}选|换粮|换成|换到|改喂|想换|打算换|准备换|"
    r"求推荐|推荐.{0,8}粮|哪款|哪个牌子|什么牌子|二选一|对比|平替|"
    r"性价比|预算|价格|便宜|贵|哪里买|正品|国产|进口|避雷|踩雷",
    re.IGNORECASE,
)
PREFERENCE_ATTRIBUTE_RE = re.compile(
    r"材质|毛绒|塑料|硅胶|木质|纸板|造型|形状|大小|尺寸|太大|太小|结构|"
    r"功能|自动|漏食|耐用|结实|容易坏|坏了|开裂|线头|玩具掉毛|毛绒掉毛|互动|声音|噪音|味道",
    re.IGNORECASE,
)
PREFERENCE_REACTION_RE = re.compile(
    # 不含"爱吃/不吃"等进食词：进食反馈属于 choice 的食品语义，不是玩具偏好证据。
    r"喜欢|不喜欢|爱玩|不玩|愿意玩|不愿意|接受|排斥|感兴趣|没兴趣|"
    r"抱着|蹬|追|扑|咬|(?<!猫)抓|舔|闻了就走|主动|上头",
    re.IGNORECASE,
)
PET_CONTEXT_RE = re.compile(r"猫|猫咪|猫猫|小猫|喵|主子|毛孩子|宠物|狗|狗狗", re.IGNORECASE)

TOY_CONTEXT_RE = re.compile(
    r"宠物玩具|猫玩具|狗玩具|玩具|逗猫棒|轨道球|猫抓板|猫抓柱|猫爬架|猫隧道|"
    r"猫激光|激光笔|踢踢鱼|毛绒小鱼|猫薄荷球|磨牙玩具|漏食玩具",
    re.IGNORECASE,
)
FOOD_CONTEXT_RE = re.compile(
    r"猫粮|主粮|干粮|湿粮|冻干|风干粮|烘焙粮|处方粮|幼猫粮|成猫粮|"
    r"罐头|猫条|宠物零食|零食|粮|适口性",
    re.IGNORECASE,
)
FOOD_PREFERENCE_RE = re.compile(
    r"爱吃|不吃|不爱吃|喜欢吃|适口|挑食|闻了就走|颗粒|油腻|太油|不油|"
    r"味道|香|腥|软便|拉稀|便秘|呕吐|吐粮|黑下巴|泪痕|吃胖|长肉",
    re.IGNORECASE,
)
FOOD_EXPERIENCE_RE = re.compile(
    r"吃了|在吃|一直吃|吃过|喂了|一直喂|试吃|回购|效果|改善|稳定|"
    r"不错|挺好|不好|有问题|不适合|适合",
    re.IGNORECASE,
)
RESERVED_MEDICAL_RE = re.compile(r"药品|药物|处方药|保健品|营养补充剂|医疗器械|雾化器", re.IGNORECASE)
RESERVED_MONITOR_RE = re.compile(r"智能项圈|定位器|摄像头|监控设备|健康监测|饮水监测|体重秤|传感器", re.IGNORECASE)

@dataclass(frozen=True)
class RouteResult:
    choice: bool
    product_preference: bool
    router_source: str


def rule_route(
    text: str,
    brand: str = "",
    product_category: str = "",
    source_keyword: str = "",
    source_title: str = "",
    source_content: str = "",
) -> tuple[RouteResult, bool]:
    post_context = " ".join(filter(None, [source_title, source_content]))
    pet_context = bool(
        PET_CONTEXT_RE.search(text)
        or PET_CONTEXT_RE.search(source_keyword)
        or PET_CONTEXT_RE.search(post_context)
    )
    toy_in_text = bool(TOY_CONTEXT_RE.search(text))
    food_in_text = bool(FOOD_CONTEXT_RE.search(text))
    reserved_in_text = bool(RESERVED_MEDICAL_RE.search(text) or RESERVED_MONITOR_RE.search(text))

    # 产品领域只按三级优先级补足：评论正文 > 采集检索关键词 > 帖子标题/内容。
    # 低优先级上下文不能覆盖评论正文已经明确的产品类型。
    if toy_in_text or food_in_text or reserved_in_text:
        toy_context, food_context, reserved = toy_in_text, food_in_text, reserved_in_text
    else:
        toy_in_keyword = bool(TOY_CONTEXT_RE.search(source_keyword))
        food_in_keyword = bool(FOOD_CONTEXT_RE.search(source_keyword))
        reserved_in_keyword = bool(
            RESERVED_MEDICAL_RE.search(source_keyword) or RESERVED_MONITOR_RE.search(source_keyword)
        )
        if toy_in_keyword or food_in_keyword or reserved_in_keyword:
            toy_context, food_context, reserved = (
                toy_in_keyword, food_in_keyword, reserved_in_keyword,
            )
        else:
            toy_context = bool(TOY_CONTEXT_RE.search(post_context))
            food_context = bool(FOOD_CONTEXT_RE.search(post_context))
            reserved = bool(
                RESERVED_MEDICAL_RE.search(post_context) or RESERVED_MONITOR_RE.search(post_context)
            )

    if reserved:
        toy_context = False
        food_context = False
    # 品牌只能补足“未明确写猫粮”的食品语境；通用宠物品牌也可能销售玩具，
    # 因此明确出现玩具时不能仅凭品牌把评论送进 Choice。
    choice_context = bool(food_context or (brand and not toy_context))
    choice = bool(choice_context and (
        CHOICE_STRONG_RE.search(text) or FOOD_PREFERENCE_RE.search(text) or FOOD_EXPERIENCE_RE.search(text)
    ))
    preference_signal = bool(PREFERENCE_ATTRIBUTE_RE.search(text) or PREFERENCE_REACTION_RE.search(text))
    product_preference = toy_context and preference_signal
    result = RouteResult(choice, product_preference, "rule")
    if choice or product_preference or reserved:
        return result, False
    preference_context = toy_context or food_context
    clearly_irrelevant = not any((
        choice_context, preference_context, pet_context,
    ))
    return result, not clearly_irrelevant


def route_comment(comment: Any) -> RouteResult:
    result, _ambiguous = rule_route(
        comment.clean_text,
        comment.brand,
        comment.product_category,
        comment.source_keyword,
        comment.source_title,
        comment.source_content,
    )
    return result
