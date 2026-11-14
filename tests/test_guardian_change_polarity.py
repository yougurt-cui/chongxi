from unittest.mock import patch
from services import guardian_task_knowledge as task
from test_guardian_task_knowledge import dim,pair


def test_good_and_attention_follow_indicator_direction_and_preserve_magnitude_order():
 ds=[dim('protein_quality',.825,.992),dim('protein',.658,.503,kind='higher_is_burden'),dim('fat',.581,.21,kind='higher_is_burden'),dim('fiber',3.46,2.028,5),dim('carbohydrate',3.42,2.4,5,kind='higher_is_burden'),dim('prebiotic',1.12,.204,5)]
 with patch.object(task,'load_rules',return_value=[]),patch.object(task,'switch_health_rows',return_value=[]),patch.object(task,'load_market_advantages',return_value={}):
  p=task.nutrition_pairs([pair(ds)],'cat')['pairs'][0]
 groups={g['key']:g for g in p['change_groups']}
 assert [d['key'] for d in groups['positive']['items']]==['fat','carbohydrate','protein_quality','protein']
 assert [d['key'] for d in groups['attention']['items']]==['fiber','prebiotic']
 assert all(d['change_label']=='好的方向' and d['title'].startswith('好的方向') for d in groups['positive']['items'])
 assert all(d['change_label']=='留心的方向' for d in groups['attention']['items'])
 assert sum(len(g['items']) for g in groups.values())==6
 for g in groups.values():assert [d['magnitude'] for d in g['items']]==sorted([d['magnitude'] for d in g['items']],reverse=True)
