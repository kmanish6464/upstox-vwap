from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import KeepTogether
import datetime

OUTPUT = "/mnt/user-data/outputs/NIFTY_Market_Plan.pdf"

doc = SimpleDocTemplate(
    OUTPUT, pagesize=A4,
    leftMargin=18*mm, rightMargin=18*mm,
    topMargin=16*mm, bottomMargin=16*mm
)

W = A4[0] - 36*mm

# ── Colors ──────────────────────────────────────────────────────────────
C_BG       = colors.HexColor("#0D0D0D")
C_CARD     = colors.HexColor("#1A1A1A")
C_BULL     = colors.HexColor("#27AE60")
C_BEAR     = colors.HexColor("#E74C3C")
C_GOLD     = colors.HexColor("#F39C12")
C_BLUE     = colors.HexColor("#2980B9")
C_MUTED    = colors.HexColor("#95A5A6")
C_WHITE    = colors.white
C_BORDER   = colors.HexColor("#2C2C2C")
C_RESIST   = colors.HexColor("#3D1515")
C_SUPPORT  = colors.HexColor("#0F2E1A")
C_NEUTRAL  = colors.HexColor("#1A2540")
C_RESIST_T = colors.HexColor("#E74C3C")
C_SUPPORT_T= colors.HexColor("#27AE60")
C_NEUTRAL_T= colors.HexColor("#5DADE2")
C_HEAD_BG  = colors.HexColor("#111111")

# ── Styles ───────────────────────────────────────────────────────────────
SS = getSampleStyleSheet()

def sty(name, **kw):
    base = kw.pop("parent", "Normal")
    s = ParagraphStyle(name, parent=SS[base], **kw)
    return s

S_TITLE   = sty("title",   fontSize=22, textColor=C_WHITE,  alignment=TA_CENTER, leading=28, spaceAfter=2)
S_SUB     = sty("sub",     fontSize=10, textColor=C_MUTED,  alignment=TA_CENTER, leading=14, spaceAfter=4)
S_DATE    = sty("date",    fontSize=9,  textColor=C_GOLD,   alignment=TA_CENTER, leading=12)
S_SH      = sty("sh",      fontSize=11, textColor=C_MUTED,  leading=14, spaceBefore=10, spaceAfter=4,
                fontName="Helvetica-Bold", textTransform="uppercase")
S_BODY    = sty("body",    fontSize=9,  textColor=C_WHITE,  leading=14, spaceAfter=3)
S_BULL_B  = sty("bullb",   fontSize=9,  textColor=C_BULL,   leading=13, spaceAfter=2, fontName="Helvetica-Bold")
S_BEAR_B  = sty("bearb",   fontSize=9,  textColor=C_BEAR,   leading=13, spaceAfter=2, fontName="Helvetica-Bold")
S_GOLD_B  = sty("goldb",   fontSize=9,  textColor=C_GOLD,   leading=13, spaceAfter=2, fontName="Helvetica-Bold")
S_BLUE_B  = sty("blueb",   fontSize=9,  textColor=C_BLUE,   leading=13, spaceAfter=2, fontName="Helvetica-Bold")
S_TH      = sty("th",      fontSize=8,  textColor=C_MUTED,  leading=11, fontName="Helvetica-Bold")
S_TD      = sty("td",      fontSize=8,  textColor=C_WHITE,  leading=11)
S_TD_G    = sty("tdg",     fontSize=8,  textColor=C_BULL,   leading=11, fontName="Helvetica-Bold")
S_TD_R    = sty("tdr",     fontSize=8,  textColor=C_BEAR,   leading=11, fontName="Helvetica-Bold")
S_TD_O    = sty("tdo",     fontSize=8,  textColor=C_GOLD,   leading=11, fontName="Helvetica-Bold")
S_TD_B    = sty("tdb",     fontSize=8,  textColor=C_BLUE,   leading=11, fontName="Helvetica-Bold")
S_SMALL   = sty("small",   fontSize=7.5,textColor=C_MUTED,  leading=11)
S_DISC    = sty("disc",    fontSize=7,  textColor=C_MUTED,  alignment=TA_CENTER, leading=10)

