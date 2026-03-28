import streamlit as st

# --- パスワード認証機能 ---
def check_password():
    def password_entered():
        if st.session_state["password"] == st.secrets["password"]:
            st.session_state["password_correct"] = True
            del st.session_state["password"]  # セッションから削除
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        st.text_input("パスワードを入力してください", type="password", on_change=password_entered, key="password")
        return False
    elif not st.session_state["password_correct"]:
        st.text_input("パスワードを入力してください", type="password", on_change=password_entered, key="password")
        st.error("😕 パスワードが違います")
        return False
    else:
        return True

if not check_password():
    st.stop()  # 認証が通るまで以下の処理を実行しない

import streamlit as st
import fitz
import re
import io
import os
from PIL import Image
import base64
import streamlit.components.v1 as components

st.set_page_config(page_title="PDF抹消・左寄せツール", layout="centered", initial_sidebar_state="expanded")

st.markdown("""
<style>
/* iOSスワイプバック誤作動防止（横スライダー操作時・画像スワイプ時） */
body {
    overscroll-behavior-x: none;
}
div[data-baseweb="slider"] {
    touch-action: pan-y !important;
}
div[data-testid="stImage"], div[data-testid="stImage"] img {
    touch-action: auto !important; /* ピンチズームやダブルタップズームを許可 */
}
</style>
""", unsafe_allow_html=True)

def _add_safe_redact(page, rect):
    annot = page.add_redact_annot(rect, cross_out=False)
    annot.set_colors(stroke=None, fill=None)
    annot.update()

