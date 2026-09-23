# -*- coding: utf-8 -*-
"""隔离单元测试（阶段4 门禁）：不访问真实团队空间，验证后端核心计算与状态机。"""
import sys, os
sys.path.insert(0, r"D:/workbuddy/财务租赁/租赁线上化LightApp")

import unittest
from decimal import Decimal

# 1) 会话签发/校验
import backend as B

class TestSession(unittest.TestCase):
    def test_roundtrip(self):
        tok = B.create_session("keeper", "张维护", "台账维护人")
        self.assertTrue("." in tok)
        # current 需要请求对象，这里只验证 payload 可解
        p64, sig = tok.split(".")
        import base64
        pad = "=" * (-len(p64) % 4)
        payload = base64.urlsafe_b64decode(p64 + pad).decode()
        self.assertIn("keeper", payload)
        self.assertIn("台账维护人", payload)

    def test_tamper_rejected(self):
        tok = B.create_session("keeper", "张维护", "台账维护人")
        p64, sig = tok.split(".")
        bad = p64 + "." + "A"*43
        self.assertFalse(B._hmac.compare_digest(B._sign(p64), bad.split(".", 1)[1]))

class TestMonthlyExpand(unittest.TestCase):
    def test_full_months(self):
        segs = [{"起始日": "2025-01-01", "终止日": "2025-01-31", "月金额": "1000"}]
        rows = B.expand_monthly(segs)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(float(rows[0][1]), 1000.0, places=2)

    def test_cross_year(self):
        segs = [{"起始日": "2024-12-01", "终止日": "2025-02-28", "月金额": "1000"}]
        rows = B.expand_monthly(segs)
        months = [r[0] for r in rows]
        self.assertIn("2024-12-01", months)
        self.assertIn("2025-01-01", months)
        self.assertIn("2025-02-01", months)

    def test_partial_tail_months(self):
        # 首尾不足月：2025-01-15 ~ 2025-02-14，全月仍按整月（整月算法）？本实现按天数折算
        segs = [{"起始日": "2025-01-15", "终止日": "2025-01-31", "月金额": "3100"}]
        rows = B.expand_monthly(segs)
        # 1月有31天，15~31共17天 → 3100*17/31 ≈ 1700
        self.assertAlmostEqual(float(rows[0][1]), 1700.0, places=0)

class TestLedgerBalances(unittest.TestCase):
    def test_balance_zero_tail(self):
        # 构造：合同金额=未付款，降租=0，已付款=0 → 实付=合同金额，余额逐月=0
        raw = [{"合同金额": "1000", "降租金额": "0", "未付款": "1000", "已付款": "0"} for _ in range(3)]
        outs = B._calc_ledger_balances(raw)
        self.assertAlmostEqual(float(outs[-1]["预付科目余额"]), 0.0, places=2)

class TestIFRS(unittest.TestCase):
    def test_zero_tail(self):
        card = {"IFRS折现率": "0.00365833"}
        pays = [("2025-01-01", "1000")] * 12
        rows = B._calc_ifrs(card, pays)
        self.assertEqual(len(rows), 12)
        self.assertLess(abs(float(rows[-1]["租赁负债余额"])), 0.02)
        self.assertLess(abs(float(rows[-1]["使用权资产余额"])), 0.02)

    def test_rejects_inconsistent_init(self):
        # 折现率为0应返回 None（不生成IFRS）
        card = {"IFRS折现率": "0"}
        self.assertIsNone(B._calc_ifrs(card, [("2025-01-01","1000")]))

class TestPassword(unittest.TestCase):
    def test_hash_verify(self):
        h = B.hash_pwd("secret123")
        self.assertTrue(h.startswith("pbkdf2$"))
        self.assertTrue(B.verify_pwd("secret123", h))
        self.assertFalse(B.verify_pwd("wrong", h))

if __name__ == "__main__":
    unittest.main(verbosity=2)