def hr():
    return HRFlowable(width="100%", thickness=0.5, color=C_BORDER, spaceAfter=8, spaceBefore=4)

def section(txt):
    return Paragraph(txt, S_SH)

# ────────────────────────────────────────────────────────────────────────
story = []

# ── HEADER BLOCK ────────────────────────────────────────────────────────
today = datetime.date.today().strftime("%A, %d %B %Y")
hdr_data = [[
    Paragraph("NIFTY", sty("hn", fontSize=26, textColor=C_GOLD, fontName="Helvetica-Bold", alignment=TA_CENTER)),
    Paragraph("Market Movement Plan", sty("hm", fontSize=13, textColor=C_WHITE, alignment=TA_CENTER, leading=18)),
    Paragraph(today, sty("hd", fontSize=9, textColor=C_MUTED, alignment=TA_CENTER)),
]]
hdr = Table([hdr_data[0]], colWidths=[40*mm, 95*mm, 40*mm])
hdr.setStyle(TableStyle([
    ("BACKGROUND", (0,0),(-1,-1), C_HEAD_BG),
    ("ROUNDEDCORNERS", [6]),
    ("BOX", (0,0),(-1,-1), 0.5, C_BORDER),
    ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
    ("TOPPADDING",(0,0),(-1,-1),10),
    ("BOTTOMPADDING",(0,0),(-1,-1),10),
]))
story.append(hdr)
story.append(Spacer(1, 8))

# ── METRIC CARDS ────────────────────────────────────────────────────────
metrics = [
    ("LTP", "24,123", C_WHITE),
    ("Max Pain", "24,100", C_GOLD),
    ("PCR (OI)", "0.62", C_BEAR),
    ("PCR (Vol)", "1.07", C_BULL),
    ("ATM Straddle", "~213 pts", C_BLUE),
    ("Bias", "Sideways/Bearish", C_BEAR),
]
def metric_cell(label, value, vc):
    return [
        Paragraph(label, sty(f"ml{label}", fontSize=7, textColor=C_MUTED, fontName="Helvetica-Bold",
                              leading=10, alignment=TA_CENTER)),
        Paragraph(value, sty(f"mv{label}", fontSize=12, textColor=vc, fontName="Helvetica-Bold",
                              leading=15, alignment=TA_CENTER)),
    ]

mc_rows = [[metric_cell(m[0],m[1],m[2]) for m in metrics]]
cw = W / 6
mt = Table([[Table([mc], colWidths=[cw-4]) for mc in mc_rows[0]]], colWidths=[cw]*6)
for i, (_, _, vc) in enumerate(metrics):
    mt.setStyle(TableStyle([
        ("BACKGROUND",(i,0),(i,0), C_CARD),
        ("BOX",(i,0),(i,0), 0.5, C_BORDER),
        ("TOPPADDING",(i,0),(i,0),7),
        ("BOTTOMPADDING",(i,0),(i,0),7),
        ("LEFTPADDING",(i,0),(i,0),3),
        ("RIGHTPADDING",(i,0),(i,0),3),
    ]))
story.append(mt)
story.append(Spacer(1, 10))

