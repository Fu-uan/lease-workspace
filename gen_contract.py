# -*- coding: utf-8 -*-
"""生成一张信息密集、刻意挑战 OCR 边界的模拟租赁合同。"""
import math, random
from PIL import Image, ImageDraw, ImageFont, ImageFilter

DPI = 200
W, H = 1654, 2339
MARGIN = 130
CW = W - 2 * MARGIN

FONT_DIR = "C:/Windows/Fonts/"
def font(name, size, idx=0):
    if name.endswith(".ttc"):
        return ImageFont.truetype(FONT_DIR + name, size, index=idx)
    return ImageFont.truetype(FONT_DIR + name, size)

F_HEI  = font("simhei.ttf", 30)          # 标题/小标题 黑体
F_HEI_B= font("simhei.ttf", 23)
F_YH   = font("msyh.ttc", 21, 0)         # 正文 雅黑
F_YH_S = font("msyh.ttc", 16, 0)         # 小字
F_YH_T = font("msyh.ttc", 12, 0)         # 极小脚注
F_SONG = font("simsun.ttc", 15, 0)       # 宋体细注
F_KAI  = font("simkai.ttf", 24)          # 手写体批注

WHITE, BLACK, RED, GREY = (255,255,255), (20,20,20), (178,34,34), (120,120,120)

img = Image.new("RGB", (W, H), WHITE)
d = ImageDraw.Draw(img)

# 极淡纸张底纹（横向暗纹）
for y in range(0, H, 26):
    d.line([(0, y), (W, y)], fill=(248,248,246), width=1)

y = 70
def nl(h=8): 
    global y; y += h

def text(s, f=F_YH, fill=BLACK, x=MARGIN, dy=4, center=False, w=None):
    global y
    if center:
        bb = d.textbbox((0,0), s, font=f)
        x = (W - (bb[2]-bb[0]))//2
    d.text((x, y), s, font=f, fill=fill)
    bb = d.textbbox((x, y), s, font=f)
    y = max(y, bb[3]) + dy
    return bb

def wrap(s, f, maxw):
    out = []
    for seg in s.split("\n"):
        cur = ""
        for ch in seg:
            if d.textlength(cur+ch, font=f) > maxw:
                out.append(cur); cur = ch
            else:
                cur += ch
        out.append(cur)
    return out

def para(s, f=F_YH, size=21, fill=BLACK, indent=0, maxw=None, lh=30):
    global y
    maxw = maxw or (CW - indent)
    for ln in wrap(s, f, maxw):
        d.text((MARGIN+indent, y), ln, font=f, fill=fill)
        y += lh
    y += 2

def heading(s):
    global y
    y += 10
    d.rectangle([(MARGIN, y), (MARGIN+8, y+30)], fill=RED)
    d.text((MARGIN+18, y), s, font=F_HEI_B, fill=BLACK)
    y += 40

