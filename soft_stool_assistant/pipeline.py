"""软便 Pipeline（v2 Pattern 逻辑 + 真实数据库）。

保留 v2 的核心设计：

一级 Pattern 只按「有没有基础饮食」分流
    有 → with_baseline_diet
    无 → without_baseline_diet
脂肪高 / 蛋白变化等只作为「当前分析结果」，不做成 Pattern。

每轮固定做：
    补信息 → 查本地数据 → 做当前总结 → 发问一个「业务问题组」

相比 v2 的三处改动（都是为了接真实库）：
1. `load_baseline_context` 支持 `diet_override`，走 `tools.build_baseline_diet_profile`；
2. 新增 `prefill_slots_from_history`：首轮用库里已有的换粮记录预填槽位
   （数据缺失时自动跳过，退回纯对话补全）；
3. `build_current_summary` / `build_final_assessment` 接入真实库的
   软便复合风险、肠胃友好功能分。
"""

from copy import deepcopy

from .tools import (
    build_baseline_diet_profile,
    get_product_by_name,
    compare_diets,
    build_manual_old_new_compare,
    get_recent_diet_change,
    get_pet_profile,
)
from .llm import extract_user_info, generate_response_from_plan, generate_two_stage_response


PATTERN_WITH_BASELINE = "with_baseline_diet"
PATTERN_WITHOUT_BASELINE = "without_baseline_diet"


def _compact_names(names: list, max_chars: int = 12) -> str:
    """压缩档案名称，确保整句追问不超过30字。"""
    result = ""
    for raw in names:
        name = str(raw).strip()
        if not name:
            continue
        candidate = f"{result}、{name}" if result else name
        if len(candidate) > max_chars:
            break
        result = candidate
    if result:
        return result
    return str(names[0]).strip()[:max_chars] if names else ""


def new_state(pet_id: str) -> dict:
    return {
        "pet_id": pet_id,
        "pipeline": "soft_stool",
        "turn": 0,

        "pattern": None,

        "context": {
            "baseline_loaded": False,
            "baseline_diet": None,
            "diet_override": None,
            "prefill": True,
            "prefilled": False,
            "prefill_source": None,
            "followup_context_loaded": False,
            "history_food_names": [],
            "disease_names": [],
            "active_question_group": None,
        },

        "slots": {
            # 饮食变化组
            "recent_diet_change": None,
            "change_type": None,
            "old_product": None,
            "new_product": None,
            "change_ratio": None,
            "change_days": None,

            # 症状组
            "soft_stool_days": None,

            # 风险组
            "vomiting": None,
            "blood_in_stool": None,
            "appetite": None,
            "energy": None,

            # 自然语言追加询问
            "history_foods_reviewed": None,
            "disease_history_reviewed": None,
        },

        "analysis": {
            "diet_compare": None,
            "current_summary": None,
        },
    }


def update_slots(state: dict, extracted: dict):
    for key, value in extracted.items():
        if key in state["slots"] and value is not None:
            state["slots"][key] = value


# =========================================================
# 1. 基础饮食 + 一级分流
# =========================================================
def load_baseline_context(state: dict):
    if state["context"]["baseline_loaded"]:
        return

    baseline = build_baseline_diet_profile(
        state["pet_id"],
        override=state["context"].get("diet_override"),
    )
    state["context"]["baseline_diet"] = baseline
    state["context"]["baseline_loaded"] = True

    state["pattern"] = (
        PATTERN_WITH_BASELINE if baseline["has_diet_data"] else PATTERN_WITHOUT_BASELINE
    )