@st.cache_data(show_spinner=False)
def extract_and_redact_page(pdf_bytes, page_index, mode, limit_to_cells):
    if mode == 0:
        return None, [], []
        
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    temp_doc = fitz.open()
    temp_doc.insert_pdf(doc, from_page=page_index, to_page=page_index)
    doc.close()
    
    page = temp_doc[0]

    extracted_items = []
    extracted_images = []
    redact_rects = []
    paths = page.get_drawings()
    h_lines = [p["rect"] for p in paths if p["rect"].height < 5 and p["rect"].width > 5]
    v_lines = [p["rect"] for p in paths if p["rect"].width < 5 and p["rect"].height > 5]
    
    def is_in_cell(rect):
        if not limit_to_cells: return True
        cx = (rect.x0 + rect.x1) / 2
        cy = (rect.y0 + rect.y1) / 2
        has_h_above = any(h.y1 <= cy and h.x0 - 5 <= cx <= h.x1 + 5 for h in h_lines)
        if not has_h_above: return False
        has_h_below = any(h.y0 >= cy and h.x0 - 5 <= cx <= h.x1 + 5 for h in h_lines)
        if not has_h_below: return False
        has_v_left = any(v.x1 <= cx and v.y0 - 5 <= cy <= v.y1 + 5 for v in v_lines)
        if not has_v_left: return False
        has_v_right = any(v.x0 >= cx and v.y0 - 5 <= cy <= v.y1 + 5 for v in v_lines)
        return has_v_right

    def is_right_aligned_in_cell(c_rect, current_line):
        if not limit_to_cells: return False
        cy = (c_rect.y0 + c_rect.y1) / 2
        raw_x = sorted([v.x0 for v in v_lines if v.y0 - 5 <= cy <= v.y1 + 5])
        if not raw_x: return False
        x_splits = [raw_x[0]]
        for x in raw_x[1:]:
            if x - x_splits[-1] > 3: x_splits.append(x)
        if len(x_splits) < 2: return False
        
        best_i = -1
        max_overlap = -1
        for i in range(len(x_splits)-1):
            overlap = max(0, min(c_rect.x1, x_splits[i+1]) - max(c_rect.x0, x_splits[i]))
            if overlap > max_overlap:
                max_overlap = overlap
                best_i = i
        if best_i == -1 or max_overlap <= 0: return False
        
        cell_x0 = x_splits[best_i]
        cell_x1 = x_splits[best_i+1]
        cell_width = cell_x1 - cell_x0
        if cell_width <= 0: return False
        
        cluster_x1 = c_rect.x1
        for s in current_line["spans"]:
            for char_dict in s.get("chars", []):
                cb = char_dict["bbox"]
                if cb[0] >= cell_x0 - 5 and cb[2] <= cell_x1 + 5:
                    cluster_x1 = max(cluster_x1, cb[2])
        rel_x1 = (cluster_x1 - cell_x0) / cell_width
        return rel_x1 > 0.73

    circle_candidates = []
    for p in paths:
        r = p["rect"]
        if 8 < r.width < 50 and 8 < r.height < 50 and (0.6 < r.width/r.height < 1.7):
            if any(item[0] == "c" for item in p["items"]):
                circle_candidates.append(r)

    merged_circles = []
    while circle_candidates:
        curr = circle_candidates.pop(0)
        found = False
        for i, m in enumerate(merged_circles):
            if (m + (-3, -3, 3, 3)).intersects(curr):
                merged_circles[i] = m | curr; found = True; break
        if not found: merged_circles.append(curr)

    def is_circled_number_match(char_txt):
        if not re.match(r'[\u1F00-\u1FFF\u2460-\u24FF\u2776-\u2793\u2800-\u28FF\u3251-\u32FF\uE000-\uF8FF①-㊿\uFFFD]', char_txt): return False
        if re.search(r'[一-龠ぁ-んァ-ヶ]', char_txt): return False
        if char_txt in '△▽▲▼〃※・。、！？：；，．()（）[]【】「」『』〈〉《》〔〕ー〜～=＝+＋/／_＿|｜々〆〇〒': return False
        return True

    blocks = page.get_text("rawdict")["blocks"]

    left_brackets = []
    right_brackets = []
    for b in blocks:
        if b["type"] == 0:
            for l in b["lines"]:
                for s in l["spans"]:
                    for c in s.get("chars", []):
                        if c["c"] in '([【（': left_brackets.append(fitz.Rect(c["bbox"]))
                        elif c["c"] in ')]】）': right_brackets.append(fitz.Rect(c["bbox"]))

    def is_in_brackets(rect):
        cx = (rect.x0 + rect.x1) / 2
        cy = (rect.y0 + rect.y1) / 2
        same_y_lefts = [lb for lb in left_brackets if abs((lb.y0+lb.y1)/2 - cy) < 5]
        same_y_rights = [rb for rb in right_brackets if abs((rb.y0+rb.y1)/2 - cy) < 5]
        leftmost_l = max([lb.x1 for lb in same_y_lefts if lb.x1 <= cx], default=-1)
        leftmost_r = max([rb.x1 for rb in same_y_rights if rb.x1 <= cx], default=-1)
        if leftmost_l == -1 or leftmost_l < leftmost_r: return False
        rightmost_l = min([lb.x0 for lb in same_y_lefts if lb.x0 >= cx], default=float('inf'))
        rightmost_r = min([rb.x0 for rb in same_y_rights if rb.x0 >= cx], default=float('inf'))
        if rightmost_r == float('inf') or rightmost_r > rightmost_l: return False
        return True

    pre_redact_rects = []
    for b in blocks:
        if b["type"] == 0:
            for l in b["lines"]:
                moving_rects = []
                for s in l["spans"]:
                    for c in s.get("chars", []):
                        char_txt = c["c"]
                        is_moving = False
                        if mode == 1 and re.match(r'[0-9０-９]', char_txt):
                            if is_in_cell(fitz.Rect(c["bbox"])): is_moving = True
                        elif mode == 2:
                            if is_circled_number_match(char_txt) and is_in_cell(fitz.Rect(c["bbox"])): is_moving = True
                            elif is_in_brackets(fitz.Rect(c["bbox"])) and re.search(r'[0-9０-９]', char_txt) and is_in_cell(fitz.Rect(c["bbox"])): is_moving = True
                            else:
                                span_rect = fitz.Rect(c["bbox"])
                                if span_rect.is_valid:
                                    for circle in merged_circles:
                                        if (circle.contains(span_rect + (2, 2, -2, -2)) or circle.intersects(span_rect)) and is_in_cell(span_rect):
                                            is_moving = True; break
                        if is_moving: moving_rects.append(fitz.Rect(c["bbox"]))
                                    
                if moving_rects and mode != 2:
                    for s in l["spans"]:
                        for c in s.get("chars", []):
                            char_txt = c["c"]
                            c_rect = fitz.Rect(c["bbox"])
                            is_bracket = False
                            if re.match(r'[()（）\[\]【】〈〉《》〔〕『』「」\x00-\x1F\x7F-\x9F]', char_txt): is_bracket = True
                            elif char_txt in ('i', 'I', 'l', '|'):
                                min_dist = min(abs(c_rect.x1 - mr.x0) if c_rect.x0 < mr.x0 else abs(mr.x1 - c_rect.x0) for mr in moving_rects)
                                if min_dist < 15: is_bracket = True
                                    
                            if is_bracket and is_in_cell(c_rect):
                                c_rect = c_rect + (-1, -1, 1, 1)
                                _add_safe_redact(page, c_rect)
                                pre_redact_rects.append(c_rect)
                                
    if pre_redact_rects:
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=0, text=0)

    for b in blocks:
        if b["type"] == 0:
            for l in b["lines"]:
                for s in l["spans"]:
                    chars = s.get("chars", [])
                    span_txt = "".join([c["c"] for c in chars])
                    if not span_txt.strip() or re.search(r'[一-龠ぁ-んァ-ヶ]', span_txt): continue
                    
                    is_hidden_circle = False
                    span_rect = fitz.Rect(s["bbox"])
                    if mode == 2 and span_rect.is_valid:
                        for circle in merged_circles:
                            if circle.contains(span_rect + (2, 2, -2, -2)) or circle.intersects(span_rect):
                                is_hidden_circle = True; break

                    for c in chars:
                        char_txt = c["c"]
                        if re.match(r'[\s]', char_txt): continue
                        c_rect = fitz.Rect(c["bbox"])
                        if re.match(r'[_ー\-－]', char_txt) or (c_rect.height > 0 and (c_rect.width / c_rect.height > 3.0)): continue

                        is_move = False; is_del = False
                        is_special_font = False

                        if mode != 2 and (re.match(r'[()（）\[\]【】〈〉《》〔〕『』「」\s\x00-\x1F\x7F-\x9F]', char_txt) or char_txt in ('', 'i')): 
                            if is_in_cell(c_rect): is_del = True
                        elif mode == 2 and (re.match(r'[\s\x00-\x1F\x7F-\x9F]', char_txt) or char_txt == ''):
                            if is_in_cell(c_rect): is_del = True
                        elif mode == 1 and re.match(r'[0-9０-９]', char_txt): 
                            if is_in_cell(c_rect) and not is_right_aligned_in_cell(c_rect, l):
                                is_move = True; is_del = True
                        elif mode == 2:
                            if is_circled_number_match(char_txt):
                                if is_in_cell(c_rect): is_move = True; is_del = True; is_special_font = True
                            elif is_in_brackets(c_rect) and re.match(r'[0-9０-９]', char_txt): 
                                if is_in_cell(c_rect): is_move = True; is_del = True
                            elif is_hidden_circle and re.match(r'[0-9０-９]', char_txt): 
                                if is_in_cell(c_rect): is_move = True; is_del = True
                        
                        if is_move:
                            if is_special_font:
                                cap_rect = c_rect + (0.5, 0, -0.5, -0.5)
                                cap_rect = cap_rect.intersect(page.rect)
                                pix = page.get_pixmap(clip=cap_rect, matrix=fitz.Matrix(2, 2), alpha=True, annots=False)
                                if pix.width > 0 and pix.height > 0:
                                    extracted_images.append({"rect": (cap_rect.x0, cap_rect.y0, cap_rect.x1, cap_rect.y1), "bytes": pix.tobytes("png")})
                            else:
                                extracted_items.append({"text": char_txt, "rect": (c_rect.x0, c_rect.y0, c_rect.x1, c_rect.y1), "size": s["size"]})

                        if is_del:
                            _add_safe_redact(page, c_rect)
                            redact_rects.append(c_rect)

    for r in merged_circles:
        if not is_in_cell(r): continue
        pix = page.get_pixmap(clip=r.intersect(page.rect), matrix=fitz.Matrix(2, 2), alpha=True, annots=False)
        if pix.width > 0 and pix.height > 0:
            _add_safe_redact(page, r)
            extracted_images.append({"rect": (r.x0, r.y0, r.x1, r.y1), "bytes": pix.tobytes("png")})
            redact_rects.append(r)

    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=0, text=0)

    for p in paths:
        r = p["rect"]
        if (r.height < 2 and r.width > 5) or (r.width < 2 and r.height > 5):
            if any(r.intersects(mask) for mask in redact_rects):
                shape = page.new_shape()
                for item in p["items"]:
                    if item[0] == "l": shape.draw_line(item[1], item[2])
                    elif item[0] == "re": shape.draw_rect(item[1])
                    elif item[0] == "c": shape.draw_bezier(item[1], item[2], item[3], item[4])
                cap = p.get("lineCap", (0,0,0))
                cap_val = cap[0] if isinstance(cap, tuple) else cap
                shape.finish(width=p.get("width", 1), color=p.get("color", (0,0,0)), fill=p.get("fill", None), lineCap=cap_val, lineJoin=p.get("lineJoin", 0))
                shape.commit()

    redacted_bytes = temp_doc.write()
    temp_doc.close()
    
    return redacted_bytes, extracted_items, extracted_images

