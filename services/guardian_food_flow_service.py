"""Food selection -> comparison -> confirmed feeding, using existing chat UI."""
from __future__ import annotations

import re
from services.taobao_sku_import_service import list_standardized_product_options
from services.miniprogram_cat_profile_service import update_cat_profile
from services.guardian_food_analysis_service import load_nutrition, load_risks, analyse_food, nutrition_text, comparison_text
from services.guardian_product_portrait_service import load_product_portraits, compare_portraits, portrait_comparison_text
from services.guardian_factor_evidence_service import load_profiles as load_factor_profiles, compare_profiles as compare_factor_profiles, factor_comparison_text
from services.guardian_food_scenario import build_scenario
from services.guardian_recommendation_presentation import alternatives_requested

STATE_KEY = "_food_flow"


def compact(text):
    return re.sub(r"[\s，。！？、:：|]+", "", str(text or "")).lower()


def target_from_text(message, flow):
    # An intention or a negative statement is never a feeding confirmation.
    if re.search(r"没|未|不|准备|打算|计划|想要|如果|假如|是否|吗|？|\?", message):
        started = False
    else:
        started = bool(re.search(r"(?:已经|已|确认).{0,3}(?:开始吃|开始喂|开始换粮)|^开始吃了[。！!]*$", message))
    match = re.search(r"(?:换成|换到|选择|选定|目标粮(?:是|为)?|想试|考虑|已经开始吃|已开始吃)\s*([^，。！？?]+)", message)
    target = match.group(1).strip() if match else None
    if re.search(r'(?:不要|不用|别|暂时不|暂不|不)(?:再)?(?:换成|换到|选择|选定|选|考虑|想试)', message):
        return None, False
    if target and re.sub(r"[了吗呢呀]$", "", compact(target)) in {"", "这款", "这款粮", "它", "新粮", "目标粮"}:
        target = None
    if flow.get("stage") == "choose_target" and not target and message and not re.search(r"推荐|选粮|换粮|没有|不知道", message):
        target = message.strip()
    if not target:
        for candidate in flow.get("candidates") or []:
            if compact(message) in {compact(candidate.get("label")), compact(str(candidate.get("brand") or "") + str(candidate.get("product_name") or ""))}:
                target = candidate.get("catalog_key")
                break
        ordinal = re.search(r"(?:选|选择|换成)第?([一二三123])(?:款|个)", message)
        choices = flow.get("candidates") or []
        if ordinal:
            index = {"一": 0, "二": 1, "三": 2, "1": 0, "2": 1, "3": 2}[ordinal.group(1)]
            if index < len(choices):
                target = choices[index]["catalog_key"]
    elif re.fullmatch(r"第?[一二三123](?:款|个)", target):
        return target_from_text("选" + target, flow)
    return target, started


def resolve_target(name, options):
    value = compact(name)
    if not value:
        return []
    if str(name).startswith("formula:"):
        return [row for row in options if row.get("catalog_key") == name]
    # Keep every matching version; exact base name must not hide a 2.0 variant.
    return [row for row in options if any(value in compact(row.get(k)) for k in ("label", "product_name", "standard_product_name"))]


def card(text, flow, **result):
    return {"response_type": "result_card", "reply": text,
            "result": {"intent": "food_switch", "food_flow": flow, "food_candidates": [], **result},
            "disclaimer": "AI内容仅供参考，不能替代兽医诊断"}


