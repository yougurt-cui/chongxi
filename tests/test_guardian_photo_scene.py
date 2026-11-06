import pytest

from services import guardian_photo_scene as photo
from services import miniprogram_chat_service as chat


@pytest.fixture
def conversation():
    return {'id': 'conv', 'pet_id': 'pet', 'profile_species': 'cat', 'profile_name': '团团'}


@pytest.fixture
def state():
    return chat._default_state('conv')


def test_route_combines_current_text_images_history_and_selected_pet(monkeypatch, conversation, state):
    images = [{'label': '粗蛋白 ≥40%'}, {'label': '粗蛋白 ≥32%'}]
    history = [{'role': 'user', 'text': '想找减重口粮'}]
    monkeypatch.setattr(chat, '_scene_history', lambda u, c: history if (u, c) == ('u', 'conv') else None)
    monkeypatch.setattr(chat, 'get_cat_profile', lambda u, p: {'name': '团团'} if (u, p) == ('u', 'pet') else None)

    def model(message, facts, recent, profile, *, followup):
        assert (message, facts, recent, profile, followup) == ('比较一下', images, history, {'name': '团团'}, False)
        return {'task': 'product_compare', 'needs': ['减重'], 'related': False, 'reply': '两者标签下限不同。'}

    monkeypatch.setattr(photo, 'decide', model)
    scene, facts, subject = chat._resolve_scene('u', conversation, state, '比较一下', images, {}, None, {}, allow_text=False)
    assert (scene['task'], scene['needs'], facts, subject) == ('product_compare', ['减重'], images, None)


def test_old_photo_requires_same_pet_turn_and_subject(monkeypatch, conversation, state):
    previous = {'pet_id': 'pet', 'turn': 0, 'subject': None, 'photos': [{'animal': 'cat'}]}
    monkeypatch.setattr(chat, '_scene_history', lambda *args: [])
    monkeypatch.setattr(chat, 'get_cat_profile', lambda *args: {})
    monkeypatch.setattr(photo, 'decide', lambda *args, **kwargs: {'task': 'pet_identify', 'related': kwargs['followup']})
    def resolve(prev, sub):
        return chat._resolve_scene('u', conversation, state, '那它呢？', [], prev, sub, {}, allow_text=True)

    assert resolve(previous, None)[0]['task'] == 'pet_identify'
    for bad in ({'turn': 1}, {'pet_id': 'different'}, {'subject': 'dog'}):
        assert resolve({**previous, **bad}, None)[0] is None
    assert resolve(previous, 'dog')[0] is None
    monkeypatch.setattr(photo, 'decide', lambda *args, **kwargs: {'task': 'pet_knowledge', 'related': False})
    assert resolve(previous, None) == (None, [], None)


def test_recognized_other_species_keeps_profile_out_of_prompt(monkeypatch, conversation, state):
    def model(message, photos, history, profile, **kwargs):
        assert profile == {}
        return {'task': 'pet_identify', 'related': False}
    monkeypatch.setattr(photo, 'decide', model)
    monkeypatch.setattr(chat, '_scene_history', lambda *args: [])
    assert chat._resolve_scene('u', conversation, state, '什么品种', [{'animal': 'dog'}], {},
                               None, {}, allow_text=False)[2] == 'dog'


def test_unreadable_photo_and_urgent_text_do_not_require_a_model(monkeypatch, conversation, state):
    monkeypatch.setattr(chat, '_scene_history', lambda *args: [])
    monkeypatch.setattr(chat, 'get_cat_profile', lambda *args: {})
    for message, expected in [('', 'unclear'), ('现在呼吸困难', 'health_consult')]:
        scene, photos, subject = chat._resolve_scene('u', conversation, state, message, [{}], {},
                                                      None, {}, allow_text=False)
        assert scene['task'] == expected
        reply = chat._scene_reply('u', conversation, message, state, scene, photos, subject, 'msg')
        if not message:
            assert '看不清' in reply['reply']
        else:
            assert reply['response_type'] == 'risk_alert'


def test_model_outage_preserves_text_router_and_does_not_invent_visible_facts(monkeypatch, conversation, state):
    def offline(**kwargs):
        raise RuntimeError('offline')

    monkeypatch.setattr(photo, 'OpenAI', offline)
    monkeypatch.setattr(chat, 'get_cat_profile', lambda *args: {})
    monkeypatch.setattr(chat, '_scene_history', lambda *args: [])
    capability = {'capabilities': {'scene_router_v1': True}}
    assert chat._resolve_scene('u', conversation, state, '请介绍一下', [], {}, None, capability, allow_text=True)[0] is None
    scene = chat._resolve_scene('u', conversation, state, '这是什么', [{'visible': '棕色短毛'}], {},
                                None, {}, allow_text=False)[0]
    assert scene['task'] == 'unclear' and '没能判断' in scene['reply']


def test_legacy_text_and_explicit_interactions_bypass_photo_classifier(conversation, state):
    assert chat._resolve_scene('u', conversation, state, '它是什么品种', [], {}, None, {}, allow_text=True)[0] is None
    assert chat._resolve_scene('u', conversation, state, '确认', [], {}, None,
                               {'capabilities': {'scene_router_v1': True}}, allow_text=False)[0] is None


def test_needs_require_user_evidence_or_verified_profile():
    proposed = [{'label': '减重', 'quote': '我想让它瘦下来'}, {'label': '软便', 'quote': '软便'},
                {'label': '成猫', 'quote': '档案'}, {'label': '老年猫', 'quote': '档案'},
                {'label': '品种-布偶猫', 'quote': '档案'}]
    profile = {'animal_type': 'cat', 'age_months': 36, 'breed': '布偶猫'}
    history = [{'role': 'assistant', 'text': '是否有软便？'}, {'role': 'user', 'text': '我想让它瘦下来'}]
    assert photo.grounded_needs(proposed, '比较这两款', history, profile, True) == ['减重', '成猫', '品种-布偶猫']
    assert photo.grounded_needs(proposed, '比较这两款', history, profile, False) == ['成猫', '品种-布偶猫']


def test_breed_recipient_and_normalized_goal_survive_partial_model_output():
    message = '给布偶幼猫推荐主粮，希望改善软便、增重和美毛'
    items = [{'label': label, 'quote': quote} for label, quote in (
        ('幼猫', '幼猫'), ('改善软便', '希望改善软便'), ('增重', '增重'), ('美毛', '美毛'))]
    needs = photo.grounded_needs(items, message, [], {'animal_type': 'cat'}, False)
    assert set(needs) == {'品种-布偶猫', '幼猫', '软便', '增重', '美毛'}
    assert photo.grounded_needs([], '我家英短需要换粮', [], {}, False) == ['品种-英国短毛猫']


def test_negated_goal_is_not_supported_by_a_partial_quote():
    items = [{'label': '改善软便', 'quote': '软便'}, {'label': '减重', 'quote': '减重'}]
    assert photo.grounded_needs(items, '没有软便，也不需要减重', [], {}, False) == []
    assert photo.grounded_needs([], '不是给布偶选粮，是给英短选粮', [], {}, False) == ['品种-英国短毛猫']
