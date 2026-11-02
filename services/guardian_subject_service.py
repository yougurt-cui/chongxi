"""Resolve the subject of a guardian turn before any personalized workflow runs."""
import re

# Product names and supplies are not, by themselves, a change of animal.
SUPPLIES = re.compile(r'[猫狗犬](?:粮|砂|抓板|爬架|条|罐头|窝|笼|绳|碗|零食|用品)|逗猫棒')
CAT = re.compile(r'猫|喵星人|布偶|英短|美短')
DOG = re.compile(r'狗|犬|汪星人|金毛|柯基|泰迪|哈士奇|拉布拉多|比熊|博美')
OTHER = re.compile(r'(?:朋友|别人|邻居|同事)(?:家(?:的)?|的)(?:小|幼|成年)?(?:猫|狗|犬|宠物)|'
                   r'另(?:外)?一只|(?:别的|其他的?)(?:小|幼)?(?:猫|狗|犬|宠物)|'
                   r'(?:新来的|新到家的|刚接回来的)(?:小|幼)?(?:猫|狗|犬)|(?:想|打算|准备|考虑)(?:再)?(?:养|领养|接|收养)(?:一只|个)?|新(?:领养|收养|接)(?:来|回家)?的?')
PROFILE = re.compile(r'档案里|已绑定|当前(?:这|那)?只|我家(?:的)?(?:这只|那只|宠物|猫|狗|犬)')
PERSONAL = re.compile(r'记录|提醒|它.{0,8}(?:吃的粮|口粮|体重|状态|饮食建议)|(?:以前|历史|最近).{0,5}(?:吃|喝|体重)')
URGENT = re.compile(r'呼吸困难|无法呼吸|抽搐|昏迷|中毒|误食.{0,10}(?:药|毒|百合|巧克力)')


def subject_phrase(message):
    text = re.split(r'说回|回到|换回', message)[-1]
    # The final question can identify a different animal from the background clause.
    clauses = [c for c in re.split(r'[，,。；;！？!?]', text) if c.strip()]
    if clauses:
        last = clauses[-1]
        if re.search(r'怎么办|如何|怎么|注意|想问|请问|能不能|要不要|可以|^是', last):
            masked = SUPPLIES.sub('', last)
            if DOG.search(masked) or CAT.search(masked):
                return last
    return text


def species_in(message):
    phrase = subject_phrase(message)
    text = SUPPLIES.sub('', phrase)
    cat, dog = CAT.search(text), DOG.search(text)
    if cat or dog:
        return 'both' if cat and dog else 'cat' if cat else 'dog'
    # A request specifically for dog/cat food should not start the other species' diet flow.
    if '狗粮' in phrase or '犬粮' in phrase:
        return 'both' if '猫粮' in phrase else 'dog'
    if '猫粮' in phrase:
        return 'cat'
    for word, species in (('兔子', 'rabbit'), ('仓鼠', 'hamster'), ('鹦鹉', 'bird'), ('乌龟', 'turtle')):
        if word in text:
            return species
    return None


def resolve(message, conversation, previous):
    """None means the selected profile; a species string means advice without its records."""
    phrase = subject_phrase(message)
    species = species_in(phrase)
    profile = conversation.get('profile_species')
    if species == 'both':
        return 'both'
    masked = SUPPLIES.sub('', phrase)
    name = conversation.get('profile_name')
    named = bool(name and name in phrase)
    other_in_background = (phrase != message and OTHER.search(SUPPLIES.sub('', message))
                           and not PROFILE.search(phrase) and not named)
    if other_in_background or OTHER.search(masked) or re.search(r'(?:猫咪?|狗狗?|犬)(?:一般|通常|普遍)', phrase):
        return species or previous or 'unknown'
    # A named pet eating a different species' food is still the named pet.
    if named and not (CAT.search(masked) or DOG.search(masked)):
        return None
    if species and species != profile:
        return species
    if PROFILE.search(phrase) or named:
        return None
    if species and previous and previous != species:
        return None  # Explicit return to the selected species.
    return previous


def external_boundary(message, interaction, attachments):
    from services.guardian_workflow import requested_flow
    from soft_stool_assistant.llm import _extract_rule_info
    from soft_stool_assistant.policy import urgent_risk
    if URGENT.search(message) or urgent_risk(_extract_rule_info(message, {})):
        return 'risk_alert', '这只宠物出现了需要优先评估的情况，请尽快联系宠物医院并说明症状。'
    if (interaction or attachments or requested_flow(message) == 'reminder'
            or re.search(r'帮.{0,5}记录|历史口粮|吃粮记录|状态解读|(?:最近|之前|以前)吃的粮', message)):
        return 'profile_required', '这项操作需要对应宠物的档案。请先选择或建立它的档案，再继续操作。'
    return None
