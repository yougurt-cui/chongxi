import json
from copy import deepcopy
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from services import guardian_conversation_context as ctx
from services import miniprogram_chat_service as chat

CONV = dict(id='c',pet_id='cat',profile_species='cat',profile_age=24)


def memory():
    return ctx.empty('owner', CONV)


def msg(i, text, *, role='assistant', fid=None, health=False):
    result = {'turn_no':i,'intent':'food_analysis'}
    if fid:
        result['health_analysis'] = {'subjects':[{'formula_id':fid,'label':f'粮{fid}','roles':['named'],'findings':['结构评分仅作参考']}]}
    if health:
        result['health_knowledge'] = {'groups':[{'cause':'饮食变化','source_ids':[1]}], 'questions':['如何核对换粮时间？']}
    return dict(id=f'{i:032x}',role=role,content=text,result=result,created_at=f'2026-11-13 12:{i:02d}:00')


def seed():
    m=memory()
    for e in [msg(1,'便臭比换粮早三天',role='user'),msg(2,'可能涉及饮食、消化等原因',health=True),
              msg(3,'分析粮11',role='user'),msg(4,'粮11的结构评分',fid=11),
              msg(5,'分析粮22',role='user'),msg(6,'粮22的结构评分',fid=22)]:
        ctx.remember(m,e)
    return m


def test_two_separate_analyses_resolve_exact_formula_ids_without_model():
    m=seed()
    with patch.object(ctx,'_model',side_effect=AssertionError('unnecessary model')):
        decision=ctx.decide('帮我对比一下前面两款粮',m)
    assert decision['product_ids']==[11,22]
    assert decision['source_ids']==[f'{4:032x}',f'{6:032x}']
    assert ctx.decide('比较前面两款粮',memory()) is None
    m['products']=m['products'][:1]
    assert ctx.decide('比较前面两款粮',m)['action']=='clarify'
    ctx.remember(m,msg(7,'粮33',fid=33));ctx.remember(m,msg(8,'粮44',fid=44))
    assert ctx.decide('比较前面两款粮',m)['action']=='clarify'


def test_health_effect_question_does_not_repeat_the_comparison_report():
    with patch.object(ctx,'_model',side_effect=AssertionError('unnecessary route model')):
        assert ctx.decide('这两款粮里，哪款肯定能解决便臭？',seed())['action']=='synthesize'


def test_memory_keeps_prior_health_and_product_evidence_across_long_unrelated_chat():
    m=seed()
    for i in range(7,65):ctx.remember(m,msg(i,'无关话题'))
    assert {e['id'] for e in m['events']} >= {f'{i:032x}' for i in [1,2,4,6]}
    assert len(m['events'])<=54
    external=msg(70,'另一只狗的情况',fid=33);external['result']['context_scope']='external';ctx.remember(m,external)
    assert not any(p['formula_id']==33 for p in m['products'])


def db(rows, source=None):
    conn=MagicMock();cur=conn.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value=rows;cur.fetchone.return_value=source
    return conn,cur


def test_load_checks_scope_owner_source_question_and_parameterized_sql():
    source=msg(2,'旧健康卡',health=True);source['result_json']=json.dumps(source.pop('result'))
    conn,cur=db([],source)
    state={'slots':{ctx.SLOT:{**seed(),'pet_id':'another-pet'}}}
    m=ctx.load(lambda:conn,'owner',CONV,state,source['id'],'如何核对换粮时间？')
    assert not m['products'] and len(m['events'])==1
    sql,params=cur.execute.call_args.args
    assert params==(source['id'],'owner','c') and 'user_id=%s' in sql and 'conversation_id=%s' in sql
    with pytest.raises(ValueError):ctx.load(lambda:conn,'owner',CONV,state,source['id'],'伪造追问')
    cur.fetchone.return_value=None
    with pytest.raises(ValueError):ctx.load(lambda:conn,'owner',CONV,state,'x','如何核对换粮时间？')


@pytest.mark.parametrize('reply',[
    {'action':'compare','product_ids':[11,999],'source_ids':[]},
    {'action':'continue','product_ids':[],'source_ids':['forged']},
    {'action':'invalid'},
])
def test_model_route_never_invents_sources_or_products(reply):
    with patch.object(ctx,'_model',return_value=reply):
        assert ctx.decide('那前面说的怎么样',seed())['action']=='unavailable'