def two_col(left, right, f=F_YH, lh=30):
    global y
    ly = y
    for ln in wrap(left, f, CW//2-30):
        d.text((MARGIN, ly), ln, font=f, fill=BLACK); ly += lh
    ry = y
    for ln in wrap(right, f, CW//2-30):
        d.text((MARGIN+CW//2+10, ry), ln, font=f, fill=BLACK); ry += lh
    y = max(ly, ry)

# ---------- 标题 ----------
text("房屋租赁合同", F_HEI, BLACK, center=True, dy=6)
text("（经营租赁 · 含管理费 · 混合计费）", F_YH_S, GREY, center=True, dy=10)
text("合同编号：HT-2026-0912-湘A-0047      签订地点：湖南省长沙市岳麓区", F_YH_S, GREY, center=True, dy=14)
d.line([(MARGIN, y), (W-MARGIN, y)], fill=BLACK, width=2); nl(14)

# ---------- 甲乙双方 ----------
heading("第一条  合同当事人")
two_col(
    "甲方（出租方）：卓越融资租赁有限公司\n统一社会信用代码：91430100MA4L8X2K9P\n法定代表人：张文涛\n开户银行：招商银行长沙分行岳麓支行\n银行账号：7319 0231 8845 6677 2009\n联系地址：湖南省长沙市岳麓区梅溪湖街道\n  景观路 188 号卓越金融中心 23 层",
    "乙方（承租方）：长沙星城智能制造股份有限公司\n统一社会信用代码：91430100MA5R1Q7B3T\n法定代表人：李慧\n开户银行：工商银行长沙麓谷支行\n银行账号：1901 0245 0920 0371 884\n联系地址：湖南省长沙市岳麓区东方红街道\n  麓谷大道 658 号星城智造园 B 区 5 栋")
nl(6)

heading("第二条  租赁物及期限")
para("2.1 租赁物坐落：湖南省长沙市岳麓区梅溪湖街道景观路 188 号卓越产业园 A 栋 5—8 层（含附属设备间）。")
para("2.2 建筑面积：12,345.67 平方米；其中可租赁面积 11,980.00 平方米。")
para("2.3 租赁期限：自 2025-01-01 起至 2029-12-31 止，共计 60 个月。免租期：无（自起租日起计租）。")
para("2.4 结算方式：付当季（按季度预付，每季度首月 10 日前支付当季费用）。")

heading("第三条  租金、管理费及递增约定")
para("3.1 月租金（不含税）：人民币捌万陆仟元整（¥86,000.00）；适用增值税税率 9%。")
para("3.2 月管理费（含物业、空调、公区能耗）：¥12,000.00，税率 6%。")
para("3.3 递增约定：自第 2 个租赁年度起，年租金在上一年度基础上上浮 3%（即 2026 年起月租金调整为 ¥88,580.00，此后逐年按 3% 复利递增）。管理费不递增。")
para("3.4 保证金：相当于 3 个月租金与管理费合计，即 ¥294,000.00，于合同签订后 5 个工作日内一次性支付，无息退还。")

# ---------- 付款计划表 ----------
heading("第四条  付款计划表（按季度）")
cols = ["期次","费用所属期间","应付日","租金(元)","管理费(元)","合计(元)","税率"]
rows = [
    ["1","2025年1至3月","2025-01-10","258000.00","36000.00","294000.00","9%/6%"],
    ["2","2025年4至6月","2025-04-10","258000.00","36000.00","294000.00","9%/6%"],
    ["3","2025年7至9月","2025-07-10","258000.00","36000.00","294000.00","9%/6%"],
    ["4","2025年10至12月","2025-10-10","258000.00","36000.00","294000.00","9%/6%"],
    ["5","2026年1至3月","2026-01-10","265740.00","36000.00","301740.00","9%/6%"],
    ["6","2026年4至6月","2026-04-10","265740.00","36000.00","301740.00","9%/6%"],
    ["7","2026年7至9月","2026-07-10","265740.00","36000.00","301740.00","9%/6%"],
    ["8","2026年10至12月","2026-10-10","265740.00","36000.00","301740.00","9%/6%"],
    ["…","…（逐年递增 3%）…","…","…","…","…","…"],
]
def draw_table(headers, data, colw, f=F_YH_S, rh=34, hfill=(235,238,242)):
    global y
    x0 = MARGIN
    # 表头
    d.rectangle([(x0, y), (x0+sum(colw), y+rh)], fill=hfill, outline=BLACK)
    cx = x0
    for i, h in enumerate(headers):
        d.text((cx+6, y+rh//2-11), h, font=F_HEI_B, fill=BLACK)
        cx += colw[i]
    y += rh
    for r in data:
        d.rectangle([(x0, y), (x0+sum(colw), y+rh)], outline=BLACK, fill=WHITE)
        cx = x0
        for i, cell in enumerate(r):
            d.text((cx+6, y+rh//2-10), str(cell), font=f, fill=BLACK)
            cx += colw[i]
        y += rh

cw = [70, 250, 160, 175, 175, 175, 90]
draw_table(cols, rows, cw)
nl(4)
# 全周期（20 期）含税合计：由租金/管理费勾稽算出，避免手写数字自相矛盾
# 租金逐年 3% 复利递增、管理费不递增；季度租金 = 当月租金 × 3
_mon, _grand = 86000.0, 0.0
for _yr in range(5):
    _grand += 4 * round(_mon, 2) * 3 + 4 * 36000.0
    _mon *= 1.03
GRAND = round(_grand, 2)          # 全周期含税合计
STAMP = round(GRAND * 0.001, 2)   # 印花税 = 合同金额 × 0.1%
text("注：上表仅为前 8 期示意；第 9 期起逐年按 3% 复利递增，全周期（20 期）含税合计约 " + f"{GRAND:,.2f}" + " 元。", F_SONG, GREY)
nl(12)

# ---------- 分摊比例表 ----------
heading("第五条  费用部门分摊比例")
cols2 = ["承担部门","分摊比例","对应成本中心","备注"]
rows2 = [
    ["生产制造部","60%","CC-PROD-01","按产量系数二次分配"],
    ["行政管理部","25%","CC-ADM-02","含前台与安保"],
    ["研发中心","15%","CC-RND-03","含实验室用电"],
]
draw_table(cols2, rows2, [230, 150, 250, 470], f=F_YH_S, rh=32)
nl(12)

# ---------- 一次性费用 ----------
heading("第六条  一次性费用及税费")
para("6.1 中介服务费：¥45,000.00（乙方承担，签约时支付）。", F_YH_S)
para("6.2 印花税：按租赁合同金额 0.1% 贴花，约 ¥" + f"{STAMP:,.2f}" + "，双方各半。", F_YH_S)
para("6.3 装修押金：¥50,000.00，退租验收后无息退还。", F_YH_S)
nl(6)

# ---------- 极小字脚注 ----------
heading("第七条  其他约定（以下为条款细则，请仔细阅读）")
notes = [
    "7.1 甲方承诺租赁物无产权瑕疵；如因抵押、查封致乙方无法使用，甲方应按已付费用的 20% 向乙方支付违约金，并赔偿装修残值损失。",
    "7.2 乙方不得擅自转租、分租或改变房屋用途；确需转租的，应提前 30 日书面征得甲方同意，并按转租收入的 10% 向甲方支付管理费。",
    "7.3 租赁期内如遇政府征收、征用，补偿款中属于乙方的装修及停产损失部分归乙方，土地及房屋补偿归甲方。",
    "7.4 本合同未尽事宜，双方可另行签署补充协议；补充协议与本合同具有同等法律效力。争议提交长沙仲裁委员会仲裁。",
    "7.5 本合同一式肆份，甲乙双方各执贰份，自双方法定代表人或授权代表签字并加盖公章之日起生效。电子扫描件与原件具有同等效力。",
    "7.6 本合同中「月租金」「管理费」「保证金」等金额均以人民币计价；汇率波动不影响本合同项下义务。任何通知以本合同载明地址为准。",
]
para(" ".join(notes[:3]), F_YH_T, 12, BLACK, lh=18)
para(" ".join(notes[3:]), F_YH_T, 12, BLACK, lh=18)
nl(16)

# ---------- 签章区 ----------
heading("第八条  签章")
sig_y = y
d.line([(MARGIN, y), (MARGIN+CW//2-20, y)], fill=BLACK, width=1)
d.line([(MARGIN+CW//2+30, y), (W-MARGIN, y)], fill=BLACK, width=1)
text("甲方（盖章）：卓越融资租赁有限公司", F_YH, BLACK, x=MARGIN, dy=18)
text("授权代表（签字）：____________", F_YH, BLACK, x=MARGIN, dy=22)
text("日期：2024 年 12 月 20 日", F_YH, BLACK, x=MARGIN, dy=10)
text("乙方（盖章）：长沙星城智能制造股份有限公司", F_YH, BLACK, x=MARGIN+CW//2+30, dy=18)
text("授权代表（签字）：____________", F_YH, BLACK, x=MARGIN+CW//2+30, dy=22)
text("日期：2024 年 12 月 20 日", F_YH, BLACK, x=MARGIN+CW//2+30, dy=14)

# 楷体手写批注（跨越一行，模拟财务复核）
d.text((MARGIN+CW//2+60, sig_y+2), "财务复核：金额无误—李", font=F_KAI, fill=(30,90,160))
nl(20)

# ---------- 红章（压在甲方签章处，半透明、略旋转） ----------
def make_seal(size=300):
    layer = Image.new("RGBA", (size, size), (0,0,0,0))
    ld = ImageDraw.Draw(layer)
    cx, cy = size//2, size//2
    ld.ellipse([10,10,size-10,size-10], outline=(200,30,30,170), width=10)
    ld.ellipse([26,26,size-26,size-26], outline=(200,30,30,150), width=3)
    # 五角星
    R = size*0.16
    pts = []
    for i in range(5):
        a = -math.pi/2 + i*2*math.pi/5
        pts.append((cx+R*math.cos(a), cy+R*math.sin(a)))
        a2 = a + math.pi/5
        pts.append((cx+R*0.4*math.cos(a2), cy+R*0.4*math.sin(a2)))
    ld.polygon(pts, fill=(200,30,30,172))
    # 环形文字
    name = "卓越融资租赁有限公司"
    fseal = font("simhei.ttf", 26)
    n = len(name)
    for i, ch in enumerate(name):
        ang = math.pi/2 - (i - (n-1)/2) * (2*math.pi/n) * 0.92
        bx, by = cx + (size*0.30)*math.cos(ang), cy - (size*0.30)*math.sin(ang)
        ch_img = Image.new("RGBA", (40,40), (0,0,0,0))
        ImageDraw.Draw(ch_img).text((0,0), ch, font=fseal, fill=(200,30,30,172))
        ch_img = ch_img.rotate(math.degrees(ang)+90, expand=True)
        layer.alpha_composite(ch_img, (int(bx-20), int(by-20)))
    return layer

seal = make_seal(300).rotate(-12, expand=True)
sx = MARGIN + 60
sy = sig_y - 40
img.paste(seal, (sx, sy), seal)

# 第二枚小章压在一条细则上（挑战压字）
seal2 = make_seal(180).rotate(8, expand=True)
img.paste(seal2, (MARGIN+720, 1180), seal2)

# ---------- 条码 + 伪二维码（非文本，考验是否误识别） ----------
bx0, by0 = MARGIN, H-150
random.seed(7)
for i in range(60):
    bw = random.choice([2,3,4,5])
    if random.random() > 0.45:
        d.rectangle([(bx0, by0),(bx0+bw, by0+70)], fill=BLACK)
    bx0 += bw + 1
d.text((MARGIN, by0+78), "合同条码 No. HT-2026-0912-湘A-0047", font=F_SONG, fill=GREY)
# 伪 QR
qx, qy, qn, qcell = W-MARGIN-220, H-330, 21, 10
d.rectangle([(qx-6,qy-6),(qx+qn*qcell+6,qy+qn*qcell+6)], outline=BLACK, width=2)
random.seed(21)
for r in range(qn):
    for c in range(qn):
        if random.random() > 0.5:
            d.rectangle([(qx+c*qcell, qy+r*qcell),(qx+(c+1)*qcell, qy+(r+1)*qcell)], fill=BLACK)

d.text((W-MARGIN-220, H-345), "扫码核验合同真伪", font=F_SONG, fill=GREY)

out = "D:/workbuddy/财务租赁/租赁线上化LightApp/ocr_test_contract.png"
img.save(out, "PNG", dpi=(DPI, DPI))
print("saved:", out, img.size)