def validate_explicit_target(item, pet):
    """Validate a product the user explicitly chose; missing catalogue fields are warnings, not rejection."""
    errors, warnings = [], []
    kind = str(item.get('product_type') or '').strip().lower()
    staple_types = {'dry', 'dry_food', 'dry food', '主粮', '干粮', '膨化粮', '全价猫粮',
                    '猫粮', '烘焙粮', '冻干', '冻干主粮', '湿粮'}
    non_staple_types = {'零食', '猫零食', '补充剂', '营养补充剂', '保健品'}
    if not kind:
        warnings.append('产品类型资料缺失，尚未核验是否为完整主粮，请查看包装标识')
    elif kind in non_staple_types:
        errors.append('该产品明确标记为零食或补充品，不能作为目标主粮')
    elif kind not in staple_types:
        warnings.append(f'产品类型“{item.get("product_type")}”尚未完成主粮属性核验，请查看包装标识')

    stage = str(item.get('life_stage') or '').strip().lower()
    all_stages = {'all', 'all_stages', 'all life stages', '全阶段', '全年龄', '全龄'}
    kitten = {'kitten', '幼猫', '幼年', '幼猫期'}
    adult = {'adult', '成猫', '成年', '成猫期'}
    senior = {'senior', '老年猫', '老年', '老年期'}
    known_stages = all_stages | kitten | adult | senior
    if not stage:
        warnings.append('适用阶段资料缺失，请核对包装说明')
    elif stage not in known_stages:
        warnings.append(f'适用阶段“{item.get("life_stage")}”尚未标准化，请核对包装说明')
    else:
        try:
            months = float(pet.get('age_months')) if pet.get('age_months') not in (None, '') else None
        except (TypeError, ValueError):
            months = None
        if months is not None and not (stage in all_stages
                or months < 12 and stage in kitten
                or 12 <= months < 84 and stage in adult
                or months >= 84 and stage in senior):
            errors.append('宠物年龄与产品明确标注的适用阶段不一致')

    ingredients = str(item.get('ingredient_composition') or '').strip()
    if not ingredients:
        warnings.append('配料表资料缺失，暂时不能进行配料对比')
    aliases = {'鸡肉': ('鸡', 'chicken'), '鸡': ('鸡', 'chicken'), '鱼': ('鱼', 'fish', 'salmon', 'tuna'),
               '牛肉': ('牛', 'beef'), '牛': ('牛', 'beef'), '乳制品': ('奶', '乳', 'milk'),
               '谷物': ('米', '麦', '玉米', 'rice', 'wheat', 'corn')}
    for allergen in pet.get('allergies') or []:
        tokens = aliases.get(allergen)
        if not tokens:
            errors.append(f'过敏原“{allergen}”尚无法与配料表可靠核对')
        elif not ingredients:
            errors.append(f'缺少配料表，无法排除过敏原“{allergen}”')
        elif any(token in ingredients.lower() for token in tokens):
            errors.append(f'配料表包含档案中的过敏原“{allergen}”')
    return errors, warnings