# ── SIGNALS ─────────────────────────────────────────────────────────────
story.append(section("Signal Summary"))
signals = [
    (C_BEAR, "BEARISH", "PCR OI 0.62 — heavy call writing signals strong overhead resistance. Market makers capping upside."),
    (C_BULL, "BULLISH", "PCR Volume 1.07 — intraday traders buying puts for protection, indicating floor support exists."),
    (C_GOLD, "ANCHOR",  "Max Pain at 24,100 acts as gravitational center. Price likely to oscillate around this level."),
    (C_BEAR, "BEARISH", "24,200 strike holds highest cumulative OI (18M) + active Short Build in calls — strongest resistance."),
    (C_BULL, "BULLISH", "24,000 strike: 14M OI + Long Build in puts — key intraday support zone."),
    (C_MUTED,"NEUTRAL", "Delta Straddle ~213 pts implies expected daily range: 23,887 – 24,313."),
]
sig_rows = []
for col, badge, txt in signals:
    sig_rows.append([
        Paragraph(badge, sty(f"sb{badge}", fontSize=7, textColor=col, fontName="Helvetica-Bold",
                              alignment=TA_CENTER, leading=9)),
        Paragraph(txt, sty(f"st{badge}", fontSize=8, textColor=C_WHITE, leading=11)),
    ])
st = Table(sig_rows, colWidths=[22*mm, W-22*mm])
st.setStyle(TableStyle([
    ("BACKGROUND",(0,0),(-1,-1), C_CARD),
    ("BOX",(0,0),(-1,-1),0.5, C_BORDER),
    ("INNERGRID",(0,0),(-1,-1),0.3, C_BORDER),
    ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
    ("TOPPADDING",(0,0),(-1,-1),4),
    ("BOTTOMPADDING",(0,0),(-1,-1),4),
    ("LEFTPADDING",(0,0),(-1,-1),6),
    ("RIGHTPADDING",(0,0),(-1,-1),6),
]))
story.append(st)
story.append(Spacer(1, 10))

# ── EXPECTED RANGE VISUAL ───────────────────────────────────────────────
story.append(section("Expected Range Map"))

range_data = [
    [Paragraph("Level", S_TH), Paragraph("Zone", S_TH), Paragraph("OI (Cum.)", S_TH),
     Paragraph("OI Action", S_TH), Paragraph("Role", S_TH)],
    [Paragraph("24,500", S_TD_R), Paragraph("Heavy Supply", sty("z1",fontSize=8,textColor=C_BEAR,leading=11)),
     Paragraph("16M", S_TD), Paragraph("Short Build Calls", S_TD_R), Paragraph("Resistance 3", S_TD_R)],
    [Paragraph("24,300", S_TD_R), Paragraph("Resistance Zone", sty("z2",fontSize=8,textColor=C_BEAR,leading=11)),
     Paragraph("17M", S_TD), Paragraph("Short Build Calls", S_TD_R), Paragraph("Resistance 2", S_TD_R)],
    [Paragraph("24,200 ★", S_TD_R), Paragraph("Key Resistance", sty("z3",fontSize=8,textColor=C_BEAR,leading=11)),
     Paragraph("18M — Highest", S_TD_R), Paragraph("Short Build Calls", S_TD_R), Paragraph("Resistance 1", S_TD_R)],
    [Paragraph("24,100", S_TD_O), Paragraph("Max Pain / ATM", sty("z4",fontSize=8,textColor=C_GOLD,leading=11)),
     Paragraph("13M", S_TD), Paragraph("Neutral — Anchor", S_TD_O), Paragraph("Max Pain", S_TD_O)],
    [Paragraph("24,123", sty("ltpz",fontSize=8,textColor=C_GOLD,fontName="Helvetica-Bold",leading=11)),
     Paragraph("LTP (Current)", sty("z5",fontSize=8,textColor=C_GOLD,leading=11)),
     Paragraph("—", S_TD), Paragraph("—", S_TD), Paragraph("Live Price", S_TD_O)],
    [Paragraph("24,000 ★", S_TD_G), Paragraph("Key Support", sty("z6",fontSize=8,textColor=C_BULL,leading=11)),
     Paragraph("14M", S_TD), Paragraph("Long Build Puts", S_TD_G), Paragraph("Support 1", S_TD_G)],
    [Paragraph("23,900", S_TD_G), Paragraph("Support Zone", sty("z7",fontSize=8,textColor=C_BULL,leading=11)),
     Paragraph("7M", S_TD), Paragraph("Long Build Puts", S_TD_G), Paragraph("Support 2", S_TD_G)],
    [Paragraph("23,850", S_TD_G), Paragraph("Deep Support", sty("z8",fontSize=8,textColor=C_BULL,leading=11)),
     Paragraph("Low", S_TD), Paragraph("—", S_TD), Paragraph("Support 3", S_TD_G)],
]
rng = Table(range_data, colWidths=[25*mm, 38*mm, 32*mm, 40*mm, 30*mm])
rng.setStyle(TableStyle([
    ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#222222")),
    ("BACKGROUND",(0,1),(-1,3), C_RESIST),
    ("BACKGROUND",(0,4),(-1,4), C_NEUTRAL),
    ("BACKGROUND",(0,5),(-1,5), colors.HexColor("#2A2000")),
    ("BACKGROUND",(0,6),(-1,7), C_SUPPORT),
    ("BACKGROUND",(0,8),(-1,8), colors.HexColor("#0A1F10")),
    ("BOX",(0,0),(-1,-1), 0.5, C_BORDER),
    ("INNERGRID",(0,0),(-1,-1), 0.3, C_BORDER),
    ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
    ("TOPPADDING",(0,0),(-1,-1),4),
    ("BOTTOMPADDING",(0,0),(-1,-1),4),
    ("LEFTPADDING",(0,0),(-1,-1),6),
    ("RIGHTPADDING",(0,0),(-1,-1),6),
]))
story.append(rng)
story.append(Spacer(1, 10))

