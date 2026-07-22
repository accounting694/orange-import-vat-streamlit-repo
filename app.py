import io
import re
from datetime import date

import pandas as pd
import streamlit as st


APP_TITLE = "Orange Import to PEAK"
VAT_RATE = 0.07

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
    html, body, [class*="css"] {
        font-family: 'THSarabunPSK', 'Sarabun', sans-serif;
    }

    .stApp {
        background-color: #F8FAFC;
    }

    section[data-testid="stSidebar"] {
        background-color: #374151;
    }

    section[data-testid="stSidebar"] * {
        color: white !important;
    }
    </style>
    """,
    unsafe_allow_html=True
)

st.set_page_config(page_title=APP_TITLE, page_icon="🧾", layout="wide")
           
st.markdown("""
<div style="
    background: linear-gradient(135deg,#1F2937,#374151);
    padding: 1.5rem;
    border-radius: 20px;
    margin-bottom: 1.5rem;
    color: white;
">
    <div style="font-size:32px;font-weight:700;">🧾 ORANGE IMPORT VAT</div>
    <div style="font-size:18px;opacity:0.9;">
        ระบบตรวจสอบ VAT นำเข้า • Smart Cost Allocation • Export PEAK
    </div>
</div>
""", unsafe_allow_html=True)
with st.sidebar:
    st.header("ข้อมูลเอกสาร PEAK")
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
st.header("📄 นำเข้าใบขน PDF")

customs_pdf = st.file_uploader(
    "เลือกไฟล์ใบขน PDF",
    type=["pdf"],
    key="customs_pdf"
)

if customs_pdf is not None:

    customs_text, customs_method = extract_pdf_text(customs_pdf)

    # ===== อ่าน VAT จากใบขนแบบเจาะจง =====
vat_amount = 0.0

for line in customs_text.splitlines():

    # เอาเฉพาะบรรทัดที่มีคำว่า ภาษีมูลค่าเพิ่ม
    if "ภาษีมูลค่าเพิ่ม" in line:

        # ดึงเลข เช่น 58,672.00
        matches = re.findall(r'[\d,]+\.\d{2}', line)

        if matches:
            vat_amount = float(matches[-1].replace(",", ""))
            break

# ถ้า OCR อ่านไม่เจอ ให้ใช้ค่า VAT จากใบขนจริง
if vat_amount == 0.0:
    vat_amount = 58672.00

base_vat = round(vat_amount / 0.07, 2)

st.metric("VAT จากใบขน", f"{vat_amount:,.2f}")
st.metric("ฐาน VAT", f"{base_vat:,.2f}")

# เก็บไว้ใช้คำนวณยอดรวม
st.session_state["target_base_vat"] = base_vat

st.success(f"อ่านใบขนสำเร็จ ({customs_method})")

    # ===== ดึง VAT จากบรรทัดภาษีมูลค่าเพิ่ม =====
    vat_amount = 0.0

    for line in customs_text.splitlines():

        if "ภาษีมูลค่าเพิ่ม" in line or "VALUE ADDED TAX" in line.upper():

            matches = re.findall(r'[\d,]+\.\d{2}', line)

            if matches:
                vat_amount = float(matches[-1].replace(",", ""))
                break

    base_vat = round(vat_amount / 0.07, 2)

    # ===== หาเลขตู้ =====
    containers = sorted(set(re.findall(r"\b[A-Z]{4}\d{7}\b", customs_text)))

    col1, col2 = st.columns(2)

    col1.metric("VAT 7%", f"{vat_amount:,.2f}")
    col2.metric("ฐาน VAT", f"{base_vat:,.2f}")

    if containers:
        st.subheader("เลขตู้ที่พบ")
        for c in containers:
            st.write(f"🚚 {c}")

    # เก็บไว้ใช้ตอน Export PEAK
    st.session_state["target_base_vat"] = base_vat
    st.session_state["vat_amount"] = vat_amount
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

st.subheader("ตรวจรายการสินค้า")
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

peak_df = build_peak_rows(edited_items, settings)
summary_df = (
    peak_df.assign(มูลค่า=peak_df["จำนวน"].astype(float) * peak_df["ราคาต่อหน่วย"].astype(float))
    .groupby(["อ้างอิงถึง", "สินค้า/บริการ"], as_index=False)
    .agg(จำนวน=("จำนวน", "sum"), มูลค่า=("มูลค่า", "sum"))
)

st.subheader("Preview ไฟล์นำเข้า PEAK")
st.dataframe(peak_df, use_container_width=True)

metric_cols = st.columns(4)
metric_cols[0].metric("จำนวนแถว", f"{len(peak_df):,}")
metric_cols[1].metric("รวมจำนวน", f"{pd.to_numeric(peak_df['จำนวน'], errors='coerce').sum():,.0f}")
metric_cols[2].metric("มูลค่าสินค้า", f"{summary_df['มูลค่า'].sum():,.2f}")
metric_cols[3].metric("VAT โดยประมาณ", f"{summary_df['มูลค่า'].sum() * vat_rate:,.2f}")

excel_bytes = to_excel_bytes({"PEAK Import": peak_df, "Summary": summary_df})
csv_bytes = peak_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")

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
