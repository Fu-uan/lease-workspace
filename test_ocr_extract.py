# -*- coding: utf-8 -*-
"""OCR 字段与付款计划抽取的回归测试（不依赖 OCR 引擎，直接用识别文本）。

覆盖真实扫描件经 OCR 后得到的文本形态：表格被拆成独立行、标签后带（单位）、
租期以"至"连接的区间书写。运行：
    python test_ocr_extract.py
"""
import unittest

import importer
from backend import _extract_segments

# 示例合同扫描件（示例合同扫描件.png）OCR 后的真实文本
SAMPLE = """房屋租赁合同
合同编号：HT202500855
出租方：长沙某某商业管理有限公司
租赁地址：湖南省长沙市岳麓区麓谷大道658号A座12层
租赁期限：2025年01月01日至2029年12月31日
计租面积（平方米）：1,280.00
月租金（元）：43,580.00
递增约定：第2年起每年递增3%
付款计划表
期次
期间
月租金 (元)
1-1
第1个月
43580.00
2-2
第2个月
0.00
3-3
第3个月
21790.00
4-12
第4至12个月
43580.00
13-24
第13至24个月
44887.40
"""


class TestExtract(unittest.TestCase):
    def setUp(self):
        self.f = importer.extract(SAMPLE)['fields']

    def test_date_range_labels(self):
        """租期区间要同时得到起始日与终止日。"""
        self.assertEqual(self.f['start_date'], '2025-01-01')
        self.assertEqual(self.f['end_date'], '2029-12-31')

    def test_label_with_unit_suffix(self):
        """标签后的（单位）不应阻断取值。"""
        self.assertEqual(self.f['monthly_amount'], '43580.00')
        self.assertEqual(self.f['area'], '1280.00')

    def test_escalation(self):
        self.assertEqual(self.f['escalation'], '第2年起每年递增3%')

    def test_region_split(self):
        self.assertEqual(self.f['province'], '湖南省')
        self.assertEqual(self.f['city'], '长沙市')
        self.assertEqual(self.f['district'], '岳麓区')

    def test_base_fields(self):
        self.assertEqual(self.f['contract_no'], 'HT202500855')
        self.assertEqual(self.f['lessor'], '长沙某某商业管理有限公司')

    def test_absent_field_not_fabricated(self):
        """文档中没有的字段必须留空，不得编造。"""
        self.assertNotIn('lessee', self.f)


class TestSegments(unittest.TestCase):
    def test_flattened_table(self):
        """OCR 把期次/期间/金额拆成独立行时仍要配对正确。"""
        self.assertEqual(_extract_segments(SAMPLE),
                         ['1-1:43580.00', '2-2:0.00', '3-3:21790.00',
                          '4-12:43580.00', '13-24:44887.40'])

    def test_single_line_format(self):
        self.assertEqual(_extract_segments('1-1:100.00;2-3:200.00'),
                         ['1-1:100.00', '2-3:200.00'])

    def test_period_line_not_taken_as_amount(self):
        """"第4至12个月"是期间说明，不能被当成金额或期次。"""
        segs = _extract_segments('4-12\n第4至12个月\n500.00')
        self.assertEqual(segs, ['4-12:500.00'])

    def test_only_parses_payment_plan_section(self):
        """合同正文其他区域的数字不得与期次误配。"""
        text = ('租赁地址：某路1号\n建筑面积：1-2\n2000.00\n'
                '付款计划表\n1-1\n第1个月\n500.00\n')
        self.assertEqual(_extract_segments(text), ['1-1:500.00'])

    def test_reversed_period_dropped(self):
        """期次倒置（5-3）视为无效，不生成段。"""
        self.assertEqual(_extract_segments('付款计划\n5-3\n100.00\n'), [])


class TestConflict(unittest.TestCase):
    """同一字段多个取值时不擅自选择，登记为冲突。"""

    def test_conflicting_values_are_not_picked(self):
        r = importer.extract('出租方：A公司\n出租方：B公司')
        self.assertNotIn('lessor', r['fields'])
        self.assertEqual(r['conflicts']['lessor'], ['A公司', 'B公司'])

    def test_duplicate_same_value_is_not_conflict(self):
        r = importer.extract('出租方：A公司\n出租方：A公司')
        self.assertEqual(r['fields']['lessor'], 'A公司')
        self.assertNotIn('lessor', r['conflicts'])


class TestColonLost(unittest.TestCase):
    """OCR 有时会丢掉冒号，数字类字段仍需取到。"""

    def test_monthly_amount_without_colon(self):
        f = importer.extract('月租金 43,580.00\n租赁地址：某路1号')['fields']
        self.assertEqual(f.get('monthly_amount'), '43580.00')

    def test_area_without_colon(self):
        f = importer.extract('计租面积（平方米） 1,280.00')['fields']
        self.assertEqual(f.get('area'), '1280.00')

    def test_mixed_colon_values_keep_conflict(self):
        r = importer.extract('月租金：1,000.00\n月租金 2,000.00')
        self.assertNotIn('monthly_amount', r['fields'])
        self.assertEqual(len(r['conflicts']['monthly_amount']), 2)

    def test_empty_label_does_not_read_next_line(self):
        r = importer.extract('月租金：\n2025年1月起执行')
        self.assertNotIn('monthly_amount', r['fields'])

    def test_currency_and_ascii_unit(self):
        r = importer.extract('月租金 ¥ 2,000.00\n计租面积(平方米) 100.00')
        self.assertEqual(r['fields']['monthly_amount'], '2000.00')
        self.assertEqual(r['fields']['area'], '100.00')

    def test_non_numeric_label_value_not_taken_as_amount(self):
        """标签后是非数字说明时不产生金额。"""
        f = importer.extract('月租金按季度支付，详见附件')['fields']
        self.assertNotIn('monthly_amount', f)


class TestRegionSplit(unittest.TestCase):
    def test_municipality(self):
        r = importer.split_region('上海市浦东新区世纪大道100号')
        self.assertEqual(r['province'], '上海市')
        self.assertEqual(r['city'], '上海市')
        self.assertEqual(r['district'], '浦东新区')

    def test_empty_address(self):
        self.assertEqual(importer.split_region(''), {})

    def test_no_region_pattern(self):
        """地址不含行政区划时返回空，不猜测。"""
        self.assertEqual(importer.split_region('麓谷大道658号'), {})


if __name__ == '__main__':
    unittest.main(verbosity=2)
