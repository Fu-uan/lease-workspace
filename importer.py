"""Bounded, local document extraction. Candidates always need human review."""
import base64
import csv
import datetime
import io
import os
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

LABELS = {
    'contract_no': ['合同编号', '合同号', '合同编码'], 'oa_no': ['OA单号'],
    'lessor': ['出租方', '出租人', '甲方'], 'lessee': ['承租方', '承租人', '乙方'],
    'lessor_id': ['出租方统一社会信用代码', '甲方统一社会信用代码', '甲方信用代码'],
    'lessee_id': ['承租方统一社会信用代码', '乙方统一社会信用代码', '乙方信用代码'],
    'lessor_bank': ['出租方开户银行', '甲方开户银行'],
    'lessee_bank': ['承租方开户银行', '乙方开户银行'],
    'lessor_account': ['出租方银行账号', '甲方银行账号'],
    'lessee_account': ['承租方银行账号', '乙方银行账号'],
    'start_date': ['租赁起始日', '起始日', '租赁开始日期', '起租日'],
    'end_date': ['租赁终止日', '终止日', '租赁结束日期', '到期日'],
    'address': ['租赁地址', '场地地址', '房屋坐落', '坐落', '租赁房屋'],
    'property_addr': ['租赁物坐落', '房屋坐落', '标的物坐落', '租赁物位置'],
    'sign_place': ['签订地点', '签约地点', '签署地点'],
    'monthly_amount': ['月租金', '月租金金额', '月租金（不含税）'],
    'monthly_fee': ['月管理费', '管理费', '物业管理费', '物业服务费'],
    'deposit': ['保证金', '押金', '租赁保证金', '履约保证金'],
    'tax_rate': ['税率'],
    'free_rent': ['免租期'],
    'department': ['申请部门', '提单部门'], 'applicant': ['申请人'],
    'area': ['计租面积', '建筑面积', '租赁面积', '使用面积'],
    'escalation': ['递增约定', '租金递增', '递增方式', '年递增', '递增率'],
    'settlement': ['结算方式', '付款方式', '支付方式'],
}

# 租期以「区间」形式出现时的标签（如：租赁期限：2025年01月01日至2029年12月31日）
RANGE_LABELS = ['租赁期限', '租赁期间', '合同期限', '合作期限', '租赁期', '租期']

DATE = r'(\d{4}\s*[年/.\-]\s*\d{1,2}\s*[月/.\-]\s*\d{1,2}\s*日?)'
DATE_SEP = r'\s*(?:起\s*)?(?:至|到|~|—|–|-)\s*(?:止\s*)?'
MUNICIPALITIES = ('北京市', '上海市', '天津市', '重庆市')


def norm_date(value):
    """把 2025年1月1日 / 2025-01-01 / 2025.1.1 统一为 YYYY-MM-DD。"""
    m = re.fullmatch(r'(\d{4})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})\s*日?', (value or '').strip())
    if not m:
        return None
    return f'{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}'


def split_region(address):
    """从租赁地址拆分省 / 市 / 区镇；识别不到就留空，不编造。"""
    out = {}
    if not address:
        return out
    addr = address.strip()
    for city in MUNICIPALITIES:
        if addr.startswith(city):
            out['province'] = city
            out['city'] = city
            rest = addr[len(city):]
            m = re.match(r'([\u4e00-\u9fa5]{2,10}?(?:区|县|旗))', rest)
            if m:
                out['district'] = m.group(1)
            return out
    m = re.match(r'([\u4e00-\u9fa5]{2,10}?(?:省|自治区|特别行政区))', addr)
    rest = addr
    if m:
        out['province'] = m.group(1)
        rest = addr[m.end():]
    m = re.match(r'([\u4e00-\u9fa5]{2,10}?(?:市|自治州|地区|盟))', rest)
    if m:
        out['city'] = m.group(1)
        rest = rest[m.end():]
    m = re.match(r'([\u4e00-\u9fa5]{2,10}?(?:区|县|旗|市))', rest)
    if m:
        out['district'] = m.group(1)
    return out


