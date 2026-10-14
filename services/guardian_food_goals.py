"""Product goals and fallback reference profiles, configurable independently of UI."""
import re

GOALS = {
    "weight_loss": {"label": "减重", "pattern": r"减重|减肥|控重|weight_loss"},
    "weight_gain": {"label": "增重", "pattern": r"增重|长肉|weight_gain"},
    "coat_health": {"label": "美毛护肤", "pattern": r"美毛|毛发|掉毛|护肤|coat_health"},
    "palatability": {"label": "适口性", "pattern": r"适口性|挑食|爱吃|palatability"},
    "soft_stool": {"label": "肠道友好", "pattern": r"digestive|肠胃|肠道|软便|腹泻|拉稀|soft_stool"},
    "black_chin": {"label": "黑下巴友好", "pattern": r"黑下巴|下巴出油|black_chin"},
    "tear_stain": {"label": "泪痕", "pattern": r"泪痕|tear_stain"},
    "general": {"label": "日常适配", "pattern": r"日常|均衡|单纯|一般|随便|无特别|没有特别|general"},
}

# Business reference scores, not clinical outcomes. Database profiles override.
REFERENCE_PROFILES = {
    "coat_health": {"profile_code": "guardian_coat_reference", "profile_name": "美毛护肤参考",
                    "symptom_type": "coat_health", "weights": {"protein_quality": .35, "fat_regulation_score": .30,
                    "skin_protection": .25, "antioxidant_score": .10}, "thresholds": {}, "avoid_rules": []},
    "general": {"profile_code": "guardian_daily_reference", "profile_name": "日常适配参考",
                "symptom_type": "general", "weights": {"protein_quality": .40, "p_buffer": .25,
                "fat_regulation_score": .20, "antioxidant_score": .15}, "thresholds": {}, "avoid_rules": []},
}


def goals_in_text(text):
    return [key for key, config in GOALS.items() if re.search(config["pattern"], str(text or ""), re.I)]


def goal_options():
    return [{"label": item["label"], "value": key} for key, item in GOALS.items()]
