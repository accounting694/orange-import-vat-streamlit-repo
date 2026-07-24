import io
import math
import re
from datetime import date

import pandas as pd
import streamlit as st

APP_TITLE = "Orange Import to PEAK"
VAT_RATE = 0.07

st.set_page_config(page_title=APP_TITLE, page_icon="🗂️", layout="wide")

PEAK_COLUMNS = [
    "ลำดับที่*",
    "วันที่เอกสาร",
    "อ้างอิงถึง",
    "ผู้รับเงิน/คู่ค้า",
    "เลขทะเบียน 13 หลัก",
    "เลขสาขา 5 หลัก",
    "เลขที่ใบกำกับฯ (ถ้ามี)",
    "วันที่ใบกำกับฯ (ถ้ามี)",
    "วันที่บันทึกภาษีซื้อ (ถ้ามี)",
    "ประเภทราคา",
    "สินค้า/บริการ",
    "บัญชี",
    "คำอธิบาย",
    "จำนวน",
    "ราคาต่อหน่วย",
    "อัตราภาษี",
    "หัก ณ ที่จ่าย (ถ้ามี)",
    "ชำระโดย",
    "จำนวนเงินที่ชำระ",
    "ภ.ง.ด. (ถ้ามี)",
    "หมายเหตุ",
    "กลุ่มจัดประเภท",
]

DEFAULT_PRODUCT_NAMES = {
    "BG": "ตู้ไม้",
    "DG": "ตู้วางข้างเตียง",
    "CTG": "ตู้วางข้างเตียง",
    "DSG": "ตู้วางทีวี",
    "SZT": "โต๊ะเครื่องแป้ง",
    "RMG": "รางผ้าม่าน",
    "KR": "ชั้นวางของ",
}

DEFAULT_PRODUCT_PRICES = {
    "BG": 257.33,
    "DG": 255.81,
    "CTG": 255.81,
    "DSG": 329.69,
    "SZT": 256.62,
    "RMG": 101.43,
    "KR": 204.69,
}

# แม็ปชื่อสินค้าภาษาอังกฤษที่อ่านได้จากใบขน (customs) ไปเป็นชื่อหมวดหมู่ภาษาไทยที่ระบบใช้อยู่แล้ว
CUSTOMS_NAME_TO_TH = {
    "DRESSING TABLE": "โต๊ะเครื่องแป้ง",
    "WOODEN CUPBOARD": "ตู้ไม้",
    "TV CABINET": "ตู้วางทีวี",
    "BEDSIDE CABINET": "ตู้วางข้างเตียง",
    "SHELF": "ชั้นวางของ",
    "CURTAIN RAIL": "รางผ้าม่าน",
}


def clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_yyyymmdd(value: date | str) -> str:
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    return re.sub(r"\D", "", str(value or ""))


def product_family(product_code: str) -> str:
    match = re.match(r"([A-Z]+)", str(product_code).strip().upper())
    return match.group(1) if match else ""


def product_name(product_code: str) -> str:
    family = product_family(product_code)
    for prefix, name in DEFAULT_PRODUCT_NAMES.items():
        if family.startswith(prefix):
            return name
    return "สินค้า"


def default_price(product_code: str) -> float:
    family = product_family(product_code)
    for prefix, price in DEFAULT_PRODUCT_PRICES.items():
        if family.startswith(prefix):
            return price
    return 0.0


def _parse_customs_number(raw: str):
    """แปลงข้อความตัวเลขจาก OCR ที่อาจสลับ , กับ . ให้เป็น float โดยยึดตัวคั่นตัวสุดท้ายเป็นจุดทศนิยม"""
    raw = raw.strip().rstrip(".,")
    only = re.sub(r"[^,.\d]", "", raw)
    if not only:
        return None
    parts = re.split(r"[,.]", only)
    if len(parts) < 2:
        try:
            return float(parts[0])
        except (ValueError, IndexError):
            return None
    dec = parts[-1]
    intp = "".join(parts[:-1])
    try:
        return float(f"{intp}.{dec}")
    except ValueError:
        return None