# =========================================================
# 2. 用历史记录预填（真实库补充，拿不到就跳过）
# =========================================================
def prefill_slots_from_history(state: dict):
    """首轮尝试从库里还原「最近饮食变化」并预填槽位。"""
    if state["context"].get("prefilled"):
        return
    state["context"]["prefilled"] = True

    if not state["context"].get("prefill", True):
        return

    try:
        info = get_recent_diet_change(state["pet_id"])
    except Exception:
        return

    if not info.get("found"):
        return

    slots = state["slots"]
    filled = []

    if info.get("old_product"):
        slots["old_product"] = info["old_product"]
        filled.append("old_product")
    if info.get("new_product"):
        slots["new_product"] = info["new_product"]
        filled.append("new_product")
    if info.get("change_days") is not None:
        slots["change_days"] = info["change_days"]
        filled.append("change_days")
    if info.get("new_product_ref"):
        slots["new_product"] = slots["new_product"] or info["new_product_ref"]

    if filled:
        slots["recent_diet_change"] = True
        slots["change_type"] = slots["change_type"] or "main_food_change"
        state["context"]["prefill_source"] = "miniprogram_food_change_intent"


def load_followup_context(state: dict):
    """读取当前宠物的历史主粮和疾病档案，供自然语言追问使用。"""
    context = state["context"]
    if context.get("followup_context_loaded"):
        return
    context["followup_context_loaded"] = True

    foods = []
    baseline = context.get("baseline_diet") or {}
    for product in baseline.get("products") or []:
        name = product.get("display_name") or product.get("name")
        if name and name not in foods:
            foods.append(name)
    try:
        change = get_recent_diet_change(state["pet_id"])
        if change.get("found"):
            for name in (change.get("old_product"), change.get("new_product")):
                if name and name not in foods:
                    foods.append(name)
    except Exception:
        pass
    context["history_food_names"] = foods

    try:
        profile = get_pet_profile(state["pet_id"])
        context["disease_names"] = [
            str(name).strip() for name in (profile.get("diseases") or [])
            if str(name).strip()
        ]
    except Exception:
        context["disease_names"] = []


# =========================================================
# 3. 当前总结
# =========================================================
def summarize_baseline_diet(state: dict) -> str:
    baseline = state["context"]["baseline_diet"]

    if not baseline["has_diet_data"]:
        return (
            "我现在还没有看到这只猫平时的基础饮食记录，"
            "所以暂时没法从历史食谱判断这次软便和原有饮食有没有关系。"
        )

    summary = baseline.get("nutrition_summary")
    products = baseline["products"]

    if len(products) == 1:
        food_desc = f"目前记录里主要吃的是 {products[0].get('display_name') or products[0]['name']}"
    else:
        names = "、".join(p.get("display_name") or p["name"] for p in products)
        food_desc = f"目前记录里是混合饮食，主要包括 {names}"

    if not summary:
        evidence = []
        detailed = baseline.get("detailed_products") or []
        protein_sources = sorted({
            source for product in detailed for source in (product.get("protein_sources") or [])
        })
        fat_sources = sorted({
            source for product in detailed for source in (product.get("fat_sources") or [])
        })
        if protein_sources:
            evidence.append("主要蛋白来源包括" + "、".join(protein_sources))
        if fat_sources:
            evidence.append("脂肪来源包括" + "、".join(fat_sources))

        scored = next((
            p for p in detailed if isinstance(p.get("gut_friendly_score"), (int, float))
        ), None)
        if scored:
            evidence.append(f"数据库中的肠胃友好评分为 {scored['gut_friendly_score']:.1f}")

        risk_product = next((p for p in detailed if (p.get("soft_stool_risk") or {}).get("risk_level")), None)
        if risk_product:
            risk = risk_product["soft_stool_risk"]
            risk_text = f"软便风险辅助模型标记为「{risk['risk_level']}」"
            if risk.get("risk_index") is not None:
                risk_text += f"（风险指数 {risk['risk_index']}）"
            evidence.append(risk_text + "，这只用于提示多留意，不代表产品一定会引起软便")

        mechanism_product = next((p for p in detailed if p.get("soft_stool_mechanisms")), None)
        if mechanism_product:
            analysis = mechanism_product["soft_stool_mechanisms"]
            primary = [
                item["mechanism"] for item in analysis.get("mechanisms", [])
                if item.get("role") == "primary"
            ]
            secondary = [
                item["mechanism"] for item in analysis.get("mechanisms", [])
                if item.get("role") == "secondary"
            ]
            amplifiers = [item["mechanism"] for item in analysis.get("amplifiers", [])]
            if primary:
                evidence.append("相对值模型显示" + "和".join(primary) + "共同构成主要关注机制")
            if secondary:
                evidence.append(secondary[0] + "属于次要观察机制")
            if amplifiers:
                evidence.append("同时存在" + "、".join(amplifiers) + "，可能放大软便表现")
            if analysis.get("confidence") == "limited":
                evidence.append(f"当前历史参考池只有 {analysis.get('reference_pool_size')} 个产品，结论需要保留不确定性")

        text = (
            f"我先看了一下历史饮食，{food_desc}。"
            "目前库里的蛋白、脂肪和粗纤维保证值还不完整，暂时不能精确计算营养变化。"
        )
        if evidence:
            text += "不过现有数据仍能提供一些参考：" + "；".join(evidence) + "。"
        return text

    basis = summary.get("nutrition_basis")
    basis_text = f"（{basis}口径）" if basis else ""

    return (
        f"我先看了一下历史饮食，{food_desc}。"
        f"按当前记录汇总，蛋白约 {summary['protein']}%，脂肪约 {summary['fat']}%，"
        f"粗纤维约 {summary['fiber']}%{basis_text}。"
        "这些是后面判断最近变化是否过大的基础参照。"
    )