def render_shifted_page(pdf_bytes, page_index, mode, limit_to_cells, shift_amount, v_shift_amount):
    if mode == 0:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        temp_doc = fitz.open()
        temp_doc.insert_pdf(doc, from_page=page_index, to_page=page_index)
        doc.close()
        return temp_doc

    redacted_bytes, extracted_items, extracted_images = extract_and_redact_page(pdf_bytes, page_index, mode, limit_to_cells)
    
    temp_doc = fitz.open(stream=redacted_bytes, filetype="pdf")
    page = temp_doc[0]
    
    try: page.insert_font(fontname="cjk", fontfile=r"C:\Windows\Fonts\msmincho.ttc")
    except:
        try: page.insert_font(fontname="cjk", fontbuffer=fitz.Font("cjk").buffer)
        except: pass
        
    for item in extracted_items:
        rx0, ry0, rx1, ry1 = item["rect"]
        page.insert_text(fitz.Point(max(rx0 - shift_amount, 2), ry1 + v_shift_amount), item["text"], fontsize=item["size"], fontname="cjk", render_mode=0)
    
    for img in extracted_images:
        rx0, ry0, rx1, ry1 = img["rect"]
        width = rx1 - rx0
        page.insert_image(fitz.Rect(max(rx0 - shift_amount, 2), ry0 + v_shift_amount, max(rx0 - shift_amount, 2) + width, ry1 + v_shift_amount), stream=img["bytes"])
        
    return temp_doc