def parse_customs_items(text: str) -> list[dict]:
    """แยกแต่ละรายการสินค้าที่นำเข้าจากข้อความใบขน (OCR) ออกเป็น
    ชื่อสินค้า (อังกฤษ), จำนวน (C62), มูลค่า (ฐานภาษีมูลค่าเพิ่ม), และ VAT ของรายการนั้นๆ
    """
    lines = text.split("\n")
    items = []

    for i, line in enumerate(lines):
        # แถวแรกของแต่ละรายการ มีรูปแบบ "USD <ราคาต่างประเทศ> 0.00 0.00 0.00 <มูลค่าบาท>"
        # ตัดแถวย่อยที่เป็นหมายเหตุแปลงสกุลเงิน (มี F= / I= / THB) ออก เพราะไม่ใช่แถวหลักของรายการ
        if "USD" not in line or "F=" in line or "I=" in line or "THB" in line:
            continue

        nums_raw = re.findall(r"[\d][\d,.]*\d", line)
        if len(nums_raw) < 3:
            continue

        value = _parse_customs_number(nums_raw[-1])
        if not value or value < 1000:
            continue

        vat = None
        qty = None
        name = None
        window_end = min(i + 14, len(lines))

        for j in range(i, window_end):
            l2 = lines[j]

            # หายอด VAT: อยู่ในแถวที่มีค่ามูลค่าเดิมปรากฏซ้ำอีกครั้ง (คอลัมน์ท้ายแถวนั้นคือ VAT)
            if vat is None and j != i:
                n2 = re.findall(r"[\d][\d,.]*\d", l2)
                parsed2 = [p for p in (_parse_customs_number(x) for x in n2) if p is not None]
                if len(parsed2) >= 2 and any(abs(p - value) < 0.5 for p in parsed2):
                    vat = parsed2[-1]

            # หาจำนวนสินค้า (หน่วยนับ C62)
            if qty is None:
                m = re.search(r"([\d][\d,.]*\d)\s*C62", l2)
                if m:
                    q = _parse_customs_number(m.group(1))
                    if q and q < 5000:
                        qty = q

            # หาชื่อสินค้าภาษาอังกฤษ (บรรทัดตัวพิมพ์ใหญ่ล้วน หรือท้ายบรรทัดที่ปนกับหมายเหตุอื่น)
            if name is None:
                l3 = l2.strip()
                if re.fullmatch(r"[A-Z][A-Z\s]{2,30}", l3) and l3 not in ("NO BRAND", "CN", "ORIGIN CRITERIA"):
                    name = l3
                else:
                    tail = re.search(r"([A-Z]{3,}(?:\s[A-Z]{2,})*)\s*$", l3)
                    if tail and tail.group(1) not in ("NO BRAND", "CN") and len(tail.group(1)) > 4:
                        name = tail.group(1)

            if vat is not None and qty is not None and name is not None:
                break

        if name:
            # ตัดตัวอักษรเดี่ยวหลุดหน้าชื่อ ที่มักเป็น noise จาก OCR (เช่น "E TV CABINET" -> "TV CABINET")
            name = re.sub(r"^[A-Z]\s+(?=[A-Z]{2,})", "", name).strip()

        if vat and qty and name:
            items.append({"name_en": name, "quantity": qty, "value": value, "vat": vat})

    return items


def extract_vat_from_header(text: str) -> float:
    """หายอด VAT จากตารางสรุปด้านบนของใบขน (อากรขาเข้า/ภาษีสรรพสามิต/.../ภาษีมูลค่าเพิ่ม/รวมทั้งสิ้น)
    โดยหาก่อนถึงรายการสินค้ารายการแรก เพราะแถวอื่นในตารางนี้เป็น 0.00 หมด
    เหลือแค่แถว "ภาษีมูลค่าเพิ่ม" กับ "รวมทั้งสิ้น" ที่มีค่าเท่ากัน (ไม่มีอากรขาเข้า/สรรพสามิตอื่น)
    วิธีนี้ไม่พึ่งพาการอ่านตัวอักษรไทยจาก OCR เลย (ซึ่งมักอ่านผิดเพี้ยนกับ PDF สแกน)
    """
    # ตัดให้เหลือแค่ส่วนหัวก่อนถึงรายการสินค้ารายการแรก (บรรทัดที่มี USD ตามด้วยตัวเลข 3 ค่าขึ้นไป)
    lines = text.split("\n")
    header_end = len(lines)
    for i, line in enumerate(lines):
        if "USD" in line and "F=" not in line and "I=" not in line and "THB" not in line:
            nums = re.findall(r"[\d][\d,.]*\d", line)
            if len(nums) >= 3:
                header_end = i
                break

    header_text = "\n".join(lines[:header_end])
    candidates = re.findall(r"([\d]{1,3}(?:,\d{3})*\.\d{2})\s*[\(\[]?\s*0[.,]00\s*[\)\]]?", header_text)
    values = [v for v in (_parse_customs_number(c) for c in candidates) if v and v > 0]
    if values:
        return max(values)
    return 0.0


def compute_price_tier(quantity: float, value: float):
    """แบ่งราคาต่อหน่วยเป็น 2 ระดับ (ต่ำ/สูง ห่างกัน 1 สตางค์) แล้วกระจายจำนวนให้ผลรวมมูลค่าตรงเป๊ะ
    เทคนิคเดียวกับเครื่องมือ 'เฉลี่ยราคาต่อชิ้น' ที่ใช้งานจริงอยู่แล้ว
    """
    if quantity <= 0:
        return 0.0, 0.0, 0, 0
    avg = value / quantity
    low = math.floor(avg * 100) / 100
    low = round(low, 2)
    high = round(low + 0.01, 2)
    qty_high = round((value - low * quantity) / 0.01)
    qty_high = max(0, min(quantity, qty_high))
    qty_low = quantity - qty_high
    return low, high, qty_low, qty_high


def allocate_tiered_rows(rows: list[dict], quantity: float, value: float, qty_key: str = "จำนวน", price_key: str = "ราคาต่อหน่วย") -> list[dict]:
    """กระจายราคาต่อหน่วยแบบ 2 ระดับให้แถวสินค้าแต่ละแถว (อาจตัดแบ่ง 1 แถวเป็น 2 แถวถ้าคาบเกี่ยวรอยต่อของราคา)"""
    low, high, qty_low, qty_high = compute_price_tier(quantity, value)
    cum = 0.0
    out_rows = []
    for row in rows:
        row_qty = float(row[qty_key])
        low_portion = max(0.0, min(row_qty, qty_low - cum))
        high_portion = row_qty - low_portion
        cum += row_qty
        if low_portion > 0:
            r = dict(row)
            r[qty_key] = round(low_portion, 6)
            r[price_key] = low
            out_rows.append(r)
        if high_portion > 0:
            r = dict(row)
            r[qty_key] = round(high_portion, 6)
            r[price_key] = high
            out_rows.append(r)
    return out_rows


