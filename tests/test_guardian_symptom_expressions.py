from unittest.mock import MagicMock, patch
import pytest
from services import guardian_task_knowledge as task


def identify(query):
    conn = MagicMock()
    cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [
        dict(issue_code='STOOL_ODOR', issue_name='粪便异味加重', aliases='便臭、猫屎臭、便便特别臭'),
        dict(issue_code='DIARRHEA', issue_name='腹泻', aliases=''),
        dict(issue_code='SOFT_STOOL', issue_name='软便', aliases=''),
    ]
    with patch.object(task, '_connect', return_value=conn):
        return {row['issue_code'] for row in task.identify_issues(query)}


@pytest.mark.parametrize('query', [
    '换粮后拉粑粑好臭啊！', '拉粑粑臭', '粑粑臭臭的', '拉巴巴特别臭',
    '便便很臭', '便便变臭', '屎很臭', '大便有异味', '粑粑的味道很重',
])
def test_colloquial_stool_odour(query):
    assert identify(query) == {'STOOL_ODOR'}


@pytest.mark.parametrize('query', [
    '粑粑不臭了', '粑粑不太臭', '粑粑没有异味', '没有便臭', '没有拉粑粑臭',
    '不再拉稀', '没有软便', '粑粑不软', '拉稀已经好了', '粑粑臭消失了',
    '臭臭', '猫砂盆好臭', '嘴巴臭',
])
def test_negative_resolved_and_unrelated_expressions(query):
    assert identify(query) == set()


@pytest.mark.parametrize('query, expected', [
    ('这两天拉稀', {'DIARRHEA'}), ('一直拉肚子', {'DIARRHEA'}),
    ('粑粑像水一样', {'DIARRHEA'}), ('粑粑软趴趴', {'SOFT_STOOL'}),
    ('便便不成形', {'SOFT_STOOL'}), ('粑粑有点软', {'SOFT_STOOL'}),
    ('没有拉稀，但是粑粑好臭', {'STOOL_ODOR'}),
    ('没有便臭，但是拉稀', {'DIARRHEA'}),
    ('粑粑好臭，而且有点软便', {'STOOL_ODOR', 'SOFT_STOOL'}),
])
def test_symptoms_stay_distinct_and_negation_stays_in_clause(query, expected):
    assert identify(query) == expected