# ── SCENARIOS ───────────────────────────────────────────────────────────
story.append(section("Scenarios for Tomorrow"))

scen = [
    [
        Paragraph("BULL CASE — 30%", sty("sc1h",fontSize=9,textColor=C_BULL,fontName="Helvetica-Bold",leading=12)),
        Paragraph("BASE CASE — 50%", sty("sc2h",fontSize=9,textColor=C_BLUE,fontName="Helvetica-Bold",leading=12,alignment=TA_CENTER)),
        Paragraph("BEAR CASE — 20%", sty("sc3h",fontSize=9,textColor=C_BEAR,fontName="Helvetica-Bold",leading=12,alignment=TA_RIGHT)),
    ],
    [
        Paragraph("Target: 24,200 – 24,280\n\nTrigger: Positive global cues, NIFTY holds above 24,150 intraday.\n\nNote: 24,300 is stiff resistance (17M OI). Unlikely breach without major catalyst.", sty("sc1b",fontSize=8,textColor=C_WHITE,leading=12)),
        Paragraph("Range: 24,000 – 24,200\n\nMax pain gravity pulls price toward 24,100. Chop in morning, possible fade in second half. Most likely outcome given current OI structure.", sty("sc2b",fontSize=8,textColor=C_WHITE,leading=12,alignment=TA_CENTER)),
        Paragraph("Target: 23,900 – 23,850\n\nTrigger: Break below 24,000 support + FII selling.\n\nNote: Momentum accelerates sharply if 24,000 CE writers start delta-hedging.", sty("sc3b",fontSize=8,textColor=C_WHITE,leading=12,alignment=TA_RIGHT)),
    ],
]

cw3 = W / 3
sc_t = Table(scen, colWidths=[cw3, cw3, cw3])
sc_t.setStyle(TableStyle([
    ("BACKGROUND",(0,0),(-1,-1), C_CARD),
    ("BACKGROUND",(0,0),(0,0), colors.HexColor("#0F2E1A")),
    ("BACKGROUND",(1,0),(1,0), C_NEUTRAL),
    ("BACKGROUND",(2,0),(2,0), C_RESIST),
    ("BOX",(0,0),(-1,-1),0.5, C_BORDER),
    ("INNERGRID",(0,0),(-1,-1),0.3, C_BORDER),
    ("VALIGN",(0,0),(-1,-1),"TOP"),
    ("TOPPADDING",(0,0),(-1,-1),7),
    ("BOTTOMPADDING",(0,0),(-1,-1),7),
    ("LEFTPADDING",(0,0),(-1,-1),8),
    ("RIGHTPADDING",(0,0),(-1,-1),8),
]))
story.append(sc_t)
story.append(Spacer(1, 10))