def extract_pdf_text(uploaded_file) -> tuple[str, str]:
    data = uploaded_file.getvalue()
    messages = []
    text_parts = []

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        for page in reader.pages:
            text_parts.append(page.extract_text() or "")
        messages.append("อ่าน text layer")
    except Exception as exc:
        messages.append(f"pypdf อ่านไม่ได้: {exc}")

    text = clean_text("\n".join(text_parts))
    if len(text) >= 40:
        return text, " / ".join(messages)

    try:
        import pypdfium2 as pdfium
        import pytesseract

        pdf = pdfium.PdfDocument(data)
        ocr_parts = []
        for index in range(len(pdf)):
            page = pdf[index]
            image = page.render(scale=3).to_pil()
            if image.mode != "RGB":
                image = image.convert("RGB")
            try:
                page_text = pytesseract.image_to_string(image, lang="tha+eng")
            except Exception:
                page_text = pytesseract.image_to_string(image, lang="eng")
            ocr_parts.append(page_text)
        messages.append("OCR ด้วย Tesseract")
        return clean_text("\n".join(ocr_parts)), " / ".join(messages)
    except Exception as exc:
        messages.append(f"OCR อ่านไม่ได้: {exc}")
        return text, " / ".join(messages)


def detect_receipt_meta(filename: str, text: str) -> dict:
    receipt_no = ""
    container = ""
    supplier = ""
    receipt_date = ""

    receipt_match = re.search(r"\b(13\d{2})\b", text)
    if receipt_match:
        receipt_no = receipt_match.group(1)
    else:
        file_match = re.search(r"\b(13\d{2})\b", filename)
        receipt_no = file_match.group(1) if file_match else ""

    date_match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if date_match:
        receipt_date = f"{date_match.group(1)}{int(date_match.group(2)):02d}{int(date_match.group(3)):02d}"

    container_match = re.search(r"\b([A-Z]{4}\d{7})\b", text)
    if container_match:
        container = container_match.group(1)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if receipt_no and line == receipt_no and index + 1 < len(lines):
            supplier = lines[index + 1]
            break

    return {
        "receipt_no": receipt_no,
        "receipt_date": receipt_date,
        "container": container,
        "supplier": supplier,
    }


def normalize_quantity(raw_qty: str) -> int:
    qty = int(re.sub(r"\D", "", raw_qty))
    # Some receipt PDFs encode curtain rail quantities as pack length totals,
    # e.g. 15600 for 260 pieces and 12000 for 200 pieces.
    if qty >= 10000 and qty % 60 == 0:
        qty = qty // 60
    return qty


def parse_receipt_items(filename: str, text: str) -> list[dict]:
    meta = detect_receipt_meta(filename, text)
    items = []
    pattern = re.compile(r"(?m)^\s*(\d{1,3})\s*\n\s*([A-Z0-9]+(?:-[A-Z0-9]+)*)\s+(\d{1,6})\s*$")

    for match in pattern.finditer(text):
        product_code = match.group(2).strip().upper()
        qty = normalize_quantity(match.group(3))
        items.append(
            {
                "ไฟล์ต้นทาง": filename,
                "ใบรับสินค้า": meta["receipt_no"],
                "วันที่ใบรับสินค้า": meta["receipt_date"],
                "ตู้": meta["container"],
                "ลำดับในใบ": int(match.group(1)),
                "สินค้า/บริการ": product_code,
                "จำนวน": qty,
                "ราคาต่อหน่วย": default_price(product_code),
                "ชื่อสินค้า": product_name(product_code),
                "ข้อความหมายเหตุ": "",
            }
        )
    return items


def read_product_master(uploaded_template) -> dict:
    if not uploaded_template:
        return {}
    try:
        df = pd.read_excel(uploaded_template, dtype=object)
    except Exception:
        return {}

    required = {"สินค้า/บริการ", "บัญชี", "คำอธิบาย", "ราคาต่อหน่วย"}
    if not required.issubset(set(df.columns)):
        return {}

    master = {}
    for _, row in df.dropna(subset=["สินค้า/บริการ"]).iterrows():
        code = str(row["สินค้า/บริการ"]).strip().upper()
        if code not in master:
            master[code] = {
                "บัญชี": row.get("บัญชี", "114102"),
                "คำอธิบาย": row.get("คำอธิบาย", ""),
                "ราคาต่อหน่วย": float(row.get("ราคาต่อหน่วย") or 0),
                "กลุ่มจัดประเภท": row.get("กลุ่มจัดประเภท", "G004-00014"),
            }
    return master


def apply_product_master(items: list[dict], master: dict) -> list[dict]:
    enriched = []
    for item in items:
        row = item.copy()
        code = row["สินค้า/บริการ"]
        if code in master:
            row["ราคาต่อหน่วย"] = master[code]["ราคาต่อหน่วย"] or row["ราคาต่อหน่วย"]
            row["บัญชี"] = master[code]["บัญชี"] or "114102"
            row["กลุ่มจัดประเภท"] = master[code]["กลุ่มจัดประเภท"] or "G004-00014"
            desc = str(master[code]["คำอธิบาย"] or "")
            row["ชื่อสินค้า"] = desc.split("(")[0].strip() or row["ชื่อสินค้า"]
        else:
            row["บัญชี"] = "114102"
            row["กลุ่มจัดประเภท"] = "G004-00014"
        enriched.append(row)
    return enriched


