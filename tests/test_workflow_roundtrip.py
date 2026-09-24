import importlib
from concurrent.futures import ThreadPoolExecutor
import pytest
import backend as B


@pytest.fixture
def local(monkeypatch, tmp_path):
    adapter = importlib.import_module('local_store')
    monkeypatch.setenv('ZL_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(B, 'lc', adapter)
    return adapter


def test_storage_survives_reload_and_filters(local):
    rid = local.add('demo', [{'合同': {'text': 'A'}}])[0]['record_id']
    assert local.get_record('demo', rid)['合同']['text'] == 'A'
    assert len(local.query('demo', {'property': {'property': '合同', 'text': {'equals': 'A'}}})) == 1
    assert not local.query('demo', {'property': {'property': '合同', 'text': {'equals': 'B'}}})
    assert importlib.reload(local).get_record('demo', rid)


def test_full_flow_and_payment_idempotency(local):
    W = importlib.import_module('workflow')
    keeper = {'account': 'keeper', 'name': '同名用户', 'role': B.ROLE_KEEPER}
    reviewer = {'account': 'reviewer', 'name': '同名用户', 'role': B.ROLE_APPROVER}
    payload = {'request_id': 'test-1', 'card': {'合同编码': 'DEMO', '费用类型': '租金',
        '承租方': '乙', '甲方': '甲', '实际付款方': '丙', '实际收款方': '丁',
        '租赁地址': '测试地址', '建筑面积': '100', '租赁起始日': '2025-01-01',
        '租赁终止日': '2025-12-31', '是否IFRS': '否', '结算方式': '付当月'},
        'segments': [{'租赁起始日': '2025-01-01','租赁终止日': '2025-12-31',
            '月份数': '12','月金额': '100','金额小计': '1200'}],
        'rows': [{'period': '2025年1月', 'rent': '100', 'total': '100',
                  '_source': '识别原文', '_original': {'rent': '90'}}],
        'sharing': [], 'oneoff': [], 'provenance': {'fields': {'甲方': {'source': '人工修改'}}}}
    first = W.save_intake(B, keeper, payload)
    assert first['ok'], first
    card_id = first['card_id']
    assert W.save_intake(B, keeper, payload)['card_id'] == card_id
    assert len(local.query(B.TBL['cards'])) == 1
    detail = W.latest_intake(B, card_id)
    assert detail['payload']['rows'][0]['_original']['rent'] == '90'
    generated = B.generate_ledgers(keeper, card_id)
    assert generated['ok'], generated
    assert W.submit(B, keeper, card_id)['ok']
    assert not W.decide(B, keeper, card_id, '通过', '')['ok']
    assert W.decide(B, reviewer, card_id, '退回', '补充依据')['ok']
    assert W.submit(B, keeper, card_id)['ok']
    assert not W.withdraw(B, reviewer, card_id, '误点')['ok']
    assert W.withdraw(B, keeper, card_id, '修改日期')['ok']
    assert W.submit(B, keeper, card_id)['ok']
    assert W.decide(B, reviewer, card_id, '通过', '核对完成')['ok']
    assert B.generate_payments(keeper, card_id)['ok']
    pay = local.query(B.TBL['pay_plan'])[0]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: W.submit_payment(B, keeper, [pay['record_id']]), range(2)))
    assert sum(bool(r['ok']) for r in results) == 1
    batch = next(r['batch'] for r in results if r['ok'])
    assert W.decide_payment(B, {'account':'payer','name':'审批人','role':'付款审批人'}, batch, '通过', '核对')['ok']
    assert W.record_payment(B, keeper, pay['record_id'], {'实付金额':'100','实付日期':'2025-01-01'})['ok']
    assert not W.record_payment(B, keeper, pay['record_id'], {'实付金额':'100','实付日期':'2025-01-01'})['ok']
    before = local.query(B.TBL['pay_plan'])
    assert B.generate_payments(keeper, card_id)['ok']
    assert local.query(B.TBL['pay_plan']) == before


def test_resume_after_partial_failure(local, monkeypatch):
    W = importlib.import_module('workflow')
    cur = {'account': 'owner', 'name': '维护', 'role': B.ROLE_KEEPER}
    body = {'request_id':'retry', 'card': {'合同编码':'RETRY','费用类型':'租金'},
            'segments': [], 'rows': [], 'sharing': [], 'oneoff': [], 'provenance': {}}
    original = B.save_payment_draft
    monkeypatch.setattr(B, 'save_payment_draft', lambda *args: {'ok':False,'error':'临时失败'})
    failed = W.save_intake(B, cur, body)
    assert not failed['ok'] and failed['card_id']
    monkeypatch.setattr(B, 'save_payment_draft', original)
    assert W.save_intake(B, cur, body)['ok']
    assert len(local.query(B.TBL['cards'])) == 1