def test_return_to_topic_and_explicit_source_are_forwarded():
    m=seed();m['source_id']=f'{2:032x}'
    with patch.object(ctx,'_model',return_value={'action':'resume','source_ids':[f'{2:032x}'],'product_ids':[]}) as model:
        assert ctx.decide('如何核对换粮时间？',m)['action']=='resume'
    assert model.call_args.args[1]['source_id']==f'{2:032x}'
    with patch.object(ctx,'_model',return_value={'action':'new'}):
        assert ctx.decide('那我们换个话题聊玩具',seed())['action']=='new'


def answer_payload():
    return {'conclusion':'目前不能确定最主要原因，先核对便臭与饮食变化的时间。',
            'reasons':[{'text':'你说便臭早于换粮，不能仅凭两款粮的评分认定换粮导致。','source_ids':[f'{1:032x}']}],
            'missing':['便臭持续多久？'],'questions':['如何记录便便变化？','哪些表现需要补充？','如何核对换粮时间？']}


def test_attribution_uses_user_facts_health_knowledge_and_both_analyses():
    m=seed();state=chat._default_state('c')
    question='结合我刚说的，你觉得便臭最大的可能是什么？'
    decision=ctx.decide(question,m)
    assert decision['action']=='synthesize'
    with patch.object(ctx,'_model',return_value=answer_payload()) as model:
        reply=ctx.answer(question,m,decision,state)
    assert reply['result']['context_answer']['status']=='answered'
    ids={e['id'] for e in model.call_args.args[1]['evidence']}
    assert ids >= {f'{i:032x}' for i in [1,2,4,6]}
    assert state['turn_count']==1 and m['focus']['source_ids']==[f'{1:032x}']
    assert json.loads(json.dumps(reply))==reply


@pytest.mark.parametrize('bad', ['source','dose','certainty','question','empty'])
def test_answer_validation_fails_closed(bad):
    output=answer_payload()
    if bad=='source':output['reasons'][0]['source_ids']=['unknown']
    if bad=='dose':output['conclusion']='每天给10毫克'
    if bad=='certainty':output['conclusion']='一定是这个粮导致'
    if bad=='question':output['questions']=[{}]
    if bad=='empty':output['reasons']=[]
    with patch.object(ctx,'_model',return_value=output):
        reply=ctx.answer('最可能是什么原因',seed(),{'action':'synthesize'},chat._default_state('c'))
    assert reply['result']['context_answer']['status']=='unavailable'


def test_dynamic_questions_use_current_answer_and_context_and_fallback_is_safe():
    reply={'reply':'两款粮的对比结论','result':{'guardian_report':{'next_steps':{'questions':['原问题']}}}}
    with patch.object(ctx,'_model',return_value={'questions':[f'问题{i}？' for i in range(6)]}) as model:
        ctx.contextual_questions(reply,'比较前面两款',seed())
    assert len(reply['result']['guardian_report']['next_steps']['questions'])==6
    assert model.call_args.args[1]['history']
    assert model.call_args.args[1]['current_products']==[]
    saved=deepcopy(reply)
    with patch.object(ctx,'_model',side_effect=TimeoutError):ctx.contextual_questions(reply,'继续',seed())
    assert reply==saved


def test_real_orchestrator_comparison_bypasses_name_search_and_preserves_history():
    state=chat._default_state('c');m=seed();saved=[]
    def save(**kw):
        item=dict(id=f'{100+len(saved):032x}',**kw);saved.append(item);return item
    with ExitStack() as stack:
        def mock(obj,key,**kw):return stack.enter_context(patch.object(obj,key,**kw))
        mock(chat,'_get_conversation',return_value=CONV);mock(chat,'_load_state',return_value=state)
        persist=mock(chat,'_save_state');mock(chat,'_save_message',side_effect=save);mock(chat,'_connect',return_value=MagicMock())
        mock(ctx,'load',return_value=m);mock(ctx,'contextual_questions')
        mock(chat,'_resolve_scene',return_value=(None,[],None))
        mock(chat,'_build_context',return_value={'pet_profile':{'animal_type':'cat'}})
        loader=mock(chat,'_food_analysis_by_formula',side_effect=lambda fid:{'product':{'formula_id':fid},'product_portrait':{'formula_id':fid}})
        mock(chat,'_recognize_guardian_turn',side_effect=AssertionError('must not resolve names again'))
        build=mock(chat.health_flow,'build_analysis_reply',return_value={'reply':'已比较两款粮','result':{'health_analysis':{'subjects':[]}}})
        result=chat.handle_message('owner','c',{'message':'帮我对比一下前面两款粮'})
    assert [c.args[0] for c in loader.call_args_list]==[11,22]
    assert [s['food_analysis']['product']['formula_id'] for s in build.call_args.args[3]['named_food_analyses']]==[11,22]
    assert result['message']['result']['context_resolution']['formula_ids']==[11,22]
    assert len(saved)==2 and state['slots'][ctx.SLOT]['events'][-1]['id']==saved[-1]['id']
    persist.assert_called_once()