def build_peak_rows(items_df: pd.DataFrame, settings: dict) -> pd.DataFrame:
    rows = []
    for _, item in items_df.iterrows():
        container = str(item.get("ตู้") or settings["container"]).strip()
        description = str(item.get("ชื่อสินค้า") or product_name(item["สินค้า/บริการ"])).strip()
        if container:
            description = f"{description} ({container})"

        rows.append(
            {
                "ลำดับที่*": 1,
                "วันที่เอกสาร": settings["document_date"],
                "อ้างอิงถึง": settings["reference_no"],
                "ผู้รับเงิน/คู่ค้า": settings["partner_code"],
                "เลขทะเบียน 13 หลัก": "",
                "เลขสาขา 5 หลัก": "",
                "เลขที่ใบกำกับฯ (ถ้ามี)": settings["tax_invoice_no"],
                "วันที่ใบกำกับฯ (ถ้ามี)": settings["tax_invoice_date"],
                "วันที่บันทึกภาษีซื้อ (ถ้ามี)": settings["vat_record_date"],
                "ประเภทราคา": 1,
                "สินค้า/บริการ": item["สินค้า/บริการ"],
                "บัญชี": item.get("บัญชี") or settings["account_code"],
                "คำอธิบาย": description,
                "จำนวน": item["จำนวน"],
                "ราคาต่อหน่วย": item["ราคาต่อหน่วย"],
                "อัตราภาษี": settings["vat_rate"],
                "หัก ณ ที่จ่าย (ถ้ามี)": "",
                "ชำระโดย": "",
                "จำนวนเงินที่ชำระ": "",
                "ภ.ง.ด. (ถ้ามี)": "",
                "หมายเหตุ": item.get("ข้อความหมายเหตุ", ""),
                "กลุ่มจัดประเภท": item.get("กลุ่มจัดประเภท") or settings["group_code"],
            }
        )
    return pd.DataFrame(rows, columns=PEAK_COLUMNS)