def handle_food_flow(user_id, conversation, message, state, context, interaction=None):
    slots = state["slots"]
    flow = slots.setdefault(STATE_KEY, {"stage": "selection"})
    scenario = build_scenario(slots)
    if re.search(r'先不换粮|暂时不换粮|不选这款|不要这款|取消选粮|取消换粮', message):
        state['current_step'] = None
        if flow.get('stage') in {'started', 'completed'}:
            flow['paused'] = True
            return card('好的，先暂停换粮安排。已经确认的吃粮记录会保留；你可以继续记录表现，或重新选粮。', flow)
        slots[STATE_KEY] = {'stage': 'selection'}
        slots.pop('target_food', None)
        return card('这次选择或换粮计划已取消，当前口粮没有改变。想继续时可以重新选粮。', slots[STATE_KEY])
    if scenario['status'] == 'blocked':
        return card(scenario['note'], flow, scenario=scenario)
    if interaction and interaction.get('slot') == 'food_flow_action':
        value = interaction.get('value')
        if value == 'switch_plan' and flow.get('target'):
            slots['_food_mode'] = 'switch'
            flow['stage'] = 'planned'
        elif value == 'restart':
            slots[STATE_KEY] = {'stage': 'selection'}
            slots.pop('target_food', None)
            return None
        else:
            raise ValueError('选粮操作无效，请重新选择')
    if re.search(r"(?:选|选择|换成)这款", message) and not flow.get("target"):
        candidates = flow.get("candidates") or []
        if len(candidates) == 1:
            interaction = {"slot": "target_food", "value": candidates[0]["catalog_key"]}
        elif candidates:
            flow["stage"] = "choose_target"
            return {"response_type": "text_or_select", "reply": "请确认你选的是哪一款：",
                    "interaction": {"slot": "target_food", "options": [{"label": r.get("label") or r["product_name"], "value": r["catalog_key"]} for r in candidates]},
                    "result": {"intent": "food_switch", "food_flow": flow}}
    target, started = target_from_text(message, flow)
    if interaction and interaction.get("slot") == "target_food":
        target = str(interaction.get("value") or "")
    if started and flow.get("stage") in {'planned', 'selected'} and not target:
        target = (flow.get("target") or {}).get("catalog_key")
    target = target or (slots.get("target_food") if not flow.get("target") else None)
    if re.search(r"重新(?:选|推荐)|再推荐|继续推荐|重新挑", message) or alternatives_requested(message):
        flow = {"stage": "selection"}
        slots[STATE_KEY] = flow
        slots.pop("target_food", None)
        return None
    if not target and not flow.get("target"):
        return None
    pet = context.get("pet_profile") or {}
    if pet.get("animal_type") not in (None, "", "cat", "猫"):
        return card("当前产品库是猫粮库，暂时没有可核验的狗粮资料。可以记录目标粮，但不能使用猫粮数据生成狗粮方案。", flow)
    if pet.get("diseases"):
        return card("已有疾病记录，目标粮和换粮安排需要先结合医生的饮食要求确认。", flow)
    if target:
        options = list_standardized_product_options(compare_available=True, limit=500).get("items") or []
        matches = resolve_target(target, options)
        if len(matches) != 1:
            flow.update(stage="choose_target", target_query=target)
            flow.pop("target", None)
            choices = [{"label": r.get("label") or r["product_name"], "value": r["catalog_key"]} for r in matches[:10]]
            state["current_step"] = "target_food"
            slots.pop("target_food", None)
            return {"response_type": "text_or_select", "reply": (
                "这款粮有多个版本，请确认具体配方：" if choices else
                "暂时没有找到这款粮的明确配方，请补充品牌和完整粮名；也可以说“重新推荐”继续选粮。"),
                "interaction": {"slot": "target_food", "options": choices},
                "result": {"intent": "food_switch", "food_flow": flow}}
        selected = matches[0]
        validation_errors, validation_warnings = validate_explicit_target(selected, pet)
        if validation_errors:
            flow.pop("target", None)
            slots.pop("target_food", None)
            return card('这款目标粮暂不能继续：' + '；'.join(validation_errors) + '。请确认资料或重新选粮。', flow)
        # Resolve afresh for every new target; never carry a prior target's start state.
        if (flow.get("target") or {}).get("catalog_key") != selected["catalog_key"]:
            flow = {"stage": 'selected' if slots.get('_food_mode') == 'selection' else "planned", "target": selected,
                    'candidates': flow.get('candidates') or [],
                    'reference_food_text': flow.get('reference_food_text'),
                    "original_food": context.get("current_food") or ({"product_name": slots["current_food"]} if slots.get("current_food") else None),
                    "original_profile_food": context.get("current_food"),
                    "original_formula_id": pet.get("food_formula_id")}
            slots[STATE_KEY] = flow
        else:
            flow["target"] = selected
        flow['validation_warnings'] = validation_warnings
        slots["target_food"] = selected.get("label") or selected["product_name"]
    selected = flow["target"]
    validation_errors, validation_warnings = validate_explicit_target(selected, pet)
    flow['validation_warnings'] = validation_warnings
    if validation_errors:
        return card('目标粮校验未通过：' + '；'.join(validation_errors) + '。请重新核对目标粮，或回复“重新推荐”。', flow)
    state["current_step"] = None
    if flow['stage'] == 'selected' and (started or slots.get('_food_mode') == 'switch'):
        flow['stage'] = 'planned'
    # Existing profile service owns authentication, canonical formula snapshot,
    # and the transaction that archives the previous food. No intention writes.
    if started and flow["stage"] == "planned":
        if not conversation.get("pet_id"):
            return card("请先绑定宠物，再保存已经开始吃的口粮。当前换粮计划已保留。", flow)
        already_saved = pet.get("food_formula_id") == selected["formula_id"]
        original_profile = flow.get("original_profile_food", flow.get("original_food")) or {}
        if not already_saved and (compact(pet.get("food_formula_id")) != compact(flow.get("original_formula_id")) or (context.get("current_food") or {}) != original_profile):
            return card("当前口粮档案已经变化，请重新确认原粮和目标粮后再保存。", flow)
        if not already_saved:
            update_cat_profile(user_id, conversation["pet_id"], {"food_product_id": selected["product_id"], "food_formula_id": selected["formula_id"]},
                               expected_food={"food_brand": original_profile.get("brand"),
                                              "food_product": original_profile.get("product_name"),
                                              "food_formula_id": flow.get("original_formula_id")})
        flow.update(stage="started", confirmation="用户确认已经开始吃", start_date=None)
    if flow["stage"] in {"started", "completed"}:
        if re.search(r"(?:已经|已|完全).{0,3}(?:换好|换完)|^换好了", message) and not re.search(r"没|未|不|想|计划|吗|？", message):
            flow["stage"] = "completed"
        if message and not started:
            flow["latest_feedback"] = {"text": message[:1000], "turn": state["turn_count"]}
        return card(("已记录换粮完成。" if flow["stage"] == "completed" else "已记录实际开始吃目标粮，并保存当前口粮及原粮历史。") +
                    "接下来可以告诉我食欲、精神和便便变化；如出现软便，可直接继续咨询。未提供的实际开始日期不会补填。", flow)
    original = flow.get("original_food") or {}
    original_label = " ".join(filter(None, [original.get("brand"), original.get("product_name")])) or "原粮未确认"
    # Compare real ingredient text only. No inferred nutrients or causal claims.
    old_matches = resolve_target(f"formula:{flow['original_formula_id']}", options) if target and flow.get("original_formula_id") else []
    if target and not old_matches and original_label != "原粮未确认":
        old_matches = resolve_target(original_label, options)
    if len(old_matches) == 1 and not flow.get('original_formula_id'):
        # Resolve the consultation-only reference without mutating the pet profile.
        flow['comparison_formula_id'] = old_matches[0]['formula_id']
    old_ingredients = old_matches[0].get("ingredient_composition") if len(old_matches) == 1 else (flow.get("comparison") or {}).get("original_ingredients")
    comparison = {"original_food": original_label, "target_food": selected.get("label"),
                  "original_ingredients": old_ingredients,
                  "target_ingredients": selected.get("ingredient_composition") or None,
                  "note": "配料仅作对照，不证明适口性、耐受或症状原因；缺失资料不推算。"}
    candidate = next((row for row in flow.get('candidates', []) if row.get('formula_id') == selected['formula_id']), {})
    detail = candidate.get('recommendation_detail') or {}
    full_cautions = list(dict.fromkeys((detail.get('cautions') or []) + (candidate.get('cautions') or [])))
    comparison_id = flow.get('original_formula_id') or flow.get('comparison_formula_id') or ((candidate.get('analysis') or {}).get('comparison_current_formula_id') if flow.get('reference_food_text') == slots.get('current_food') else None)
    nutrients = load_nutrition([comparison_id, selected['formula_id']])
    # Both formulas, not only the target: a comparison needs the original's
    # risk tags to say anything about what actually differs.
    risks = load_risks([comparison_id, selected['formula_id']])
    analysis = analyse_food(selected, nutrients, cautions=full_cautions, current=comparison_id, risks=risks)
    comparison['nutrition'] = analysis['comparison']
    portraits = load_product_portraits([comparison_id, selected['formula_id']])
    comparison['portrait'] = compare_portraits(portraits.get(comparison_id), portraits.get(selected['formula_id']))
    factor_profiles = load_factor_profiles([comparison_id, selected['formula_id']])
    comparison['factor_evidence'] = compare_factor_profiles(factor_profiles.get(comparison_id),
                                                            factor_profiles.get(selected['formula_id']))
    comparison['factor_current'] = factor_profiles.get(comparison_id)
    comparison['factor_target'] = factor_profiles.get(selected['formula_id'])
    analysis['product_portrait'] = portraits.get(selected['formula_id'])
    analysis['portrait_comparison'] = comparison['portrait']
    analysis['risk_models_current'] = (risks or {}).get(comparison_id) if comparison_id else None
    flow["comparison"] = comparison

    def factor_ready():
        factor = comparison['factor_evidence']
        return (factor.get('status') in {'ready', 'partial'}
                and bool(factor.get('only_current') or factor.get('only_target') or factor.get('shared')))

    def comparison_body():
        """Factor chain when risk tags exist, five-dimension fallback otherwise."""
        if factor_ready():
            return '\n' + factor_comparison_text(comparison['factor_evidence'],
                                                 comparison.get('factor_current'), comparison.get('factor_target'))
        return '\n' + portrait_comparison_text(comparison['portrait'])
    if flow['stage'] == 'selected':
        text = f"已选定：{selected.get('label') or selected['product_name']}。当前口粮没有改变。\n" + nutrition_text(analysis)
        text += '\n' + comparison_text(analysis)
        text += comparison_body()
        if candidate.get('strengths'):
            text += '\n适配理由：' + '；'.join(candidate['strengths'][:3])
        else:
            text += '\n这款粮尚未按你的诉求与其他产品排序，不能据此认定它最适合。'
        if candidate.get('current_food_reasons'):
            text += '\n' + candidate['current_food_reasons'][0]
        if candidate.get('history_reasons'):
            text += '\n' + candidate['history_reasons'][0]
        text += '\n需留意：' + '；'.join(full_cautions or [analysis['risk_note']])
        if flow.get('validation_warnings'):
            text += '\n资料提示：' + '；'.join(flow['validation_warnings']) + '。'
        if pet.get('age_months') is None:
            text += '\n年龄未填写，本次未按年龄筛选；请核对包装适用阶段：' + str(selected.get('life_stage') or '暂无资料') + '。'
        # The factor chain already states both risk levels per symptom.
        if not factor_ready():
            for risk in analysis.get('risk_models', []):
                text += '\n' + ('软便' if risk['kind'] == 'soft_stool' else '黑下巴') + '结构风险参考：' + str(risk.get('relative_risk_level') or '等级暂无') + '（非患病概率）。'
        text += '\n确定要换到这款时，可以继续查看换粮安排；已经实际开始吃，也可以直接告诉我。'
        return {**card(text, flow, food_analysis=analysis, food_comparison=comparison), 'response_type': 'text_or_select',
                'interaction': {'slot': 'food_flow_action', 'options': [{'label': '看看怎么换粮', 'value': 'switch_plan'}, {'label': '重新选粮', 'value': 'restart'}]}}
    plan = {"steps": ["确认目标主粮适用阶段及包装喂食说明。", "保留原粮，逐步引入目标粮，期间避免同时增加其他新食物。",
                       "记录食欲、精神、便便形态及呕吐情况；出现异常先暂停推进并联系兽医。"],
            "confirmation": "已经开始吃后，请回复“已经开始吃这款”；完全换好后可以回复“已经换好了”。"}
    text = f"原粮：{original_label}；目标：{selected.get('label') or selected['product_name']}。\n"
    if old_ingredients:
        text += f"原粮配料：{old_ingredients[:220]}\n"
    else:
        text += "原粮配方资料未确认，暂时无法完成双方配料对比。\n"
    text += f"目标配料：{(selected.get('ingredient_composition') or '暂无资料')[:220]}\n"
    text += comparison["note"] + "\n" + "\n".join(plan["steps"]) + "\n" + plan["confirmation"]
    text += '\n' + nutrition_text(analysis) + '\n' + analysis['risk_note']
    text += '\n' + comparison_text(analysis)
    text += comparison_body()
    if flow.get('validation_warnings'):
        text += '\n资料提示：' + '；'.join(flow['validation_warnings']) + '。'
    if pet.get("age_months") is None:
        stage = selected.get("life_stage")
        stage_label = {"adult": "成猫", "kitten": "幼猫", "senior": "老年猫", "all": "全阶段", "all_stages": "全阶段"}.get(stage, stage or "资料未提供")
        text += "\n年龄未填写，本次未按年龄筛选，请核对目标粮适用阶段：" + stage_label + "。"
    return card(text, flow, food_comparison=comparison, food_analysis=analysis, switch_plan=plan)