def build_current_summary(state: dict) -> str:
    """每轮都先总结「现在已经知道什么」，再继续发问。"""
    slots = state["slots"]
    baseline = state["context"]["baseline_diet"]
    compare = state["analysis"].get("diet_compare")

    parts = []

    # 第一层：基础饮食
    if state["turn"] == 1:
        parts.append(summarize_baseline_diet(state))

    # 第二层：近期饮食变化
    if slots["recent_diet_change"] is False:
        parts.append("目前没有记录到近期明确的换粮、加罐头、零食或其他新食物变化。")

    if slots["recent_diet_change"] is True:
        desc = "目前可以确认近期有饮食变化"
        if slots["change_type"] == "main_food_change":
            desc += "，主要是换粮"
        elif slots["change_type"] == "wet_food_added":
            desc += "，主要是增加了罐头"
        elif slots["change_type"] == "snack_added":
            desc += "，主要是增加了零食"

        if slots["change_ratio"] is not None:
            desc += f"，变化比例大约 {int(slots['change_ratio'] * 100)}%"

        if slots["change_days"] is not None:
            desc += f"，大约从 {slots['change_days']} 天前开始"

        parts.append(desc + "。")

    # 第三层：营养 + 风险对比
    if compare:
        delta = compare["delta"]
        new_name = compare["new_product"].get("display_name") or compare["new_product"]["name"]

        detail = (
            f"我把当前基础饮食和 {new_name} 做了对比："
            f"蛋白变化 {delta['protein']:+.1f} 个百分点，"
            f"脂肪变化 {delta['fat']:+.1f} 个百分点，"
            f"粗纤维变化 {delta['fiber']:+.1f} 个百分点。"
        )

        if compare.get("protein_source_changed"):
            detail += " 主要蛋白来源也发生了变化。"

        risk = compare.get("new_soft_stool_risk") or {}
        if risk.get("risk_level"):
            detail += f" 另外这只新粮在软便复合风险模型里的档位是「{risk['risk_level']}」"
            if risk.get("risk_index") is not None:
                detail += f"（风险指数 {risk['risk_index']}）"
            detail += "。"

        gut = compare.get("new_gut_friendly_score")
        if isinstance(gut, (int, float)):
            detail += f" 它的「肠胃友好」功能性评分为 {gut:.1f}。"

        parts.append(detail)

    if baseline.get("notes"):
        parts.append("（数据提示：" + "；".join(baseline["notes"]) + "）")

    # 第四层：风险表现
    risk_signals = []
    if slots["vomiting"] is True:
        risk_signals.append("有呕吐")
    if slots["blood_in_stool"] is True:
        risk_signals.append("有便血")
    if slots["appetite"] == "poor":
        risk_signals.append("食欲下降")
    if slots["energy"] == "poor":
        risk_signals.append("精神变差")

    if risk_signals:
        parts.append("另外目前还有：" + "、".join(risk_signals) + "。")

    return " ".join(parts).strip()