# ── ACTION PLAN TABLE ────────────────────────────────────────────────────
story.append(section("Key Levels & Action Plan"))

act_hdr = ["Level", "Type", "OI Context", "Action / Strategy", "SL"]
act_rows = [
    ["24,300+",  "Resistance 2", "17M, Short Build",            "Sell CE on pullback / Buy PE after rejection",          "24,350 close"],
    ["24,200 ★", "Resistance 1", "18M — Chain high, S.Build",   "Fade rallies. Sell 24,200 CE or buy 24,200 PE",         "24,250 close"],
    ["24,100",   "Max Pain/ATM", "13M, neutral anchor",         "Dead zone — avoid directional between 24,050–24,150",   "—"],
    ["24,000 ★", "Support 1",    "14M, Long Build puts",        "Scalp long. Buy 24,000 CE on holding candle close",     "23,960 close"],
    ["23,900",   "Support 2",    "7M, Long Build",              "Last defence. Below = bearish bias for rest of week",   "23,850 close"],
]
color_map = [C_BEAR, C_BEAR, C_GOLD, C_BULL, C_BULL]
act_data = [[Paragraph(h, S_TH) for h in act_hdr]]
for i, row in enumerate(act_rows):
    c = color_map[i]
    act_data.append([
        Paragraph(row[0], sty(f"al{i}", fontSize=8, textColor=c, fontName="Helvetica-Bold", leading=11)),
        Paragraph(row[1], sty(f"at{i}", fontSize=8, textColor=c, leading=11)),
        Paragraph(row[2], S_TD),
        Paragraph(row[3], S_TD),
        Paragraph(row[4], S_SMALL),
    ])
at = Table(act_data, colWidths=[22*mm, 28*mm, 42*mm, 60*mm, 23*mm])
at.setStyle(TableStyle([
    ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#222222")),
    ("BACKGROUND",(0,1),(-1,5), C_CARD),
    ("BOX",(0,0),(-1,-1),0.5,C_BORDER),
    ("INNERGRID",(0,0),(-1,-1),0.3,C_BORDER),
    ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
    ("TOPPADDING",(0,0),(-1,-1),4),
    ("BOTTOMPADDING",(0,0),(-1,-1),4),
    ("LEFTPADDING",(0,0),(-1,-1),6),
    ("RIGHTPADDING",(0,0),(-1,-1),6),
]))
story.append(at)
story.append(Spacer(1, 10))

# ── PREFERRED SETUPS ────────────────────────────────────────────────────
story.append(section("Preferred Trading Setups"))

setup_hdr = ["Setup", "Type", "Entry Zone", "Target", "Stop Loss"]
setups = [
    ["Sell 24,200 CE (weekly)", "Intraday Sell", "24,180 – 24,200 touch", "24,080 – 24,050", "24,250 close"],
    ["Buy 24,000 CE (weekly)", "Bounce Buy",    "24,000 – 24,020 hold",  "24,100 – 24,150", "23,960 close"],
    ["Short Strangle",         "Premium Sell",  "Sell 24,300CE + 23,900PE", "50% premium decay", "2× premium either leg"],
]
s_colors = [C_BEAR, C_BULL, C_BLUE]
s_data = [[Paragraph(h, S_TH) for h in setup_hdr]]
for i, row in enumerate(setups):
    c = s_colors[i]
    s_data.append([
        Paragraph(row[0], sty(f"ss{i}", fontSize=8, textColor=c, fontName="Helvetica-Bold", leading=11)),
        Paragraph(row[1], sty(f"st{i}", fontSize=8, textColor=c, leading=11)),
        Paragraph(row[2], S_TD),
        Paragraph(row[3], S_TD_G if i==1 else (S_TD_R if i==0 else S_TD_B)),
        Paragraph(row[4], S_TD_R),
    ])