@st.cache_data(show_spinner=False, max_entries=50)
def generate_cached_preview(pdf_bytes, page_index, mode, limit_to_cells, shift_amount, v_shift_amount):
    mod_doc = render_shifted_page(pdf_bytes, page_index, mode, limit_to_cells, shift_amount, v_shift_amount)
    page = mod_doc[0]
    # プレビューレンダリングを高速化するため解像度を1.3に落として軽量化
    pix = page.get_pixmap(matrix=fitz.Matrix(1.3, 1.3))
    mod_doc.close()
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

def render_preview(pdf_bytes, page_index, mode, limit_to_cells, shift_amount, v_shift_amount):
    return generate_cached_preview(pdf_bytes, page_index, mode, limit_to_cells, shift_amount, v_shift_amount)

def create_final_pdf(pdf_bytes, apply_pages_str, mode, limit_to_cells, shift_amount, v_shift_amount):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    indices = []
    if apply_pages_str.strip():
        for part in re.split(r'[ ,]+', apply_pages_str):
            if '-' in part:
                s, e = map(int, part.split('-'))
                indices.extend(range(s-1, e))
            elif part.isdigit():
                indices.append(int(part)-1)
    else:
        # 空白の場合は全ページ対象にする機能は廃止し、空のリストを維持する
        pass
        
    indices = sorted(list(set(indices)))
    total = len(doc)
    doc.close()

    if not indices:
        return None # エラーハンドリングとしてNoneを返す


    new_doc = fitz.open()
    progress_bar = st.progress(0)
    
    for idx, i in enumerate(indices):
        if 0 <= i < total:
            mod_doc = render_shifted_page(pdf_bytes, i, mode, limit_to_cells, shift_amount, v_shift_amount)
            new_doc.insert_pdf(mod_doc)
            mod_doc.close()
        progress_bar.progress((idx + 1) / len(indices))

    pdf_out = new_doc.write(garbage=4, deflate=True)
    new_doc.close()
    return pdf_out