def to_excel_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            df.to_excel(writer, index=False, sheet_name=sheet_name)
        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = "A2"
            for column_cells in worksheet.columns:
                max_len = max(len(str(cell.value or "")) for cell in column_cells)
                worksheet.column_dimensions[column_cells[0].column_letter].width = min(max(max_len + 2, 10), 36)
    output.seek(0)
    return output.getvalue()

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Sarabun:wght@400;500;600;700&display=swap');

    :root {
        --ot-navy: #1B2A4A;
        --ot-navy-dark: #10192E;
        --ot-navy-tint: #EEF1F6;
        --ot-gold: #A9812E;
        --ot-gold-tint: #F7F1E2;
        --ot-ink: #202634;
        --ot-slate: #5B6273;
        --ot-paper: #FFFFFF;
        --ot-bg: #F4F5F7;
        --ot-line: #DCE0E8;
    }

    html, body, [class*="css"] {
        font-family: 'Sarabun', 'THSarabunPSK', sans-serif;
    }

    .stApp {
        background-color: var(--ot-bg);
    }

    h1, h2, h3, .ot-step-label {
        font-family: 'Sarabun', sans-serif !important;
        font-weight: 700 !important;
        color: var(--ot-ink) !important;
        letter-spacing: 0.2px;
    }

    /* แถบขั้นตอน */
    .ot-step {
        display: flex;
        align-items: center;
        gap: 0.7rem;
        margin: 1.7rem 0 0.5rem 0;
        padding-bottom: 0.5rem;
        border-bottom: 1px solid var(--ot-line);
    }
    .ot-step-num {
        flex-shrink: 0;
        width: 32px;
        height: 32px;
        border-radius: 50%;
        border: 1.5px solid var(--ot-navy);
        color: var(--ot-navy);
        background: var(--ot-paper);
        font-weight: 700;
        font-size: 15px;
        display: flex;
        align-items: center;
        justify-content: center;
    }
    .ot-step-label {
        font-size: 20px;
        font-weight: 700 !important;
    }
    .ot-step-sub {
        font-size: 13.5px;
        color: var(--ot-slate);
        margin-left: 42px;
        margin-top: -2px;
    }

    /* Sidebar */
    section[data-testid="stSidebar"] {
        background-color: var(--ot-navy-dark);
    }
    section[data-testid="stSidebar"] * {
        color: #E7E9EE !important;
    }
    section[data-testid="stSidebar"] input, section[data-testid="stSidebar"] [data-baseweb="input"] {
        color: var(--ot-ink) !important;
    }

    /* ปุ่ม */
    .stButton > button, .stDownloadButton > button {
        background: var(--ot-navy);
        color: white;
        border: none;
        border-radius: 6px;
        font-weight: 600;
        padding: 0.5rem 1.25rem;
        transition: background 0.15s ease;
    }
    .stButton > button:hover, .stDownloadButton > button:hover {
        background: var(--ot-navy-dark);
        color: white;
    }

    /* Metric */
    div[data-testid="stMetric"] {
        background: var(--ot-paper);
        border: 1px solid var(--ot-line);
        border-top: 3px solid var(--ot-gold);
        border-radius: 6px;
        padding: 0.9rem 1rem;
    }
    div[data-testid="stMetricLabel"] {
        color: var(--ot-slate);
    }
    div[data-testid="stMetricValue"] {
        font-family: 'Sarabun', sans-serif;
        font-weight: 700;
        color: var(--ot-navy);
    }

    /* File uploader */
    [data-testid="stFileUploaderDropzone"] {
        background: var(--ot-paper);
        border: 1.5px dashed #B7BECC;
        border-radius: 6px;
    }

    /* Expander / dataframe */
    div[data-testid="stExpander"], div[data-testid="stDataFrame"] {
        border-radius: 6px;
        overflow: hidden;
        border: 1px solid var(--ot-line);
    }

    hr {
        border-top: 1px solid var(--ot-line);
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div style="
        background: var(--ot-navy, #1B2A4A);
        padding: 1.5rem 1.8rem;
        border-radius: 8px;
        border-bottom: 4px solid #A9812E;
        margin-bottom: 1.3rem;
        color: white;
    ">
        <div style="font-family:'Sarabun',sans-serif;font-size:26px;font-weight:700;letter-spacing:0.3px;">
            Orange Import VAT
        </div>
        <div style="font-size:14.5px;opacity:0.85;margin-top:4px;letter-spacing:0.2px;">
            ระบบตรวจสอบ VAT นำเข้า &nbsp;·&nbsp; เฉลี่ยราคาต่อชิ้นให้ตรงใบขน &nbsp;·&nbsp; นำเข้าข้อมูล PEAK
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)


def step_header(number: str, title: str, subtitle: str = "") -> None:
    """หัวข้อขั้นตอนแบบมีเลขกำกับ ให้เห็นลำดับงานชัดเจน"""
    sub_html = f'<div class="ot-step-sub">{subtitle}</div>' if subtitle else ""
    st.markdown(
        f"""
        <div class="ot-step">
            <div class="ot-step-num">{number}</div>
            <div class="ot-step-label">{title}</div>
        </div>
        {sub_html}
        """,
        unsafe_allow_html=True,
    )


with st.sidebar:
    st.header("⚙️ ข้อมูลเอกสาร PEAK")
    document_date = st.date_input("วันที่เอกสาร", value=date(2026, 5, 28))
    reference_no = st.text_input("อ้างอิงถึง", value="YLZ-8020")
    partner_code = st.text_input("ผู้รับเงิน/คู่ค้า", value="C00129")
    tax_invoice_no = st.text_input("เลขที่ใบกำกับฯ", value="0210-010019")
    tax_invoice_date = st.date_input("วันที่ใบกำกับฯ", value=date(2026, 5, 28))
    vat_record_date = st.date_input("วันที่บันทึกภาษีซื้อ", value=date(2026, 5, 28))
    account_code = st.text_input("บัญชี", value="114102")
    group_code = st.text_input("กลุ่มจัดประเภท", value="G004-00014")
    vat_rate = st.number_input("อัตราภาษี", min_value=0.0, max_value=1.0, value=VAT_RATE, step=0.01, format="%.2f")
    default_container = st.text_input("ตู้เริ่มต้น ถ้าอ่านไม่ได้", value="")
st.markdown("---")
step_header("1", "อัปโหลดใบขน", "ระบบจะอ่านยอด VAT และรายการสินค้าที่นำเข้าให้อัตโนมัติ")

customs_pdf = st.file_uploader(
    "เลือกไฟล์ใบขน PDF",
    type=["pdf"],
    key="customs_pdf"
)

if customs_pdf is not None:

    customs_text, customs_method = extract_pdf_text(customs_pdf)

    st.success(f"อ่านใบขนสำเร็จ ({customs_method})")

    # ===== แยกรายการสินค้าแต่ละหมวดจากใบขน (ชื่อ/จำนวน/มูลค่า/VAT) =====
    auto_customs_items = parse_customs_items(customs_text)

    st.markdown(f"**รายการสินค้าที่อ่านได้จากใบขน** (พบอัตโนมัติ {len(auto_customs_items)} รายการ — ถ้าอ่านไม่ครบ/ผิด แก้ไขหรือเพิ่มแถวเองในตารางด้านล่างได้เลย)")

    customs_items_df_source = pd.DataFrame(auto_customs_items) if auto_customs_items else pd.DataFrame(
        columns=["name_en", "quantity", "value", "vat"]
    )
    if not customs_items_df_source.empty:
        customs_items_df_source["หมวดหมู่ (ไทย)"] = customs_items_df_source["name_en"].apply(
            lambda n: CUSTOMS_NAME_TO_TH.get(n, "")
        )
    else:
        customs_items_df_source["หมวดหมู่ (ไทย)"] = []

    customs_items_df_source = customs_items_df_source.rename(
        columns={"name_en": "ชื่อสินค้า (อังกฤษ)", "quantity": "จำนวน", "value": "มูลค่า (ฐาน VAT)", "vat": "VAT"}
    )[["หมวดหมู่ (ไทย)", "ชื่อสินค้า (อังกฤษ)", "จำนวน", "มูลค่า (ฐาน VAT)", "VAT"]]

    edited_customs_items_df = st.data_editor(
        customs_items_df_source,
        use_container_width=True,
        num_rows="dynamic",
        column_config={
            "หมวดหมู่ (ไทย)": st.column_config.SelectboxColumn(options=sorted(set(CUSTOMS_NAME_TO_TH.values()))),
            "จำนวน": st.column_config.NumberColumn(format="%.2f", min_value=0.0),
            "มูลค่า (ฐาน VAT)": st.column_config.NumberColumn(format="%.2f", min_value=0.0),
            "VAT": st.column_config.NumberColumn(format="%.2f", min_value=0.0),
        },
        key="customs_items_editor",
    )

    # แปลงกลับเป็นรูปแบบที่ใช้ในการคำนวณ โดยยึด "หมวดหมู่ (ไทย)" ที่แก้ไขแล้วเป็นหลัก
    customs_items = []
    for _, r in edited_customs_items_df.iterrows():
        th_name = r.get("หมวดหมู่ (ไทย)") or CUSTOMS_NAME_TO_TH.get(r.get("ชื่อสินค้า (อังกฤษ)", ""), "")
        if not th_name or pd.isna(r.get("จำนวน")) or float(r.get("จำนวน", 0)) <= 0:
            continue
        customs_items.append({
            "name_en": r.get("ชื่อสินค้า (อังกฤษ)", ""),
            "name_th_override": th_name,
            "quantity": float(r.get("จำนวน", 0)),
            "value": float(r.get("มูลค่า (ฐาน VAT)", 0)),
            "vat": float(r.get("VAT", 0)),
        })
    st.session_state["customs_items"] = customs_items

    # ===== หายอด VAT: ยึดจากตารางสรุปหัวใบขนเป็นอันดับแรก (แม่นยำสุด ไม่พึ่งข้อความไทยที่ OCR มักอ่านผิด) =====
    auto_vat_amount = extract_vat_from_header(customs_text)

    if auto_vat_amount == 0.0 and customs_items:
        # สำรอง: ถ้าหาจากหัวตารางไม่เจอ ใช้ผลรวมรายการสินค้าแทน (อาจคลาดเคลื่อนเล็กน้อยจากการปัดเศษ)
        auto_vat_amount = round(sum(it["vat"] for it in customs_items), 2)

    if auto_vat_amount == 0.0:
        # สำรองสุดท้าย: หาจากคำภาษาไทย/อังกฤษตรงๆ ในข้อความ (เผื่อเป็น PDF ที่มี text layer จริง)
        for line in customs_text.splitlines():
            if "ภาษีมูลค่าเพิ่ม" in line or "VALUE ADDED TAX" in line.upper():
                matches = re.findall(r'[\d,]+\.\d{2}', line)
                if matches:
                    auto_vat_amount = float(matches[-1].replace(",", ""))
                    break

    # ยอด VAT เป็นตัวเลขที่สำคัญที่สุดของทั้งระบบ (ทุกอย่างอ้างอิงจากตัวนี้)
    # จึงให้ผู้ใช้ตรวจสอบ/แก้ไขได้เสมอ ไม่เชื่อระบบอัตโนมัติ 100% เพราะ OCR ใบขนสแกนมีโอกาสอ่านผิดจุดได้
    st.markdown("**ยอด VAT รวมจากใบขน** (ตรวจสอบเลขให้ตรงกับช่อง \"ภาษีมูลค่าเพิ่ม\" ในตารางสรุปด้านบนสุดของใบขนเสมอ)")
    vat_amount = st.number_input(
        "ยอด VAT รวมจากใบขน (บาท) — แก้ไขได้ถ้าอ่านผิด",
        min_value=0.0,
        value=float(auto_vat_amount),
        step=0.01,
        format="%.2f",
        key="confirmed_vat_amount",
    )
    if auto_vat_amount == 0.0:
        st.warning("⚠️ ระบบอ่านยอด VAT อัตโนมัติไม่เจอ กรุณากรอกยอด VAT เอง (ดูจากช่อง \"ภาษีมูลค่าเพิ่ม\" ในใบขน)")

    base_vat = round(vat_amount / vat_rate, 2) if vat_amount else 0.0

    col1, col2 = st.columns(2)

    col1.metric("💰 VAT จากใบขน", f"{vat_amount:,.2f}")
    col2.metric("🧮 ฐาน VAT (VAT ÷ อัตราภาษี)", f"{base_vat:,.2f}")

    st.session_state["target_base_vat"] = base_vat

    # ===== หาเลขตู้ =====
    containers = sorted(set(re.findall(r"\b[A-Z]{4}\d{7}\b", customs_text)))

    if containers:
        st.subheader("เลขตู้ที่พบ")
        for c in containers:
            st.write(f"🚚 {c}")

    # เก็บไว้ใช้ตอน Export PEAK
    st.session_state["target_base_vat"] = base_vat
    st.session_state["vat_amount"] = vat_amount

step_header("2", "อัปโหลดใบรับสินค้า", "แตกรายการสินค้าอัตโนมัติ แล้วจับคู่ราคากับใบขนตามหมวดหมู่")

uploaded_pdfs = st.file_uploader(
    "อัปโหลด PDF ใบรับสินค้า เช่น 1386.pdf, 1388.pdf",
    type=["pdf"],
    accept_multiple_files=True,
)
uploaded_template = st.file_uploader(
    "อัปโหลดรายการนำเข้า PEAK เดิม เพื่อใช้เป็น master ราคา/คำอธิบาย",
    type=["xlsx", "xls"],
)

if not uploaded_pdfs:
    st.info("อัปโหลด PDF ใบรับสินค้าก่อน แล้วระบบจะแตกแถวสินค้าให้อัตโนมัติ")
    st.stop()

raw_text_by_file = {}
items = []
with st.status("กำลังอ่าน PDF...", expanded=True) as status:
    for uploaded_pdf in uploaded_pdfs:
        text, method = extract_pdf_text(uploaded_pdf)
        raw_text_by_file[uploaded_pdf.name] = text
        parsed_items = parse_receipt_items(uploaded_pdf.name, text)
        items.extend(parsed_items)
        st.write(f"{uploaded_pdf.name}: {method}, พบ {len(parsed_items)} รายการ")
    status.update(label="อ่าน PDF เสร็จแล้ว", state="complete")

if not items:
    st.error("ยังไม่พบรายการสินค้าใน PDF กรุณาเปิดดูข้อความ OCR ด้านล่างหรือเพิ่มรายการในตารางเอง")
    items_df = pd.DataFrame(columns=["ไฟล์ต้นทาง", "ใบรับสินค้า", "วันที่ใบรับสินค้า", "ตู้", "ลำดับในใบ", "สินค้า/บริการ", "จำนวน", "ราคาต่อหน่วย", "ชื่อสินค้า", "ข้อความหมายเหตุ"])
else:
    product_master = read_product_master(uploaded_template)
    items = apply_product_master(items, product_master)
    items_df = pd.DataFrame(items)

st.markdown('<div style="height:0.4rem"></div>', unsafe_allow_html=True)
st.subheader("📝 ตรวจรายการสินค้า")
edited_items = st.data_editor(
    items_df,
    use_container_width=True,
    num_rows="dynamic",
    column_config={
        "จำนวน": st.column_config.NumberColumn(format="%d", min_value=0),
        "ราคาต่อหน่วย": st.column_config.NumberColumn(format="%.4f", min_value=0.0),
    },
)

settings = {
    "document_date": parse_yyyymmdd(document_date),
    "reference_no": reference_no,
    "partner_code": partner_code,
    "tax_invoice_no": tax_invoice_no,
    "tax_invoice_date": parse_yyyymmdd(tax_invoice_date),
    "vat_record_date": parse_yyyymmdd(vat_record_date),
    "account_code": account_code,
    "group_code": group_code,
    "vat_rate": vat_rate,
    "container": default_container,
}

customs_items = st.session_state.get("customs_items", [])
customs_vat_amount = st.session_state.get("vat_amount", 0.0)
customs_base_vat = st.session_state.get("target_base_vat", 0.0)

# สร้างตารางค้นหา: ชื่อหมวดไทย -> รายการในใบขน (รวมกรณีมีหลายรายการชื่อเดียวกัน)
customs_by_th_name: dict[str, dict] = {}
for it in customs_items:
    th_name = it.get("name_th_override") or CUSTOMS_NAME_TO_TH.get(it["name_en"])
    if not th_name:
        continue
    agg = customs_by_th_name.setdefault(th_name, {"quantity": 0.0, "value": 0.0})
    agg["quantity"] += it["quantity"]
    agg["value"] += it["value"]

# ยึดยอด "ฐาน VAT ที่ยืนยันแล้ว" เป็นตัวตั้งต้นเสมอ (anchor) แล้วปรับสเกลมูลค่าแต่ละหมวดให้รวมกันตรงเป๊ะกับยอดนี้
# (มูลค่าต่อหมวดที่อ่านได้จากใบขนอาจคลาดเคลื่อนเล็กน้อยจากการปัดเศษ OCR แต่สัดส่วนระหว่างหมวดยังเชื่อถือได้)
sum_customs_value = sum(g["value"] for g in customs_by_th_name.values())
scale = (customs_base_vat / sum_customs_value) if (customs_base_vat > 0 and sum_customs_value > 0) else 1.0

items_records = edited_items.to_dict("records")
unmatched_categories = []
category_check_rows = []
allocated_records = []

if customs_by_th_name and customs_base_vat > 0:
    items_by_category: dict[str, list[dict]] = {}
    for row in items_records:
        items_by_category.setdefault(row.get("ชื่อสินค้า", ""), []).append(row)

    for th_name, rows in items_by_category.items():
        customs_group = customs_by_th_name.get(th_name)
        group_total_qty = sum(float(r["จำนวน"]) for r in rows)

        if customs_group and group_total_qty > 0:
            adjusted_value = round(customs_group["value"] * scale, 2)
            # แบ่งราคาต่อหน่วยเป็น 2 ระดับ (ห่างกัน 1 สตางค์) ให้ผลรวมของหมวดนี้ตรงกับ adjusted_value เป๊ะๆ
            tiered_rows = allocate_tiered_rows(rows, customs_group["quantity"], adjusted_value)
            allocated_records.extend(tiered_rows)

            qty_diff = round(group_total_qty - customs_group["quantity"], 2)
            category_check_rows.append({
                "หมวดสินค้า": th_name,
                "จำนวนในใบรับ": group_total_qty,
                "จำนวนในใบขน": customs_group["quantity"],
                "ผลต่างจำนวน": qty_diff,
                "สถานะ": "✅ ตรงกัน" if qty_diff == 0 else "⚠️ ไม่ตรงกัน",
            })
        else:
            # ไม่พบหมวดนี้ในใบขน -> ใช้ราคาที่ตั้งไว้เดิม (default/master) ไปก่อน พร้อมตั้งค่าสถานะไว้เตือน
            allocated_records.extend(rows)
            unmatched_categories.append(th_name)

    value_metric_label = "มูลค่าสินค้า (ยึดฐาน VAT ใบขนเป็นหลัก)"
    vat_metric_label = "VAT (คำนวณจากมูลค่าที่ตรงใบขนแล้ว)"
else:
    # ยังไม่มียอด VAT ที่ยืนยัน หรือยังไม่มีรายการจากใบขนเลย -> ใช้ราคาที่ตั้งไว้เดิมทั้งหมด พร้อมเตือน
    allocated_records = items_records
    value_metric_label = "มูลค่าสินค้า (ยังไม่ผูกกับใบขน)"
    vat_metric_label = "VAT โดยประมาณ (ยังไม่ผูกกับใบขน)"
    st.warning("⚠️ ยังไม่มียอด VAT ที่ยืนยันจากใบขน ตัวเลขนี้เป็นค่าประมาณการเท่านั้น กรุณาอัปโหลด/ยืนยันยอด VAT จากใบขนด้านบนก่อน เพื่อให้ยอดตรงกัน")

if unmatched_categories:
    st.warning(
        "⚠️ ไม่พบหมวดสินค้าต่อไปนี้ในใบขน จึงใช้ราคาประมาณการแทน: "
        + ", ".join(sorted(set(unmatched_categories)))
    )

allocated_items_df = pd.DataFrame(allocated_records) if allocated_records else edited_items
peak_df = build_peak_rows(allocated_items_df, settings)

qty_numeric = pd.to_numeric(peak_df["จำนวน"], errors="coerce").fillna(0)
price_numeric = pd.to_numeric(peak_df["ราคาต่อหน่วย"], errors="coerce").fillna(0)
peak_df["มูลค่า"] = (qty_numeric * price_numeric).round(2)
peak_df["VAT (จากใบขน)"] = (peak_df["มูลค่า"] * vat_rate).round(2)

total_value = peak_df["มูลค่า"].sum()
vat_total_display = peak_df["VAT (จากใบขน)"].sum()

summary_df = (
    peak_df.groupby(["อ้างอิงถึง", "สินค้า/บริการ"], as_index=False)
    .agg(จำนวน=("จำนวน", "sum"), มูลค่า=("มูลค่า", "sum"), VAT=("VAT (จากใบขน)", "sum"))
)

if category_check_rows:
    st.subheader("ตรวจสอบจำนวนสินค้าต่อหมวด (ใบรับ vs ใบขน)")
    st.dataframe(pd.DataFrame(category_check_rows), use_container_width=True)

step_header("3", "ตรวจสอบและส่งออกไฟล์", "เทียบยอดกับใบขนแล้วดาวน์โหลดไฟล์นำเข้า PEAK")
st.subheader("Preview ไฟล์นำเข้า PEAK")
st.dataframe(peak_df, use_container_width=True)

metric_cols = st.columns(4)
metric_cols[0].metric("จำนวนแถว", f"{len(peak_df):,}")
metric_cols[1].metric("รวมจำนวน", f"{pd.to_numeric(peak_df['จำนวน'], errors='coerce').sum():,.0f}")
metric_cols[2].metric(value_metric_label, f"{total_value:,.2f}")
metric_cols[3].metric(vat_metric_label, f"{vat_total_display:,.2f}")

check_cols = st.columns(2)
if customs_base_vat > 0:
    value_diff_check = round(total_value - customs_base_vat, 2)
    if abs(value_diff_check) <= 0.05:
        check_cols[0].success(f"✅ มูลค่าสินค้ารวมตรงกับฐาน VAT ใบขน ({customs_base_vat:,.2f} บาท)")
    else:
        check_cols[0].error(f"⚠️ มูลค่าสินค้ารวมยังต่างจากฐาน VAT ใบขนอยู่ {value_diff_check:,.2f} บาท")

if customs_vat_amount > 0:
    vat_diff = round(vat_total_display - customs_vat_amount, 2)
    if abs(vat_diff) <= 0.05:
        check_cols[1].success(f"✅ ยอด VAT รวมตรงกับใบขน ({customs_vat_amount:,.2f} บาท)")
    else:
        check_cols[1].error(f"⚠️ ยอด VAT รวมยังต่างจากใบขนอยู่ {vat_diff:,.2f} บาท กรุณาตรวจสอบรายการสินค้า")

peak_export_df = peak_df[PEAK_COLUMNS]
excel_bytes = to_excel_bytes({"PEAK Import": peak_export_df, "Summary": summary_df})
csv_bytes = peak_export_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")

download_cols = st.columns(2)
download_cols[0].download_button(
    "ดาวน์โหลด Excel นำเข้า PEAK",
    data=excel_bytes,
    file_name=f"{reference_no}_peak_import.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
download_cols[1].download_button(
    "ดาวน์โหลด CSV",
    data=csv_bytes,
    file_name=f"{reference_no}_peak_import.csv",
    mime="text/csv",
)

with st.expander("ข้อความที่อ่านได้จาก PDF"):
    for filename, text in raw_text_by_file.items():
        st.markdown(f"**{filename}**")
        st.text_area("ข้อความ", text, height=180, key=f"text-{filename}")