def _has_urgent_risk(state: dict) -> bool:
    slots = state.get("slots") or {}
    return bool(
        slots.get("vomiting") is True
        or slots.get("blood_in_stool") is True
        or slots.get("appetite") == "poor"
        or slots.get("energy") == "poor"
    )


# =========================================================
# 4. 按业务问题组发问
# =========================================================
def choose_next_question_group(state: dict):
    """
    槽位按业务问题组收集，不是一槽一问。
    返回：(group_name, question)
    """
    slots = state["slots"]

    # 先展示查询结果，再用短句收集遗漏信息。只问主粮，不使用按钮。
    if slots["history_foods_reviewed"] is None:
        foods = state["context"].get("history_food_names") or []
        if foods:
            names = _compact_names(foods)
            return "history_food_group", f"记录显示吃过{names}，最近还吃过哪些主粮？"
        return "history_food_group", "没查到饮食记录，最近吃过哪些主粮？"

    if slots["disease_history_reviewed"] is None:
        diseases = state["context"].get("disease_names") or []
        if diseases:
            names = _compact_names(diseases)
            return "disease_history_group", f"记录显示有{names}，还得过其他疾病吗？"
        return "disease_history_group", "没查到疾病记录，以前得过其他疾病吗？"

    # 1. 先补「近期饮食变化组」
    if slots["recent_diet_change"] is None:
        if state["pattern"] == PATTERN_WITH_BASELINE:
            return (
                "recent_diet_change_group",
                "最近换过主粮吗？如果换过，现在吃什么主粮？",
            )
        return (
            "recent_diet_change_group",
            "最近换过主粮吗？如果换过，原来和现在吃什么主粮？",
        )

    # 2. 已确认有变化，但关键信息没收齐
    if slots["recent_diet_change"] is True:
        missing = []

        if state["pattern"] == PATTERN_WITHOUT_BASELINE and not slots["old_product"]:
            missing.append("原来主要吃什么")

        if not slots["new_product"] and slots["change_type"] in ("main_food_change", None):
            missing.append("现在换成什么")

        if slots["change_days"] is None:
            missing.append("大概从什么时候开始")

        if slots["change_ratio"] is None:
            missing.append("现在大概占多少比例")

        if missing:
            return (
                "diet_change_detail_group",
                "饮食变化这部分我还差一点信息：" + "、".join(missing) + "？",
            )

    # 3. 症状表现组
    symptom_missing = []
    if slots["soft_stool_days"] is None:
        symptom_missing.append("软便大概持续多久")
    if slots["vomiting"] is None:
        symptom_missing.append("有没有呕吐")
    if slots["blood_in_stool"] is None:
        symptom_missing.append("有没有便血或明显黏液")

    if symptom_missing:
        return (
            "symptom_group",
            "再确认一下这次软便本身：" + "、".join(symptom_missing) + "？",
        )

    # 4. 状态风险组
    risk_missing = []
    if slots["appetite"] is None:
        risk_missing.append("食欲有没有明显下降")
    if slots["energy"] is None:
        risk_missing.append("精神状态有没有变差")

    if risk_missing:
        return (
            "risk_group",
            "最后再看一下整体状态：" + "、".join(risk_missing) + "？",
        )

    return (None, None)