def _labeled_value(text, labels):
    """匹配「标签[(单位)]：值」，标签后的括号单位可省略。允许『2.1』『第一条』等编号前缀（不要求行首）；一行多字段时用双空格截断。"""
    alt = '|'.join(re.escape(x) for x in labels)
    pattern = (r'(?:' + alt + r')[ \t]*(?:[（(][^）)\r\n]{0,12}[）)])?'
               r'[ \t]*[:：\t][ \t]*([^\r\n]+?)(?=[ \t]{2,}|[ \t]*\r?$)')
    values = [x.strip() for x in re.findall(pattern, text, re.MULTILINE) if x.strip()]
    # OCR常丢失冒号；对金额、面积等标签允许“标签 空格 数值”，但不对主体/自由文本放宽，避免吞整段合同正文。
    if any(k in ''.join(labels) for k in ('面积', '租金', '管理费', '保证金', '押金')):
        loose = (r'(?:' + alt + r')[ \t]*(?:[（(][^）)\r\n]{0,12}[）)])?'
                 r'[ \t]+([￥¥]?[ \t]*\d[\d,，.]*)')
        values.extend(x.strip() for x in re.findall(loose, text, re.MULTILINE) if x.strip())
    return values


def _parse_number(s):
    """把金额串解析为 float。容忍 OCR 把千分位逗号误读成点（『12.000.00』→12000.00）。"""
    s = (s or '').strip().replace(' ', '').replace('￥', '').replace('¥', '')
    s = s.strip('.,，').replace('，', ',')          # 去掉被字符类一并吞入的首尾分隔符
    if not s:
        return None
    m = re.fullmatch(r'(\d{1,3}(?:[.,]\d{3})+)[.,](\d{1,2})', s)
    if m:
        return float(m.group(1).replace('.', '').replace(',', '') + '.' + m.group(2))
    try:
        return float(s.replace(',', ''))
    except ValueError:
        return None


def clean_amount(s):
    """从『人民币捌万陆仟元整（￥86,000.00）』/『￥12,000.00』/『294,000.00』中抽出浮点数值。"""
    if not s:
        return None
    m = (re.search(r'[￥¥]\s*([\d,，.]+(?:\.\d+)?)', s)
         or re.search(r'[（(]\s*([\d,，.]+(?:\.\d+)?)', s)
         or re.search(r'([\d,，.]+(?:\.\d+)?)', s))
    if not m:
        return None
    return _parse_number(m.group(1))


def _period_offsets(period, ref):
    """把『2025年1至3月』解析为相对偏移 (off_start, off_end)，ref=(年,月) 为基准年月。"""
    m = re.search(r'(\d{4})\s*年\s*(\d{1,2})(?:\s*月)?(?:\s*(?:至|到|-|—|–)\s*(\d{1,2})\s*月)?', period or '')
    if not m:
        return None
    y1, m1 = int(m.group(1)), int(m.group(2))
    m2 = int(m.group(3)) if m.group(3) else m1
    ry, rm = ref
    off_s = (y1 - ry) * 12 + (m1 - rm) + 1
    off_e = (y1 - ry) * 12 + (m2 - rm) + 1
    if off_s < 1 or off_e < off_s:
        return None
    return (off_s, off_e)


# 付款计划表：兼容「整行式」与「单元格逐行式（列状）」两种 OCR 版式
_P_HDR_WORDS = ('期次', '应付日', '费用所属期间', '租金', '管理费', '合计', '税率',
                '付款日', '期间', '月份', '金额', '序号', '本期应付')
_P_TERM = re.compile(r'注[:：]|第[四五六七八九十]条|费用部门分摊|一次性费用|其他约定|上表|逐年递增逐')
_P_PERIOD = re.compile(r'\d{4}\s*年\s*\d{1,2}')
_P_DATE = re.compile(r'\d{4}-\d{1,2}-\d{1,2}')
_P_AMT = re.compile(r'[¥￥]?\s*[\d,，.]+\.\d{2}')
_P_TAX = re.compile(r'[%\d\s]{1,6}/[%\d\s]{1,6}')


def _is_header_line(s):
    t = re.sub(r'[（(][^）)]*[）)]', '', s).strip()
    if not t or re.search(r'\d', t) or len(t) > 8:
        return False
    return any(w in t for w in _P_HDR_WORDS)


