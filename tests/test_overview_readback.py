from unittest.mock import patch
import backend as B


def test_readback_mismatch_preserves_previous_rows():
    card = {'record_id': 'card', 'properties': {'审批状态': {'select': '草稿'}}}
    with patch.object(B, 'val', side_effect=lambda row, key, default=None: row.get(key, default)), \
         patch.object(B.lc, 'get_record', side_effect=[{'审批状态': '草稿'}, {'月金额': 999}]), \
         patch.object(B, '_overview_by_card', return_value=[{'record_id': 'old'}]), \
         patch.object(B.lc, 'add', return_value=[{'success': True, 'record_id': 'new'}]), \
         patch.object(B.lc, 'delete') as delete:
        result = B.save_amount_overview({}, 'card', [{'s': '2025-01-01', 'e': '2025-01-31', 'months': 1, 'amt': 100}])
        assert result['ok'] is False
        assert result['write_confirmed'] is False
        delete.assert_not_called()


def test_readback_supports_record_id_and_compares_all_values():
    rows = {}
    def add(table, payload):
        rows['new'] = {k: next(iter(v.values())) for k, v in payload[0].items()}
        return [{'success': True, 'record_id': 'new'}]
    with patch.object(B, 'val', side_effect=lambda row, key, default=None: row.get(key, default)), \
         patch.object(B.lc, 'get_record', side_effect=lambda table, rid: {'审批状态': '草稿'} if rid == 'card' else rows.get(rid)), \
         patch.object(B, '_overview_by_card', return_value=[{'record_id': 'old'}]), \
         patch.object(B.lc, 'add', side_effect=add), patch.object(B.lc, 'delete') as delete:
        result = B.save_amount_overview({}, 'card', [{'s': '2025-01-01', 'e': '2025-01-31', 'months': 1, 'amt': 100}])
        assert result['write_confirmed'] is True
        delete.assert_called_once_with(B.TBL['amount_overview'], ['old'])


def test_incomplete_approved_card_does_not_complete_workflow():
    with patch.object(B, 'val', side_effect=lambda row, key, default=None: row.get(key, default)):
        result = B.get_card_stage({'record_id': 'card', '审批状态': '通过'}, overview=[{}], ledger=[{}])
        assert result['stage'] == '已通过'
        assert result['blocking_reasons']
        assert result['step_index'] == 1
