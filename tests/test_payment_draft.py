from unittest.mock import patch
import backend as B


def test_roundtrip_and_idempotent_retry():
    saved = {}
    def add(table, payload):
        saved.update({k: next(iter(v.values())) for k, v in payload[0].items()})
        return [{'success': True, 'record_id': 'snapshot'}]
    with patch.object(B, 'val', side_effect=lambda row, key, default=None: row.get(key, default)), \
         patch.object(B.lc, 'get_record', side_effect=lambda table, rid: {'审批状态': '草稿'} if rid == 'card' else saved), \
         patch.object(B.lc, 'query', side_effect=lambda *a, **kw: [saved] if saved else []), \
         patch.object(B.lc, 'add', side_effect=add) as write:
        rows = [{'period': '2025年1至3月', 'pay_date': '2025-01-10', 'rent': '300', 'mgmt': '60', 'total': '360', 'tax': '9%/6%'}]
        first = B.save_payment_draft({'role': B.ROLE_KEEPER}, 'card', rows)
        second = B.save_payment_draft({'role': B.ROLE_KEEPER}, 'card', rows)
        assert first['write_confirmed'] and first['rows'] == rows
        assert second['reused']
        assert write.call_count == 1


def test_approver_cannot_write():
    with patch.object(B.lc, 'add') as write:
        assert not B.save_payment_draft({'role': B.ROLE_APPROVER}, 'card', [])['ok']
        write.assert_not_called()


def test_nonfinite_amount_rejected():
    assert not B.save_payment_draft({'role': B.ROLE_KEEPER}, 'card', [{'rent': 'NaN'}])['ok']