def _parse_payment_table(text, start_iso=None):
    """解析付款计划表。兼容两种 OCR 版式：
    ① 整行式：一行含「期间 租金 管理费 合计 税率」；
    ② 列状式：每个单元格单独成行、且顺序可能错乱——按 token 分类，并以税率/期次切行。
    租金/管理费/合计用「合计 = 其余两项之和」判定，避免依赖列序。
    """
    rows, lines = [], [x.strip() for x in (text or '').splitlines()]
    start = None
    for i, ln in enumerate(lines):
        if re.search(r'付款计划表|付款计划|付款安排|支付计划|租金支付计划|付款明细|费用所属期间', ln):
            start = i
            break
    if start is None:
        for i, ln in enumerate(lines):
            if re.search(r'应付日', ln) and re.search(r'租金|合计', ln):
                start = i
                break
    if start is None:
        return rows

    # 收集数据 token（跳过表头行，遇注释/下一节停止）
    toks = []
    for ln in lines[start + 1:]:
        if not ln:
            continue
        if _P_TERM.search(ln):
            break
        if _is_header_line(ln):
            continue
        toks.extend(t for t in re.split(r'\s+', ln) if t)

    def _has_period(c):
        return any(_P_PERIOD.match(t) for t in c)

    def _amt_n(c):
        return sum(1 for t in c if _P_AMT.fullmatch(t))

    # 切行：税率 token 收尾；或缺税率列时遇新期次且本行已含「期次 + ≥3 金额」
    groups, cur = [], []
    for t in toks:
        if _P_TAX.fullmatch(t) and '/' in t:
            cur.append(t)
            groups.append(cur)
            cur = []
            continue
        if _P_PERIOD.match(t) and _has_period(cur) and _amt_n(cur) >= 3:
            groups.append(cur)
            cur = []
        cur.append(t)
    if cur:
        groups.append(cur)

    ref = None
    if start_iso:
        m = re.match(r'(\d{4})-(\d{1,2})', start_iso)
        if m:
            ref = (int(m.group(1)), int(m.group(2)))

    seq = 0
    for g in groups:
        ptok = next((t for t in g if _P_PERIOD.match(t)), '')
        if not ptok:
            continue
        pm = re.match(r'\d{4}\s*年\s*\d{1,2}\s*月?(?:\s*(?:至|到|-|—|–)\s*\d{1,2}\s*月)?', ptok)
        period = pm.group(0) if pm else ptok
        date_tok = next((t for t in g if _P_DATE.fullmatch(t)), '')
        amts = sorted({clean_amount(t) for t in g
                       if _P_AMT.fullmatch(t) and clean_amount(t) is not None}, reverse=True)
        if not amts:
            continue
        total = rent = mgmt = None
        if len(amts) >= 3:
            for i, a in enumerate(amts):
                rest = [x for j, x in enumerate(amts) if j != i]
                if len(rest) >= 2 and abs(a - (rest[0] + rest[1])) < 0.01:
                    total, rent, mgmt = a, max(rest[0], rest[1]), min(rest[0], rest[1])
                    break
        if total is None:                       # 退化：最大值为合计，其余降序作租金/管理费
            total = amts[0]
            rent = amts[1] if len(amts) > 1 else 0.0
            mgmt = amts[2] if len(amts) > 2 else 0.0
        tax_tok = next((t for t in g if _P_TAX.fullmatch(t) and '/' in t), '')
        tn = re.findall(r'\d{1,2}', tax_tok)
        tax = '%s%%/%s%%' % (tn[0], tn[1]) if len(tn) >= 2 else ''
        if ref is None:
            m0 = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月', period)
            if m0:
                ref = (int(m0.group(1)), int(m0.group(2)))
        off = _period_offsets(period, ref) if ref else None
        if not off:
            continue
        seq += 1
        months = off[1] - off[0] + 1
        rows.append({
            'seq': seq, 'period': period, 'pay_date': date_tok,
            'rent': '%.2f' % rent, 'mgmt': '%.2f' % mgmt, 'total': '%.2f' % total,
            'tax': tax, 'off_start': off[0], 'off_end': off[1],
            'monthly': '%.2f' % (total / months if months > 0 else total),
        })
    return rows


_S_TERM = re.compile(r'第[五六七八九十]条|一次性费用|其他约定|印花税|中介服务费|装修押金')
_S_RATIO = re.compile(r'\d{1,3}\s*%')
_S_CC = re.compile(r'[A-Za-z]{2,}[-\w]+')