# =========================================================
# 5. 按需调用 Tool
# =========================================================
def run_required_tools(state: dict):
    """根据当前槽位决定是否调用更深一层 Tool。"""
    slots = state["slots"]
    baseline = state["context"]["baseline_diet"]

    # Path A：有基础饮食 + 用户说发生换粮 + 已知新产品
    if state["pattern"] == PATTERN_WITH_BASELINE:
        if slots["recent_diet_change"] is True and slots["new_product"]:
            new_product = get_product_by_name(slots["new_product"])
            if new_product:
                state["analysis"]["diet_compare"] = compare_diets(
                    baseline_profile=baseline,
                    new_product=new_product,
                )

    # Path B：无基础饮食，但用户把原粮和新粮都补齐
    if state["pattern"] == PATTERN_WITHOUT_BASELINE:
        if slots["recent_diet_change"] is True and slots["old_product"] and slots["new_product"]:
            state["analysis"]["diet_compare"] = (
                build_manual_old_new_compare(
                    old_product_name=slots["old_product"],
                    new_product_name=slots["new_product"],
                ).get("compare") or None
            )


# =========================================================
# 6. 当前阶段结论
# =========================================================
def build_final_assessment(state: dict) -> str:
    """只做「相关性判断」，不做确诊。"""
    slots = state["slots"]
    compare = state["analysis"].get("diet_compare")

    # 风险优先
    if (
        slots["vomiting"] is True
        or slots["blood_in_stool"] is True
        or slots["appetite"] == "poor"
        or slots["energy"] == "poor"
    ):
        return (
            "现在的信息里已经出现了需要提高警惕的伴随表现，"
            "这时候不建议只按换粮或饮食适应来解释。"
            "更稳妥的是尽快联系宠物医院进一步评估。"
        )

    # 有明确饮食变化
    if slots["recent_diet_change"] is True:
        evidence = []

        if slots["change_ratio"] is not None:
            evidence.append(f"饮食变化比例约 {int(slots['change_ratio'] * 100)}%")

        if slots["change_days"] is not None:
            evidence.append(f"变化发生在约 {slots['change_days']} 天前")

        if compare:
            delta = compare["delta"]
            if abs(delta["fat"]) >= 3:
                evidence.append(f"脂肪变化 {delta['fat']:+.1f} 个百分点")
            if abs(delta["protein"]) >= 5:
                evidence.append(f"蛋白变化 {delta['protein']:+.1f} 个百分点")
            if compare.get("protein_source_changed"):
                evidence.append("蛋白来源也发生了变化")

            risk = compare.get("new_soft_stool_risk") or {}
            if risk.get("risk_level") in ("高风险", "极高风险"):
                evidence.append(f"新粮在软便复合风险模型里是「{risk['risk_level']}」档")

            gut = compare.get("new_gut_friendly_score")
            if isinstance(gut, (int, float)) and gut < 40:
                evidence.append(f"新粮「肠胃友好」功能性评分偏低（{gut:.1f}）")

        if evidence:
            return (
                "结合目前信息，近期饮食变化和这次软便的时间关系值得优先关注。"
                "当前看到的主要变化包括：" + "；".join(evidence) + "。"
                "第一步更适合先把饮食变化放缓，并继续观察便便、食欲和精神状态。"
                "如果软便持续加重，或出现血便、明显呕吐、精神差、不吃东西，就不要只靠调整换粮。"
            )

        return (
            "目前可以确认近期有饮食变化，但产品和比例信息还不足以判断变化幅度。"
            "现阶段更适合先按饮食变化相关因素继续观察。"
        )

    # 无饮食变化
    return (
        "目前没有看到明确的近期饮食变化，因此这次软便暂时不能优先归因到换粮。"
        "后续更需要结合持续时间、便便变化以及其他健康表现来判断。"
    )