sd = Table(s_data, colWidths=[38*mm, 25*mm, 40*mm, 40*mm, 32*mm])
sd.setStyle(TableStyle([
    ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#222222")),
    ("BACKGROUND",(0,1),(-1,3), C_CARD),
    ("BOX",(0,0),(-1,-1),0.5,C_BORDER),
    ("INNERGRID",(0,0),(-1,-1),0.3,C_BORDER),
    ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
    ("TOPPADDING",(0,0),(-1,-1),5),
    ("BOTTOMPADDING",(0,0),(-1,-1),5),
    ("LEFTPADDING",(0,0),(-1,-1),6),
    ("RIGHTPADDING",(0,0),(-1,-1),6),
]))
story.append(sd)
story.append(Spacer(1, 10))

# ── OPEN STRATEGY ────────────────────────────────────────────────────────
story.append(section("Opening Strategy Framework"))
open_data = [
    [Paragraph("Open Scenario", S_TH), Paragraph("Condition", S_TH), Paragraph("Preferred Action", S_TH)],
    [Paragraph("Gap Up / Above 24,150", sty("op1", fontSize=8, textColor=C_BEAR, fontName="Helvetica-Bold", leading=11)),
     Paragraph("Price moves above 24,150 at open", S_TD),
     Paragraph("Watch for fake breakout to 24,200. Fade with tight SL above 24,250.", S_TD_R)],
    [Paragraph("Flat Open 24,050–24,150", sty("op2", fontSize=8, textColor=C_GOLD, fontName="Helvetica-Bold", leading=11)),
     Paragraph("Price opens in Max Pain dead zone", S_TD),
     Paragraph("Wait for 15-min candle close to confirm direction before entering.", S_TD_O)],
    [Paragraph("Gap Down / Below 24,000", sty("op3", fontSize=8, textColor=C_BULL, fontName="Helvetica-Bold", leading=11)),
     Paragraph("Price opens below 24,000 support", S_TD),
     Paragraph("Scalp long opportunity. Buy CE with SL at 23,960 close. Target 24,080.", S_TD_G)],
]
op = Table(open_data, colWidths=[42*mm, 55*mm, 78*mm])
op.setStyle(TableStyle([
    ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#222222")),
    ("BACKGROUND",(0,1),(-1,1), C_RESIST),
    ("BACKGROUND",(0,2),(-1,2), colors.HexColor("#1A1F10")),
    ("BACKGROUND",(0,3),(-1,3), C_SUPPORT),
    ("BOX",(0,0),(-1,-1),0.5,C_BORDER),
    ("INNERGRID",(0,0),(-1,-1),0.3,C_BORDER),
    ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
    ("TOPPADDING",(0,0),(-1,-1),5),
    ("BOTTOMPADDING",(0,0),(-1,-1),5),
    ("LEFTPADDING",(0,0),(-1,-1),6),
    ("RIGHTPADDING",(0,0),(-1,-1),6),
]))
story.append(op)
story.append(Spacer(1, 10))

# ── FOOTER ───────────────────────────────────────────────────────────────
story.append(hr())
story.append(Paragraph(
    "Analysis based on NIFTY Options Chain data from GoCharting. "
    "PCR OI: 0.62 | PCR Vol: 1.07 | Max Pain: 24,100 | Cum OI: 184M | Generated: " + today,
    S_DISC
))
story.append(Spacer(1, 2))
story.append(Paragraph(
    "This plan is for educational/analytical purposes only and does not constitute investment advice. "
    "Always manage risk with proper position sizing and stop-losses.",
    S_DISC
))

# ── BUILD ────────────────────────────────────────────────────────────────
def on_page(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(C_BG)
    canvas.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)
    canvas.restoreState()

doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
print("Done:", OUTPUT)