def _parse_sharing(text):
    """解析『费用部门分摊比例』表，返回 {dept, ratio(0-1), cost_center, note}。
    兼容整行式与列状逐行式（比例/部门/成本中心/备注列序可能错乱）——先按 token 分类，再按出现顺序对齐。"""
    rows, lines = [], [x.strip() for x in (text or '').splitlines()]
    start = None
    for i, ln in enumerate(lines):
        if '分摊比例' in ln:
            start = i
            break
    if start is None:
        return rows

    def _is_hdr(s):
        toks = [t for t in re.split(r'\s+', re.sub(r'[（(][^）)]*[）)]', '', s)) if t]
        if not toks:
            return False
        return all(len(t) <= 8 and not re.search(r'\d', t)
                   and any(w in t for w in ('承担部门', '分摊比例', '成本中心', '备注', '费用部门'))
                   for t in toks)

    depts, ratios, ccs, notes = [], [], [], []
    for ln in lines[start + 1:]:
        if not ln:
            continue
        if _S_TERM.search(ln):
            break
        if _is_hdr(ln):
            continue
        for t in re.split(r'\s+', ln):          # 逐 token 分类，兼容「整行式」与「单元格逐行式」
            if not t:
                continue
            if _S_RATIO.fullmatch(t):
                ratios.append(float(re.findall(r'\d{1,3}', t)[0]) / 100.0)
            elif _S_CC.fullmatch(t) and '-' in t:
                ccs.append(t)
            elif re.search(r'(部|中心|室|组|科|处|公司|工厂|车间|事业部|研究院)$', t) and len(t) <= 20:
                depts.append(t)
            else:
                notes.append(t)
    n = max(len(ratios), len(depts), len(ccs))
    if n == 0:
        return rows
    for i in range(n):
        rows.append({
            'dept': depts[i] if i < len(depts) else '',
            'ratio': round(ratios[i], 4) if i < len(ratios) else 0.0,
            'cost_center': ccs[i] if i < len(ccs) else '',
            'note': notes[i] if i < len(notes) else '',
        })
    return rows


_OO_TERM = re.compile(r'第[七八九十]条|其他约定|签章|争议|本合同一式')


def _oneoff_type(name):
    """把一次性费用名称归一到 oneoff_fees.费用类型 的枚举：
    租赁保证金 / 物管保证金 / 水电保证金 / 其他保证金 / 预付租金；不匹配返回空串（靠说明承载）。"""
    s = name or ''
    if re.search(r'预付', s):
        return '预付租金'
    if re.search(r'水电', s):
        return '水电保证金'
    if re.search(r'物业|物管', s):
        return '物管保证金'
    if re.search(r'装修|履约|其他', s):
        return '其他保证金'
    if re.search(r'保证金|押金', s):
        return '租赁保证金'
    return ''


def _parse_oneoff(text, deposit=None):
    """解析「第六条 一次性费用及税费」（中介服务费/印花税/装修押金…），返回
    [{费用类型, 金额, 说明}]；另把第三条的「保证金」作为一条租赁保证金补入。"""
    rows, lines = [], [x.strip() for x in (text or '').splitlines()]
    start = None
    for i, ln in enumerate(lines):
        if re.search(r'一次性费用|其他一次性|一次性支出', ln):
            start = i
            break
    if start is not None:
        for ln in lines[start + 1:]:
            if not ln:
                continue
            if _OO_TERM.search(ln):
                break
            m = (re.match(r'\d+(?:\.\d+)?\s*([\u4e00-\u9fa5]{2,10})\s*[:：]\s*(.+)', ln)
                 or re.match(r'([\u4e00-\u9fa5]{2,10}(?:费|税|押金|保证金))\s*[:：]\s*(.+)', ln))
            if not m:
                continue
            name, detail = m.group(1), m.group(2).strip().rstrip('。')
            amount = clean_amount(detail)
            if amount is None:
                continue
            rows.append({'费用类型': _oneoff_type(name), '金额': '%.2f' % amount,
                         '说明': name + '：' + detail})
    if deposit is not None:
        rows.insert(0, {'费用类型': '租赁保证金', '金额': '%.2f' % float(deposit),
                        '说明': '保证金（合同载明）'})
    return rows