# =========================================================
# 单轮入口
# =========================================================
def run_soft_stool_turn(
    pet_id: str,
    user_input: str,
    state: dict = None,
    diet_override=None,
    prefill: bool = True,
) -> dict:
    """
    每一轮固定执行：
    1. 读取/保持基础饮食（并完成一级分流）
    2. 用历史记录预填饮食变化槽位
    3. LLM 抽取本轮信息
    4. 更新槽位
    5. 根据当前状态调用 Tool
    6. 做一轮当前总结
    7. 选择下一业务问题组；没有缺失槽位时，给当前阶段结论
    """
    if state is None:
        state = new_state(pet_id)
        state["context"]["diet_override"] = diet_override
        state["context"]["prefill"] = prefill

    state = deepcopy(state)
    # 兼容功能上线前已经保存在会话中的旧版 state。
    context = state.setdefault("context", {})
    for key, default in (
        ("followup_context_loaded", False), ("history_food_names", []),
        ("disease_names", []), ("active_question_group", None),
    ):
        context.setdefault(key, default)
    slots = state.setdefault("slots", {})
    slots.setdefault("history_foods_reviewed", None)
    slots.setdefault("disease_history_reviewed", None)
    state["turn"] += 1

    # 1. 首轮加载基础饮食并完成一级分流
    load_baseline_context(state)

    # 1.1 查询历史主粮与疾病档案，供后续自然语言追问
    load_followup_context(state)

    # 2. 历史记录预填
    prefill_slots_from_history(state)

    # 用户对上一轮自然语言追问作答后，即视为已补充；原文保留在槽位中。
    active_group = state["context"].get("active_question_group")
    if user_input and active_group == "history_food_group":
        state["slots"]["history_foods_reviewed"] = user_input.strip()
    elif user_input and active_group == "disease_history_group":
        state["slots"]["disease_history_reviewed"] = user_input.strip()

    # 3. LLM 抽取
    extracted = extract_user_info(user_input, state)

    # 4. 更新槽位
    update_slots(state, extracted)

    # 5. 根据最新槽位调用 Tool
    run_required_tools(state)

    # 6. 当前总结
    current_summary = build_current_summary(state)
    state["analysis"]["current_summary"] = current_summary

    # 7. 下一槽位组
    group_name, question = choose_next_question_group(state)
    state["context"]["active_question_group"] = group_name

    # Risk signals take precedence over product-mechanism explanation.
    if _has_urgent_risk(state):
        assessment = build_final_assessment(state)
        reply = generate_response_from_plan(state, current_summary, assessment)
        return {
            "status": "answered",
            "reply": reply,
            "reply_parts": [{"response_type": "risk_alert", "reply": reply}],
            "pattern": state["pattern"],
            "next_group": None,
            "state": state,
        }

    if question:
        if state["turn"] == 1 and (state["context"]["baseline_diet"].get("detailed_products") or []):
            reply_parts = generate_two_stage_response(
                state=state,
                evidence_summary=summarize_baseline_diet(state),
                current_summary=current_summary,
                question=question,
            )
            reply = "\n\n".join(part["reply"] for part in reply_parts)
        else:
            reply = generate_response_from_plan(state, current_summary, question)
            reply_parts = [{"response_type": "text", "reply": reply}]
        return {
            "status": "need_more_info",
            "reply": reply,
            "reply_parts": reply_parts,
            "pattern": state["pattern"],
            "next_group": group_name,
            "state": state,
        }

    # 信息够了，给一轮结论
    assessment = build_final_assessment(state)
    reply = generate_response_from_plan(
        state=state,
        current_summary=current_summary,
        question=assessment,
    )

    return {
        "status": "answered",
        "reply": reply,
        "reply_parts": [{"response_type": "result_card", "reply": reply}],
        "pattern": state["pattern"],
        "next_group": None,
        "state": state,
    }


# v1 命名兼容
soft_stool_pipeline = run_soft_stool_turn
