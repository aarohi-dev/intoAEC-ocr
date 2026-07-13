import fitz
import pdfplumber

import re

def parse_float(val):
    if not isinstance(val, str):
        return 0.0
    # Extract numeric part, ignoring commas and currency symbols
    cleaned = re.sub(r'[^\d.-]', '', val)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0

def generate_summary_table(tables):
    summary = {}
    for table in tables:
        if not table or len(table) < 2:
            continue
            
        # Try to guess columns based on the first data row (skip header)
        # We assume the column with the longest average text is the description.
        # We assume columns that can be parsed as float are numeric data to be summed.
        header = table[0]
        data_rows = table[1:]
        
        if not data_rows:
            continue
            
        # Find numeric columns
        numeric_cols = []
        for col_idx in range(len(header)):
            # Check if this column is mostly numeric
            numeric_count = 0
            for row in data_rows:
                if col_idx < len(row) and parse_float(row[col_idx]) != 0.0:
                    numeric_count += 1
            if numeric_count > 0:
                numeric_cols.append(col_idx)
                
        # Find description column (first non-numeric column, or just col 0)
        desc_col = 0
        for col_idx in range(len(header)):
            if col_idx not in numeric_cols:
                desc_col = col_idx
                break
                
        for row in data_rows:
            if desc_col < len(row):
                desc = row[desc_col].strip()
                if not desc or len(desc) < 2:
                    continue
                    
                if desc not in summary:
                    summary[desc] = [0.0] * len(numeric_cols)
                    
                for i, n_col in enumerate(numeric_cols):
                    if n_col < len(row):
                        while len(summary[desc]) <= i:
                            summary[desc].append(0.0)
                        summary[desc][i] += parse_float(row[n_col])
                        
    if not summary:
        return []
        
    # Format the summary table
    max_cols = max(len(totals) for totals in summary.values())
    
    result = [["Description"] + [f"Total {i+1}" for i in range(max_cols)]]
    for desc, totals in summary.items():
        padded_totals = totals + [0.0] * (max_cols - len(totals))
        formatted_totals = [f"{t:g}" if t.is_integer() else f"{t:.2f}" for t in padded_totals]
        result.append([desc] + formatted_totals)
        
    return result

def extract_data(pdf_path: str) -> dict:
    """
    Extracts verbatim text and structured tables from a PDF.
    """
    data = {
        "text": "",
        "tables": [],
        "summary_table": []
    }
    
    # 1. Extract raw text using PyMuPDF (fitz) for perfect layout handling
    doc = fitz.open(pdf_path)
    text_parts = []
    for pg_idx in range(len(doc)):
        page = doc[pg_idx]
        blocks = page.get_text("blocks")
        blocks.sort(key=lambda b: (b[1], b[0]))
        page_text = "\\n\\n".join([b[4] for b in blocks if b[6] == 0])
        text_parts.append(f"--- Page {pg_idx + 1} ---\\n{page_text}")
    doc.close()
    
    data["text"] = "\\n\\n".join(text_parts)
    
    # 2. Extract tables using pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                cleaned_table = []
                for row in table:
                    cleaned_row = [str(cell).strip() if cell is not None else "" for cell in row]
                    if any(cleaned_row):
                        cleaned_table.append(cleaned_row)
                
                if cleaned_table:
                    data["tables"].append(cleaned_table)
                    
    # 3. Generate summary table
    if data["tables"]:
        data["summary_table"] = generate_summary_table(data["tables"])
                    
    return data