def _parse_tax_rate(text, payment_table):
    """税率：优先取付款计划表里出现最多的税率（如 9%/6%），否则回退文本里的「税率9%」。"""
    vals = [r.get('tax') for r in (payment_table or []) if r.get('tax')]
    if vals:
        vals.sort(key=lambda v: (-vals.count(v), v))
        return vals[0]
    found = re.findall(r'(?:增值税)?税率\s*[:：]?\s*(\d{1,3})\s*%', text or '')
    uniq = list(dict.fromkeys(found))
    return '/'.join('%s%%' % x for x in uniq) if uniq else ''


def _labeled_number(text, labels):
    """数字类字段的宽松兜底：OCR 可能丢掉冒号，直接取标签后第一个数字。"""
    alt = '|'.join(re.escape(x) for x in labels)
    pattern = (r'(?:^|\n)\s*(?:' + alt + r')\s*(?:[（(][^）)\n]{0,12}[）)])?\s*[:：\t]?\s*'
               r'[^\d\n]{0,6}?([\d,]+(?:\.\d+)?)')
    out = []
    for m in re.findall(pattern, text):
        cleaned = re.sub(r'[,，]', '', m)
        if re.fullmatch(r'\d+(?:\.\d+)?', cleaned):
            out.append(cleaned)
    return out


def _party_name(text, party, role):
    """取甲/乙方名称：优先『甲方（出租方）：XXX』，其次『出租方：XXX』，最后首个『甲方：XXX』。
    甲/乙方在条款正文中会频繁出现，交给通用标签循环会被判为多值冲突而丢弃，故单独处理。"""
    m = (re.search(re.escape(party) + r'\s*[（(]\s*' + re.escape(role) + r'\s*[）)]\s*[:：]?\s*([^\n，。；、]{2,40})', text)
         or re.search(r'(?:^|\n)\s*' + re.escape(role) + r'\s*[:：]\s*([^\n，。；、]{2,40})', text)
         or re.search(r'(?:^|\n)\s*' + re.escape(party) + r'\s*[:：]\s*([^\n，。；、]{2,40})', text))
    if not m:
        return None
    v = re.split(r'\s{2,}', m.group(1).strip())[0]      # 同行含「甲…乙…」时，双空格截断
    v = re.split(r'[甲乙]方[（(]', v)[0].strip()
    return v or None


def _party_candidates(text, party, role):
    """返回同一主体标签的候选名称；多个候选必须进入人工冲突，不自动挑第一个。"""
    patterns = [
        re.escape(party) + r'\s*[（(]\s*' + re.escape(role) + r'\s*[）)]\s*[:：]?\s*([^\n，。；、]{2,40})',
        r'(?:^|\n)\s*' + re.escape(role) + r'\s*[:：]\s*([^\n，。；、]{2,40})',
        r'(?:^|\n)\s*' + re.escape(party) + r'\s*[:：]\s*([^\n，。；、]{2,40})',
    ]
    out = []
    for pat in patterns:
        for x in re.findall(pat, text, re.MULTILINE):
            v = re.split(r'\s{2,}', x.strip())[0]
            v = re.split(r'[甲乙]方[（(]', v)[0].strip()
            if v and v not in out:
                out.append(v)
    return out


