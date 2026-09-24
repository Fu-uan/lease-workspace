from unittest.mock import patch
import pytest
import backend as B


@pytest.mark.parametrize("pending,valid", [(True, True), (False, False)])
def test_invalid_submission_makes_no_writes(pending, valid):
    card = {"审批状态": {"select": "草稿"}}
    with patch.object(B.lc, "get_record", return_value=card), \
         patch.object(B, "_ocr_pending", return_value=pending), \
         patch.object(B, "validate_card", return_value={
             "ok": valid, "errors": [{"message": "日期未覆盖"}]}), \
         patch.object(B, "_ledger_by_card", return_value=[{}]), \
         patch.object(B.lc, "add") as add, \
         patch.object(B, "_set_card_state") as state:
        assert not B.submit_card({"name": "维护人"}, "card")["ok"]
        add.assert_not_called()
        state.assert_not_called()


@pytest.mark.parametrize("payload", [
    {}, {"实付金额": "100"}, {"实付日期": "2025-01-01"},
    {"实付金额": "NaN", "实付日期": "2025-01-01"},
    {"实付金额": "Infinity", "实付日期": "2025-01-01"},
    {"实付金额": "-1", "实付日期": "2025-01-01"},
    {"实付金额": "100", "实付日期": "2025-02-30"},
    {"实付金额": "100.001", "实付日期": "2025-01-01"},
])
def test_invalid_payment_result_never_marks_paid(payload):
    with patch.object(B.lc, "get_record", return_value={"record_id": "pay"}), \
         patch.object(B.lc, "update_checked") as write:
        assert not B.record_payment({}, "pay", payload)["ok"]
        write.assert_not_called()


def test_valid_payment_result_stores_amount_and_date():
    with patch.object(B.lc, "get_record", return_value={"record_id": "pay"}), \
         patch.object(B.lc, "update_checked") as write:
        assert B.record_payment({}, "pay", {
            "实付金额": "100.25", "实付日期": "2025-01-01"})["ok"]
        props = write.call_args.args[1][0]["properties"]
        assert props["实付金额"]["currency"] == 100.25
        assert props["实付日期"]["date"] == "2025-01-01"