def test_missing_portrait_does_not_silently_drop_one_side():
    with patch.object(chat,'_food_analysis_by_formula',return_value=None):
        reply=chat._context_comparison('owner',CONV,chat._default_state('c'),seed(),
            {'action':'compare','product_ids':[11,22],'source_ids':[]},{})
    assert reply['result']['context_answer']['status']=='missing_product_evidence'


def test_urgent_symptoms_never_enter_context_reasoning():
    state=chat._default_state('c');state['slots'][ctx.SLOT]=seed()
    with ExitStack() as stack:
        def mock(obj,key,**kw):return stack.enter_context(patch.object(obj,key,**kw))
        mock(chat,'_get_conversation',return_value=CONV);mock(chat,'_load_state',return_value=state)
        mock(chat,'_save_state');mock(chat,'_connect',return_value=MagicMock())
        mock(chat,'_save_message',side_effect=lambda **kw:dict(id='a'*32,**kw))
        mock(ctx,'load',return_value=seed());decide=mock(ctx,'decide');mock(ctx,'contextual_questions')
        mock(chat,'_resolve_scene',return_value=({'task':'health_consult','needs':[]},[],None))
        mock(chat,'_scene_reply',return_value={'reply':'立即就医','response_type':'risk_alert','result':{}})
        r=chat.handle_message('owner','c',{'message':'结合刚才的情况，它现在呼吸困难了'})
        decide.assert_not_called();assert r['message']['response_type']=='risk_alert'
        assert len(state['slots'][ctx.SLOT]['products'])==2


def test_emergency_scene_works_without_client_capabilities_or_fresh_topic():
    state=chat._default_state('c');state.update(primary_intent='food_analysis',current_step='food_analysis_usage')
    scene,_,_=chat._resolve_scene('owner',CONV,state,'结合刚才的情况，它呼吸困难了',[],{},None,{},allow_text=False)
    assert scene['task']=='health_consult'


def test_synthesis_never_uses_previous_synthesis_as_independent_evidence():
    m=seed();prior=msg(10,'旧推测');prior['result']={'pipeline':'conversation_context','context_answer':answer_payload()}
    ctx.remember(m,prior);m['source_id']=prior['id']
    with patch.object(ctx,'_model',return_value=answer_payload()) as model:
        ctx.answer('有什么依据',m,{'action':'continue'},chat._default_state('c'))
    evidence=model.call_args.args[1]['evidence']
    assert prior['id'] not in {e['id'] for e in evidence}
    assert f'{1:032x}' in {e['id'] for e in evidence}


def test_focused_old_card_excludes_unselected_product_reports_and_keeps_correction():
    m=seed();m['source_id']=f'{2:032x}'
    ctx.remember(m,msg(9,'更正：便臭是在换粮后开始的',role='user'))
    with patch.object(ctx,'_model',return_value=answer_payload()) as model:
        reply=ctx.answer('怎样核对寄生虫的可能？',m,{'action':'resume'},chat._default_state('c'))
    ids={e['id'] for e in model.call_args.args[1]['evidence']}
    assert {f'{2:032x}',f'{9:032x}'} <= ids
    assert not {f'{4:032x}',f'{6:032x}'} & ids
    assert reply['reply']==answer_payload()['conclusion']
    assert reply['result']['context_answer']['reasons']==answer_payload()['reasons']