def extract(text):
    fields, sources, conflicts = {}, {}, {}
    # 1) 租赁期限区间（最可靠）：优先带标签，退化到一行两日期
    range_pattern = (r'(?:' + '|'.join(RANGE_LABELS) + r')[^\n]{0,10}?[:：]?\s*'
                     + DATE + DATE_SEP + DATE)
    m = re.search(range_pattern, text)
    if not m:
        m = re.search(DATE + DATE_SEP + DATE, text)
    if m:
        start, end = norm_date(m.group(1)), norm_date(m.group(2))
        if start:
            fields['start_date'] = start
            sources['start_date'] = m.group(1)
        if end:
            fields['end_date'] = end
            sources['end_date'] = m.group(2)

    # 2) 逐标签取值（金额类用 clean_amount 处理中文大写 / 全角逗号 / ￥）
    NUMERIC = ('monthly_amount', 'monthly_fee', 'deposit', 'area')   # tax_rate 单独解析（含 9%/6% 这类写法）
    for key, labels in LABELS.items():
        if key in ('lessor', 'lessee'):
            continue   # 甲/乙方名单独处理（见 2.1），避免条款中的重复出现被误判为冲突而丢弃
        values = list(dict.fromkeys(_labeled_value(text, labels)))
        if len(values) > 1:
            conflicts[key] = values
            continue
        if not values:
            continue
        value = values[0]
        if key in ('start_date', 'end_date'):
            continue  # 已由区间解析
        if key in NUMERIC:
            cleaned = clean_amount(value)
            if cleaned is None:
                continue
            value = '%.2f' % cleaned
        fields[key] = value
        sources[key] = values[0]

    # 2.1) 甲/乙方名称（条款正文里 甲方/乙方 反复出现，通用循环会判为冲突而丢弃）
    party_vals = _party_candidates(text, '甲方', '出租方')
    if len(party_vals) > 1:
        conflicts['lessor'] = party_vals
    pn = party_vals[0] if len(party_vals) == 1 else None
    if pn:
        fields['lessor'] = pn
        sources['lessor'] = pn
        conflicts.pop('lessor', None)
    party_vals = _party_candidates(text, '乙方', '承租方')
    if len(party_vals) > 1:
        conflicts['lessee'] = party_vals
    pn = party_vals[0] if len(party_vals) == 1 else None
    if pn:
        fields['lessee'] = pn
        sources['lessee'] = pn
        conflicts.pop('lessee', None)

    # 3) 派生：含税状态 / 结算方式归一 / 签订地点拆分
    if re.search(r'不含税', text):
        fields['tax_incl'] = '不含税'
    elif re.search(r'含税', text):
        fields['tax_incl'] = '含税'
    if fields.get('settlement'):
        sm = re.search(r'付[当每两半当年下]+\s*[\u4e00-\u9fa5]{0,4}', fields['settlement'])
        if sm:
            fields['settlement'] = sm.group(0).strip()

    # 3.1) 标签相同的配对字段：按出现顺序分配给甲/乙（首见=出租方，次见=承租方）
    for label, (k_first, k_sec) in (
        ('统一社会信用代码', ('lessor_id', 'lessee_id')),
        ('开户银行', ('lessor_bank', 'lessee_bank')),
        ('银行账号', ('lessor_account', 'lessee_account')),
    ):
        vals = _labeled_value(text, [label])
        if len(vals) >= 1 and k_first not in fields:
            fields[k_first] = vals[0]
        if len(vals) >= 2 and k_sec not in fields:
            fields[k_sec] = vals[1]

    # 3.2) 保证金精确优先：规避「租赁保证金」与「装修押金/租房押金」等歧义项
    #       在通用循环里若同时命中多标签会被判为冲突而丢弃，这里优先取精确词。
    if 'deposit' not in fields:
        for dlabel in ('保证金', '租赁保证金', '履约保证金'):
            dv = _labeled_value(text, [dlabel])
            if dv:
                dc = clean_amount(dv[0])
                if dc is not None:
                    fields['deposit'] = '%.2f' % dc
                    sources['deposit'] = dv[0]
                    conflicts.pop('deposit', None)
                break

    # 4) 付款计划表 + 分摊比例 + 一次性费用 + 税率
    payment_table = _parse_payment_table(text, fields.get('start_date'))
    sharing = _parse_sharing(text)
    oneoff_fees = _parse_oneoff(text, fields.get('deposit'))
    _tr = _parse_tax_rate(text, payment_table)
    if _tr:
        fields['tax_rate'] = _tr
        sources['tax_rate'] = _tr
    segments = ['%d-%d:%s' % (r['off_start'], r['off_end'], r['monthly']) for r in payment_table]

    # 5) 地址优先用「租赁物坐落」，再拆省市区
    if fields.get('property_addr') and not fields.get('address'):
        fields['address'] = fields['property_addr']
    if fields.get('address'):
        fields.update(split_region(fields['address']))

    return {'fields': fields, 'sources': sources, 'conflicts': conflicts,
            'payment_table': payment_table, 'sharing': sharing, 'oneoff_fees': oneoff_fees,
            'segments': segments, 'text': text, 'requires_review': True,
            'field_status': field_status(fields)}


