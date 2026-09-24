# -*- coding: utf-8 -*-
"""结算方式（十类）付款周期分组的回归测试（纯函数，不访问资料库）。

依据需求「3、租赁卡片W列未付款金额在不同结算方式下的设置案例」：
    结算方式 | 首个付款节点（租赁首月=7月） | 付款周期 | 求和范围
    付当月   | 7月  | 1个月 | 当月
    预付下月 | 6月  | 1个月 | 下月
    付两月   | 7月  | 2个月 | 当月+下月
    预付两月 | 6月  | 2个月 | 下月+下下月
    预付下季 | 6月  | 3个月 | 下月起共3个月
运行：python test_settlement.py
"""
import unittest
from decimal import Decimal

from backend import SETTLE_RULE, group_payment_nodes, _period_label

# 以租赁首月 = 7 月为例，取 12 个月
MONTHS = ["2025-%02d" % m for m in range(7, 13)] + ["2026-%02d" % m for m in range(1, 7)]
AMTS = [(m, Decimal("1000")) for m in MONTHS]


def nodes(settle, months=None, amts=None):
    r = SETTLE_RULE[settle]
    lead = -1 if r["prepay"] else 0
    return group_payment_nodes(amts if amts is not None else AMTS, r["cycle"], lead)


class TestRules(unittest.TestCase):
    def test_all_ten_settlements_defined(self):
        """需求要求的十类结算方式必须全部有规则。"""
        expect = {"付当月", "预付下月", "付两月", "预付两月", "付当季",
                  "预付下季", "付半年", "预付半年", "付当年", "预付下年"}
        self.assertEqual(set(SETTLE_RULE), expect)

    def test_rows_example_mapping(self):
        """对齐需求案例表：周期与预付方向。"""
        self.assertEqual((SETTLE_RULE["付当月"]["cycle"], SETTLE_RULE["付当月"]["prepay"]), (1, False))
        self.assertEqual((SETTLE_RULE["预付下月"]["cycle"], SETTLE_RULE["预付下月"]["prepay"]), (1, True))
        self.assertEqual((SETTLE_RULE["付两月"]["cycle"], SETTLE_RULE["付两月"]["prepay"]), (2, False))
        self.assertEqual((SETTLE_RULE["预付两月"]["cycle"], SETTLE_RULE["预付两月"]["prepay"]), (2, True))
        self.assertEqual((SETTLE_RULE["预付下季"]["cycle"], SETTLE_RULE["预付下季"]["prepay"]), (3, True))


class TestGrouping(unittest.TestCase):
    def test_pay_current_month_every_month(self):
        """付当月：12 个月 → 12 笔，每月一笔。"""
        n = nodes("付当月")
        self.assertEqual(len(n), 12)
        self.assertTrue(all(x["金额"] == Decimal("1000") for x in n))
        self.assertEqual(n[0]["付款月"], "2025-07")

    def test_prepay_next_month_first_node_before_lease(self):
        """预付下月：首个付款节点是租赁首月的前一个月（6 月付 7 月）。"""
        n = nodes("预付下月")
        self.assertEqual(len(n), 12)
        self.assertEqual(n[0]["付款月"], "2025-06")
        self.assertEqual(n[0]["费用月份"], ["2025-07"])

    def test_pay_two_months_merges(self):
        """付两月：12 个月 → 6 笔，每笔覆盖两个月。"""
        n = nodes("付两月")
        self.assertEqual(len(n), 6)
        self.assertEqual(n[0]["费用月份"], ["2025-07", "2025-08"])
        self.assertEqual(n[0]["金额"], Decimal("2000"))
        self.assertEqual(n[1]["付款月"], "2025-09")

    def test_prepay_two_months_starts_month_before(self):
        """预付两月：首节点 6 月，覆盖 7-8 月。"""
        n = nodes("预付两月")
        self.assertEqual(len(n), 6)
        self.assertEqual(n[0]["付款月"], "2025-06")
        self.assertEqual(n[0]["费用月份"], ["2025-07", "2025-08"])

    def test_prepay_quarter(self):
        """预付下季：12 个月 → 4 笔，首节点 6 月，覆盖 7-9 月。"""
        n = nodes("预付下季")
        self.assertEqual(len(n), 4)
        self.assertEqual(n[0]["付款月"], "2025-06")
        self.assertEqual(n[0]["费用月份"], ["2025-07", "2025-08", "2025-09"])
        self.assertEqual(n[1]["付款月"], "2025-09")

    def test_pay_year_single_node(self):
        """付当年：12 个月 → 1 笔。"""
        self.assertEqual(len(nodes("付当年")), 1)

    def test_all_zero_group_skipped(self):
        """整段为 0（全免租）不生成付款节点。"""
        amts = [("2025-07", Decimal("0")), ("2025-08", Decimal("0")), ("2025-09", Decimal("100"))]
        self.assertEqual(len(nodes("付两月", amts=amts)), 1)

    def test_partial_tail_group_kept(self):
        """不足一个周期的尾段照常生成一笔。"""
        amts = [(m, Decimal("100")) for m in MONTHS[:5]]
        n = nodes("付两月", amts=amts)
        self.assertEqual(len(n), 3)
        self.assertEqual(n[2]["费用月份"], ["2025-11"])

    def test_total_preserved(self):
        """分组不改变合计金额。"""
        for s in SETTLE_RULE:
            self.assertEqual(sum(x["金额"] for x in nodes(s)), Decimal("12000"), s)


class TestPeriodLabel(unittest.TestCase):
    def test_single_month(self):
        self.assertEqual(_period_label(["2026-04"]), "2026年04月")

    def test_range_same_year(self):
        self.assertEqual(_period_label(["2026-04", "2026-05"]), "2026年4至5月")
        self.assertEqual(_period_label(["2026-05", "2026-06", "2026-07"]), "2026年5至7月")

    def test_range_across_years(self):
        self.assertEqual(_period_label(["2025-12", "2026-01"]), "2025年12月至2026年1月")


if __name__ == '__main__':
    unittest.main(verbosity=2)