def test_product_followup_keeps_numeric_comparison_without_unrelated_health_card():
    m=seed();source=msg(8,'两款粮益生元相同')
    source['result']['guardian_report']={'feeding':{'comparisons':[{
        'left_label':'粮11','right_label':'粮22','status':'comparable','dimensions':[
            {'title':'益生元支持','status':'comparable','left':{'score':1.56,'scale_max':5},
             'right':{'score':1.56,'scale_max':5},'delta':0}]}]}}
    ctx.remember(m,source);m['source_id']=source['id']
    payload=answer_payload();payload['reasons'][0]['source_ids']=[source['id']]
    with patch.object(ctx,'_model',return_value=payload) as model:
        reply=ctx.answer('先关注哪个区别？',m,{'action':'continue'},chat._default_state('c'))
    assert reply['result']['context_answer']['status']=='answered'
    evidence=model.call_args.args[1]['evidence']
    assert not {f'{1:032x}',f'{2:032x}'} & {e['id'] for e in evidence}
    dimension=next(e for e in evidence if e['id']==source['id'])['evidence']['comparisons'][0]['dimensions'][0]
    assert dimension['left_score']==dimension['right_score']==1.56 and dimension['delta']==0


@pytest.mark.parametrize('questions',[
    ['怎样记录？','怎样记录?','要补充什么？'],
    ['最可能是什么原因？','要补充什么？','怎样记录？'],
    ['怎样记录？','要补充什么？','粮款全名'*8+'怎样比较？'],
])
def test_redundant_or_overlong_questions_are_rejected(questions):
    output=answer_payload();output['questions']=questions
    with patch.object(ctx,'_model',return_value=output):
        reply=ctx.answer('最可能是什么原因',seed(),{'action':'synthesize'},chat._default_state('c'))
    assert reply['result']['context_answer']['status']=='unavailable'


def test_hidden_audit_fields_are_still_checked_for_unsupported_certainty():
    output=answer_payload();output['reasons'][0]['text']='一定是新粮导致'
    with patch.object(ctx,'_model',return_value=output):
        reply=ctx.answer('最可能是什么原因',seed(),{'action':'synthesize'},chat._default_state('c'))
    assert reply['result']['context_answer']['status']=='unavailable'


def test_new_topic_clears_pending_flow_but_keeps_memory():
    state=chat._default_state('c');state.update(primary_intent='food_analysis',current_step=chat.health_flow.CONFIRM_SLOT)
    state['slots'][chat.health_flow.STATE_KEY]={'stage':'pending'}
    with ExitStack() as stack:
        def mock(obj,key,**kw):return stack.enter_context(patch.object(obj,key,**kw))
        mock(chat,'_get_conversation',return_value=CONV);mock(chat,'_load_state',return_value=state)
        mock(chat,'_save_state');mock(chat,'_connect',return_value=MagicMock())
        mock(chat,'_save_message',side_effect=lambda **kw:dict(id='b'*32,**kw))
        mock(ctx,'load',return_value=seed());mock(ctx,'decide',return_value={'action':'new'});mock(ctx,'contextual_questions')
        def scene(*args,**kw):
            assert state['primary_intent'] is None and state['current_step'] is None
            assert state['slots'][chat.health_flow.STATE_KEY]['stage']=='superseded'
            return {'task':'pet_knowledge','needs':[]},[],None
        mock(chat,'_resolve_scene',side_effect=scene)
        mock(chat,'_scene_reply',return_value={'reply':'玩具的新话题','result':{}})
        chat.handle_message('owner','c',{'message':'那换个话题聊玩具'})
    assert len(state['slots'][ctx.SLOT]['products'])==2


def test_independent_topics_can_later_be_combined_and_replayed():
    m = memory()
    for event in [msg(1, '猫咪便便臭是什么原因', role='user'),
                  msg(2, '蛋白发酵与纤维组成是可能线索', health=True),
                  msg(3, '分析历史粮和当前粮', role='user'),
                  msg(4, '两款粮的独立分析', fid=11)]:
        ctx.remember(m, event)
    assert len(m['topics']) == 2
    a, b = m['topics']
    assert '便便臭' in a['question'] and '历史粮' in b['question']
    assert a['product_ids'] == [] and b['product_ids'] == [11]
    d = ctx.decide('结合我的换粮历史帮我看看什么原因造成的', m)
    assert d['action'] == 'synthesize'
    assert set(d['topic_ids']) == {a['id'], b['id']}
    follow = msg(5, '结合历史看看原因', role='user')
    follow['result']['discussion'] = ctx.discussion(d)
    ctx.remember(m, follow)
    assert len(m['topics']) == 2
    assert set(m['events'][-1]['topic_ids']) == {a['id'], b['id']}
    again = deepcopy(m)
    ctx.remember(again, follow)
    assert again['topics'] == m['topics']