# ---- OCR 字段状态与租期覆盖检查（人工核对闸口）------------------------------
# 状态取值：未识别 / 已识别待核对 / 人工已确认 / 核对失败
FIELD_LABELS = {
    'card_code': '合同编码', 'contract_no': '合同编号', 'card_name': '卡片名称',
    'tenant': '承租方', 'lessor': '出租方', 'payer': '实际付款方', 'receiver': '实际收款方',
    'address': '租赁地址', 'property_addr': '租赁物坐落', 'province': '省级',
    'city': '市级', 'district': '区镇', 'area': '计租面积', 'monthly_rent': '月租金',
    'start_date': '租赁起始日', 'end_date': '租赁终止日', 'tax_rate': '税率',
    'ifrs': '是否IFRS', 'discount_rate': 'IFRS折现率', 'settle': '结算方式',
    'deposit': '保证金', 'rent_rise': '递增约定', 'free_period': '免租期',
}
# 必须人工确认的关键字段（缺失或未确认不得提交）
KEY_FIELDS = ('card_code', 'contract_no', 'tenant', 'lessor', 'address',
              'start_date', 'end_date', 'area', 'tax_rate', 'discount_rate')


def field_status(fields):
    """逐字段给出 OCR 来源与核对状态；未识别的字段显式留空，绝不填充猜测值。"""
    out = {}
    keys = set(FIELD_LABELS) | set((fields or {}).keys())
    for k in sorted(keys):
        v = (fields or {}).get(k)
        empty = v is None or str(v).strip() in ('', '0', '0.0')
        out[k] = {
            'label': FIELD_LABELS.get(k, k),
            'source': 'OCR',
            'status': '未识别' if empty else '已识别待核对',
            'manual_confirm_required': k in KEY_FIELDS,
        }
    return out


def _months_between(start, end):
    s, e = norm_date(start), norm_date(end)
    if not s or not e or len(s) < 10 or len(e) < 10:
        return None, None
    try:
        d1 = datetime.date.fromisoformat(s[:10])
        d2 = datetime.date.fromisoformat(e[:10])
    except ValueError:
        return None, None
    if d2 < d1:
        return None, None
    months = []
    cur = datetime.date(d1.year, d1.month, 1)
    last = datetime.date(d2.year, d2.month, 1)
    while cur <= last:
        months.append('%04d-%02d' % (cur.year, cur.month))
        cur = (datetime.date(cur.year + 1, 1, 1) if cur.month == 12
               else datetime.date(cur.year, cur.month + 1, 1))
    return months, (d1, d2)


def coverage_check(start, end, segments):
    """付款计划是否覆盖完整租期：缺失月份 / 重复月份 / 是否完整。

    segments 支持两种形态：{'起始日','终止日'} 或 {'期次起','期次止','金额'}+ref。
    这里只处理已解析成起止日期的段落（与 backend 口径一致）。
    """
    expected, _ = _months_between(start, end)
    if expected is None:
        return {'missing': [], 'duplicated': [], 'complete': False,
                'error': '起止日期缺失或非法'}
    counter = {}
    for seg in segments or []:
        a = norm_date(seg.get('起始日') or seg.get('start'))
        b = norm_date(seg.get('终止日') or seg.get('end'))
        ms, _r = _months_between(a, b)
        for m in (ms or []):
            counter[m] = counter.get(m, 0) + 1
    missing = sorted(set(expected) - set(counter))
    duplicated = sorted([m for m, n in counter.items() if n > 1])
    return {'missing': missing, 'duplicated': duplicated,
            'complete': (not missing and not duplicated),
            'expected_months': len(expected), 'covered_months': len(counter)}


def ocr(data):
    try:
        from PIL import Image
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        raise ValueError('扫描识别组件未安装，请让部署人员安装 requirements-import.txt；也可手动登记。')
    with Image.open(io.BytesIO(data)) as im:
        if im.width * im.height > 24000000:
            raise ValueError('图片超过2400万像素，请压缩后重试')
        im.thumbnail((2200, 2200))
        result, _ = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)(np.array(im.convert('RGB')))
    return '\n'.join(row[1] for row in (result or []))