if "initialized" not in st.session_state:
    st.session_state.initialized = True
    st.session_state.shift_val = 41
    st.session_state.v_shift_val = 0
    st.session_state.display_page = 1
    st.session_state.apply_pages_input = "1"

if "pdf_bytes" not in st.session_state:
    st.session_state.pdf_bytes = None
if "pdf_name" not in st.session_state:
    st.session_state.pdf_name = ""

# マイナス・プラスボタン用コールバック関数
def adjust_h(val):
    new_val = st.session_state.shift_val + val
    if new_val < -50: new_val = -50
    if new_val > 400: new_val = 400
    st.session_state.shift_val = new_val

def adjust_v(val):
    new_val = st.session_state.v_shift_val + val
    if new_val < -100: new_val = -100
    if new_val > 100: new_val = 100
    st.session_state.v_shift_val = new_val

if "shift_val" not in st.session_state:
    st.session_state.shift_val = 41
if "v_shift_val" not in st.session_state:
    st.session_state.v_shift_val = 0

st.title("PDF抹消・左寄せツール")
st.markdown("**(iOSブラウザ対応版)**")
st.warning("⚠️ **注意**: Googleドライブの「スキャン機能」やスマホのカメラ等で撮影した文字が画像として認識されているPDFには対応していません。参考書や問題集などのPDFデータ（文字が選択できるPDF）をご使用ください。")

uploaded_file = st.file_uploader("1. PDFファイルをアップロード", type=["pdf"])

if uploaded_file is not None:
    if st.session_state.pdf_name != uploaded_file.name:
        st.session_state.pdf_bytes = uploaded_file.read()
        st.session_state.pdf_name = uploaded_file.name
        st.session_state.shift_val = 41
        st.session_state.v_shift_val = 0
        st.session_state.display_page = 1
        st.session_state.apply_pages_input = "1"
        