def test_plain_independent_question_is_classified_with_topic_index():
    m = seed()
    with patch.object(ctx, '_model', return_value={'action': 'new'}) as model:
        assert ctx.decide('帮我分析一下历史粮和当前粮', m)['action'] == 'new'
    assert model.call_args.args[1]['topics'] == m['topics']


def test_synthesis_enriches_only_selected_products_with_raw_ingredients():
    m = seed()
    decision = ctx.decide('结合换粮历史分析原因', m)
    with patch.object(chat, '_build_context', return_value={'diet_history': [{'formula_id': 11}]}), \
         patch.object(chat, '_food_analysis_by_formula', side_effect=lambda fid: {'ingredient_analysis': {'raw_ingredient_text': '复合原料(菊糖)'}}) as loader, \
         patch.object(ctx, 'answer', return_value={'reply': '综合结论'}) as answer:
        chat._context_answer('owner', CONV, '结合历史分析原因', chat._default_state('c'), m, decision, {})
    assert [c.args[0] for c in loader.call_args_list] == [11, 22]
    assert answer.call_args.kwargs['product_evidence'][0]['analysis']['ingredient_analysis']['raw_ingredient_text'] == '复合原料(菊糖)'
    assert answer.call_args.kwargs['diet_context']['diet_history'] == [{'formula_id': 11}]


def test_switch_history_synthesis_beats_food_switch_keyword_in_real_entry():
    state = chat._default_state('c')
    m = seed()
    with ExitStack() as stack:
        def mock(obj, key, **kw): return stack.enter_context(patch.object(obj, key, **kw))
        mock(chat, '_get_conversation', return_value=CONV)
        mock(chat, '_load_state', return_value=state)
        mock(chat, '_save_state'); mock(chat, '_connect', return_value=MagicMock())
        saved = []
        def save(**kw):
            item = dict(id=f'{100+len(saved):032x}', **kw); saved.append(item); return item
        mock(chat, '_save_message', side_effect=save)
        mock(ctx, 'load', return_value=m); mock(ctx, 'contextual_questions')
        mock(chat, '_resolve_scene', return_value=(None, [], None))
        mock(chat, '_build_context', return_value={})
        mock(chat, '_food_analysis_by_formula', return_value={'ingredient_analysis': {}})
        answer = mock(ctx, 'answer', return_value={'reply': '结合便臭与配方变化的推论', 'result': {'pipeline': 'conversation_context'}})
        mock(chat, '_recognize_guardian_turn', side_effect=AssertionError('must not start selection'))
        result = chat.handle_message('owner', 'c', {'message': '结合我的换粮历史帮我看看什么原因造成的'})
    assert answer.call_args.args[2]['action'] == 'synthesize'
    assert result['message']['result']['pipeline'] == 'conversation_context'


def test_continuation_can_select_topic_without_repeating_message_ids():
    m = seed()
    topic = m['topics'][0]
    with patch.object(ctx, '_model', return_value={'action': 'resume', 'topic_ids': [topic['id']]}):
        d = ctx.decide('回到便臭的问题', m)
    assert d['action'] == 'resume' and d['topic_ids'] == [topic['id']]


def test_multiple_health_topics_require_semantic_selection():
    m = seed()
    ctx.remember(m, msg(9, '另外问一下皮肤痒的原因', role='user'))
    ctx.remember(m, msg(10, '皮肤问题的可能机制', health=True))
    with patch.object(ctx, '_model', return_value={'action': 'synthesize', 'topic_ids': [m['topics'][0]['id']]} ) as model:
        d = ctx.decide('结合前面的资料看看便臭原因', m)
    model.assert_called_once()
    assert d['topic_ids'] == [m['topics'][0]['id']]