def parse(data):
    name = data.get('filename', '')
    if not isinstance(name, str) or len(name) > 200:
        raise ValueError('文件名无效')
    ext = Path(name).suffix.lower()
    if ext not in ['.txt', '.csv', '.docx', '.xlsx', '.pdf', '.png', '.jpg', '.jpeg']:
        raise ValueError('支持 PDF、DOCX、XLSX、CSV、TXT、JPG、PNG 文件')
    try:
        raw = base64.b64decode(data.get('content', ''), validate=True)
    except (ValueError, TypeError):
        raise ValueError('文件编码无效')
    max_mb = int(os.environ.get('ZL_UPLOAD_MAX_MB', '50'))
    max_pages = int(os.environ.get('ZL_PDF_MAX_PAGES', '50'))
    if not 0 < len(raw) <= max_mb * 1024 * 1024:
        raise ValueError(f'请选择不超过{max_mb}MB的文件')
    warnings = []
    if ext in ('.png', '.jpg', '.jpeg'):
        text = ocr(raw)
    elif ext == '.pdf':
        try:
            import pymupdf
        except ImportError:
            raise ValueError('PDF组件未安装，请让部署人员安装 requirements-import.txt')
        with pymupdf.open(stream=raw, filetype='pdf') as doc:
            if doc.needs_pass or len(doc) > max_pages:
                raise ValueError(f'请上传未加密、最多{max_pages}页的PDF；超过边界请联系管理员调整服务器配置')
            pages = []
            for page in doc:
                value = page.get_text()
                if len(value.strip()) < 20:
                    if page.rect.width * page.rect.height > 6000000:
                        raise ValueError('PDF页面尺寸过大')
                    value = ocr(page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).tobytes('png'))
                pages.append(value)
            text = '\n'.join(pages)
    elif ext in ('.docx', '.xlsx'):
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            if len(z.infolist()) > 5000 or sum(i.file_size for i in z.infolist()) > max_mb * 4 * 1024 * 1024:
                raise ValueError('文档解压后过大，请精简后重试')
            def xml(path):
                content = z.read(path)
                if b'<!DOCTYPE' in content or b'<!ENTITY' in content:
                    raise ValueError('不支持含实体声明的文档')
                return ET.fromstring(content)
            if ext == '.docx':
                ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                text = '\n'.join(''.join(p.itertext()) for p in xml('word/document.xml').findall('.//w:p', ns))
            else:
                ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                strings = [''.join(i.itertext()) for i in xml('xl/sharedStrings.xml')] if 'xl/sharedStrings.xml' in z.namelist() else []
                rows = []
                for row in xml('xl/worksheets/sheet1.xml').findall('.//s:row', ns):
                    values = []
                    for c in row.findall('s:c', ns):
                        value = c.findtext('s:v', '', ns)
                        if c.get('t') == 's': value = strings[int(value)]
                        elif c.get('t') == 'inlineStr': value = ''.join(c.itertext())
                        values.append(value)
                    rows.append(values)
                text = table_text(rows)
                warnings.append('仅读取第一个工作表；表格使用两列“字段、值”或表头加一行数据，日期请用YYYY-MM-DD文本。')
    else:
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('gb18030')
        if ext == '.csv':
            text = table_text(list(csv.reader(io.StringIO(text))))
    if len(text) > 60000:
        raise ValueError('文本超过6万字，请拆分关键页后导入')
    if not text.strip():
        raise ValueError('没有读到文字，请换一张清晰图片或手动登记')
    result = extract(text)
    result['warnings'] = warnings + ['识别结果仅为候选值，请逐项核对；文件不作为附件长期保存。']
    return result


def table_text(rows):
    rows = [r for r in rows if any(r)]
    if not rows:
        return ''
    if all(len(r) == 2 for r in rows):
        return '\n'.join(f'{r[0]}：{r[1]}' for r in rows)
    if len(rows) != 2:
        raise ValueError('当前一次导入一份合同，请使用字段/值两列，或表头加一行数据')
    return '\n'.join(f'{k}：{v}' for k, v in zip(*rows))


if __name__ == '__main__':
    try:
        result = parse(json.loads(sys.stdin.buffer.read()))
        sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
    except Exception as e:
        message = str(e) if isinstance(e, ValueError) else '文件无法读取，请检查文件格式与内容'
        sys.stdout.buffer.write(json.dumps({'error': message}, ensure_ascii=False).encode('utf-8'))