if st.session_state.pdf_bytes:
    doc_info = fitz.open(stream=st.session_state.pdf_bytes, filetype="pdf")
    total_pages = len(doc_info)
    doc_info.close()

    st.sidebar.markdown("## ⚙️ 操作・調整パネル")
    st.sidebar.markdown("※ スワイプしながらプレビューを確認し、ここで調整してください。")
    
    st.sidebar.markdown("### 2. モード選択")
    mode = st.sidebar.radio(
        "処理モード",
        options=[0, 1, 2],
        format_func=lambda x: [
            "モード0: オフ（何もしない）",
            "モード1: 全数字を左寄せ",
            "モード2: 記号・丸数字を左寄せ"
        ][x]
    )
    limit_to_cells = st.sidebar.checkbox("表のセル内の文字のみ処理対象にする", value=True)

    st.sidebar.markdown("### 3. 位置の微調整")
    if st.sidebar.button("🔄 位置の微調整を初期値（元通り）にリセット"):
        st.session_state.shift_val = 41
        st.session_state.v_shift_val = 0
        st.rerun()

    st.sidebar.markdown(f"**左右位置** (現在値: {st.session_state.shift_val}) ※マイナスで右へ")
    h_col1, h_col2, h_col3 = st.sidebar.columns([1, 3, 1])
    h_col1.button("◀", on_click=adjust_h, args=(1,), key="h_minus")
    shift_amount = h_col2.slider("左右", min_value=-50, max_value=400, key="shift_val", label_visibility="collapsed")
    h_col3.button("▶", on_click=adjust_h, args=(-1,), key="h_plus")

    st.sidebar.markdown(f"**上下位置** (現在値: {st.session_state.v_shift_val}) ※マイナスで上へ")
    v_col1, v_col2, v_col3 = st.sidebar.columns([1, 3, 1])
    v_col1.button("◀", on_click=adjust_v, args=(-1,), key="v_minus")
    v_shift_amount = v_col2.slider("上下", min_value=-100, max_value=100, key="v_shift_val", label_visibility="collapsed")
    v_col3.button("▶", on_click=adjust_v, args=(1,), key="v_plus")

    st.sidebar.markdown("### 4. 保存対象のページ")
    apply_pages = st.sidebar.text_input("保存するページ (例: 1, 3-5)", key="apply_pages_input")
    
    st.sidebar.markdown("---")
    st.sidebar.markdown("### 💾 ダウンロード")
    if st.sidebar.button("設定を反映してPDFを作成する"):
        if not apply_pages.strip():
            st.sidebar.error("⚠️ 保存するページが空白です。対象とするページ番号を入力してください。（例: 1, 3-5）")
        else:
            with st.spinner("PDF処理中..."):
                final_pdf_bytes = create_final_pdf(
                    st.session_state.pdf_bytes, 
                    apply_pages, 
                    mode, 
                    limit_to_cells, 
                    shift_amount, 
                    v_shift_amount
                )
                if final_pdf_bytes is None:
                    st.sidebar.error("⚠️ 無効なページ番号が指定されています。")
                else:
                    st.sidebar.success("完了しました！")
                    st.sidebar.download_button(
                        label="📥 処理済みPDFをダウンロード",
                        data=final_pdf_bytes,
                        file_name=f"redacted_{st.session_state.pdf_name}",
                        mime="application/pdf"
                    )
                
                if final_pdf_bytes is not None:
                    b64_pdf = base64.b64encode(final_pdf_bytes).decode('utf-8')
                    js_code = f"""
                    <script>
                        // BlobURLを生成して別タブで開くスクリプト（iOS対応版）
                        const b64 = "{b64_pdf}";
                        const binary = atob(b64);
                        const array = new Uint8Array(binary.length);
                        for(let i = 0; i < binary.length; i++) {{
                            array[i] = binary.charCodeAt(i);
                        }}
                        const blob = new Blob([array], {{type: 'application/pdf'}});
                        const url = URL.createObjectURL(blob);
                        
                        const link = document.createElement('a');
                        link.href = 'javascript:void(0);';
                        link.style.display = 'block';
                        link.style.padding = '0.8rem';
                        link.style.backgroundColor = '#1565c0';
                        link.style.color = '#ffffff';
                        link.style.textAlign = 'center';
                        link.style.borderRadius = '0.5rem';
                        link.style.textDecoration = 'none';
                        link.style.fontWeight = 'bold';
                        link.style.fontFamily = 'sans-serif';
                        link.innerText = '👆 ダウンロード完了！ここをタップで別タブ表示';
                        
                        link.onclick = function(e) {{
                            e.preventDefault();
                            // 親ウィンドウのコンテキストで別タブ起動を試みる
                            const newTab = window.parent.open(url, '_blank');
                            if (!newTab) {{
                                // Safariのポップアップブロック対策として、同タブ表示にフォールバック
                                window.location.href = url; 
                            }}
                        }};
                        
                        document.body.appendChild(link);
                    </script>
                    <style>body {{ margin: 0; padding: 0; }}</style>
                    """
                    with st.sidebar:
                        components.html(js_code, height=60)

    st.markdown("---")
    st.markdown("### 🔍 プレビュー")
    if "display_page" not in st.session_state:
        st.session_state.display_page = 1

    if "temp_number_input" not in st.session_state:
        st.session_state.temp_number_input = st.session_state.display_page
    if "temp_slider_input" not in st.session_state:
        st.session_state.temp_slider_input = st.session_state.display_page

    def change_page(val):
        new_val = st.session_state.display_page + val
        if new_val < 1: new_val = 1
        if new_val > total_pages: new_val = total_pages
        
        st.session_state.display_page = new_val
        st.session_state.temp_number_input = new_val
        st.session_state.temp_slider_input = new_val
        st.session_state.apply_pages_input = str(new_val)

    def update_page_from_number():
        st.session_state.display_page = st.session_state.temp_number_input
        st.session_state.temp_slider_input = st.session_state.display_page
        st.session_state.apply_pages_input = str(st.session_state.display_page)
        
    def update_page_from_slider():
        st.session_state.display_page = st.session_state.temp_slider_input
        st.session_state.temp_number_input = st.session_state.display_page
        st.session_state.apply_pages_input = str(st.session_state.display_page)

    st.markdown("**ページ移動 （携帯・タブレットでは左右スワイプで移動できます）**")
    
    # 1. ページ指定パネル（手打ち＆増減）
    p_col1, p_col2, p_col3 = st.columns([1, 4, 1])
    p_col1.button("◀ 前へ", on_click=change_page, args=(-1,), key="p_minus", disabled=(total_pages <= 1))
    p_col2.number_input(
        "ページ番号直接入力", 
        min_value=1, 
        max_value=max(1, total_pages), 
        value=st.session_state.display_page,
        key="temp_number_input",
        on_change=update_page_from_number,
        label_visibility="collapsed",
        step=1,
        disabled=(total_pages <= 1)
    )
    p_col3.button("次へ ▶", on_click=change_page, args=(1,), key="p_plus", disabled=(total_pages <= 1))

    # 2. 直感的操作用のスライダー枠（手をつないで連動させる）
    if total_pages > 1:
        st.slider(
            "スライダーで一気に移動", 
            min_value=1, 
            max_value=total_pages, 
            value=st.session_state.display_page,
            key="temp_slider_input",
            on_change=update_page_from_slider,
            label_visibility="collapsed"
        )
    
    with st.spinner("プレビューを生成中..."):
        img = render_preview(
            st.session_state.pdf_bytes, 
            st.session_state.display_page - 1, 
            mode, 
            limit_to_cells, 
            shift_amount, 
            v_shift_amount
        )
        st.image(img, use_container_width=True)

    # プレビュー画像等のスワイプ操作を検知してボタンをクリックさせるJS
    swipe_js = """
    <script>
    const parentDoc = window.parent.document;
    if (!parentDoc.getElementById('swipe-handler-injected')) {
        const marker = parentDoc.createElement('div');
        marker.id = 'swipe-handler-injected';
        marker.style.display = 'none';
        parentDoc.body.appendChild(marker);
        
        let touchstartX = 0;
        let touchstartY = 0;
        let touchendX = 0;
        let touchendY = 0;
        
        // Use capturing to ensure Streamlit's native events don't swallow our touches
        parentDoc.addEventListener('touchstart', function(event) {
            const target = event.target;
            // スライダーや入力フィールドでのスワイプは無視
            if (target.closest('[data-baseweb="slider"]') || target.closest('input')) {
                touchstartX = -1; 
                return;
            }
            
            // ズーム中(scale > 1.05)や、2本指以上(ピンチ等)の場合はページ送りを無効化（ズーム・パン操作を優先）
            if ((window.visualViewport && window.visualViewport.scale > 1.05) || event.touches.length > 1) {
                touchstartX = -1;
                return;
            }

            touchstartX = event.changedTouches[0].clientX;
            touchstartY = event.changedTouches[0].clientY;
        }, {capture: true, passive: true});

        parentDoc.addEventListener('touchend', function(event) {
            if (touchstartX === -1) return;
            touchendX = event.changedTouches[0].clientX;
            touchendY = event.changedTouches[0].clientY;
            
            const diffX = touchendX - touchstartX;
            const diffY = Math.abs(touchendY - touchstartY);
            
            // 上下スクロール成分が大きい場合はスワイプとみなさない (縦移動ガード)
            if (diffY > Math.abs(diffX)) return;

            // 全ボタンから「次へ ▶」「◀ 前へ」をテキストで検索
            const btns = Array.from(parentDoc.querySelectorAll('button'));

            // 左スワイプで次へ (▶) - しきい値を少し下げて感度を良くする
            if (diffX < -30) {
                const b = btns.find(btn => btn.innerText.includes('次へ ▶'));
                if (b) b.click();
            }
            // 右スワイプで前へ (◀)
            else if (diffX > 30) {
                const b = btns.find(btn => btn.innerText.includes('◀ 前へ'));
                if (b) b.click();
            }
        }, {capture: true, passive: true});
    }
    </script>
    """
    components.html(swipe_js, height=0, width=0)


