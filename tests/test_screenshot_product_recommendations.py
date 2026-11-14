from copy import deepcopy
import json
from pathlib import Path
import zipfile
from xml.sax.saxutils import escape
import pytest
from services import screenshot_product_recommendation_import_service as sheet
from services import product_recommendation_import_service as products


def values():
    return {'种类':'猫','品种':'不限（未指定品种）','需求':'敏感肠胃','品牌':'皇家',
            '商品名':'皇家低敏处方粮','推荐关系':'截图推荐线索','推荐比例':'0.35','场景排名':'1',
            '生命阶段':'不限','需求代码':'digestive_sensitivity','截图提问':'给敏感肠胃的猫咪选口粮',
            '参考经验人数':'1974','来源图片':'IMG_8518.PNG','核验状态':'待核验'}


def record(row=None, row_number=2, file_hash='abc'):
    return sheet.normalize_row(row or values(),file_name='source.xlsx',file_hash=file_hash,
                               sheet_name='产品推荐关系',row_number=row_number)


def test_useful_fields_are_typed_preserved_and_not_model_evidence():
    r=record();group=products.group_records([r])[0]
    assert group['entity_level']=='product' and group['model_count']==0
    assert group['recommendation_ratio']==0.35 and group['scene_rank']==1
    assert group['experience_count']==1974 and group['source_images']==['IMG_8518.PNG']
    assert group['life_stage_code']=='unspecified' and group['breed_code']=='all_cat'
    s=group['sources'][0]
    assert s['source_file']=='source.xlsx' and s['source_row']==2 and s['source_type']=='community_screenshot'
    assert s['recommendation']['evidence_status']=='unverified_community'
    assert r['verification_status']=='pending'


def test_brand_only_is_not_a_fabricated_product_or_claim_of_all_stages():
    row=values();row['商品名']='未明确';row['生命阶段']='幼猫'
    brand=record(row)
    product=record()
    assert brand['entity_level']=='brand' and brand['product_name']=='未明确'
    assert brand['product_key']!=product['product_key']
    assert brand['recommendations'][0]['life_stage_code']=='kitten'
    assert products.group_records([brand])[0]['entity_level']=='brand'


def test_multiple_community_scenes_keep_all_signals_without_averaging_or_summing():
    a=record();row=values();row['推荐比例']='0.2';row['参考经验人数']='100';row['场景排名']='2';row['来源图片']='another.png'
    b=record(row,row_number=3)
    g=products.group_records([a,b])[0]
    assert len(g['community_signals'])==2 and g['source_count']==2
    assert g['recommendation_ratio'] is None and g['scene_rank'] is None and g['experience_count'] is None
    assert sorted(s['recommendation_ratio'] for s in g['community_signals'])==[0.2,0.35]
    assert products.group_records([b,a])==products.group_records([a,b])


@pytest.mark.parametrize('field,value',[('推荐比例','1.5'),('场景排名','1.5'),('参考经验人数','-2'),('推荐比例','NaN')])
def test_invalid_metrics_are_not_imported(field,value):
    row=values();row[field]=value
    with pytest.raises(ValueError):record(row)


def test_xlsx_reader_extracts_headers_and_preserves_row_provenance(tmp_path):
    path=tmp_path/'source.xlsx';row=values();headers=list(row)
    def column(n):
        return chr(65+n)
    def xml_row(number, vals):
        return '<row r="'+str(number)+'">'+''.join('<c r="'+column(i)+str(number)+'" t="inlineStr"><is><t>'+escape(v)+'</t></is></c>' for i,v in enumerate(vals))+'</row>'
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('xl/workbook.xml','<workbook xmlns="'+sheet.NS['s']+'" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="产品推荐关系" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="'+sheet.NS['s']+'"><sheetData>'+xml_row(1,headers)+xml_row(2,[row[k] for k in headers])+'</sheetData></worksheet>')
    result=sheet.normalize_workbook(path)
    assert result['source_rows']==1 and not result['errors']
    r=result['records'][0]
    assert r['sources'][0]['source_row']==2 and r['sources'][0]['spreadsheet_row']==row
    assert sheet.normalize_workbook(path)==result
