"""
docx日英翻訳パイプライン - ラン結合・抽出・再挿入モジュール

設計方針:
- 同一段落内で隣接し、かつ w:rPr (書式) が完全一致するランのみを1グループに結合する
- ハイパーリンク内のランは常に独立したグループ境界として扱う(書式が同じでも結合しない)
- 各グループに一意ID (P{段落連番}-U{グループ連番}) を付与
- 抽出はテキストのみ。再挿入時は最初のランの w:t にのみ訳文を書き込み、
  グループ内の他ランの w:t は空文字にする(rPr要素自体は削除せず温存する=構造を壊さない)
"""

from lxml import etree
import json
import zipfile
import shutil
import tempfile
import os

NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
W = NS['w']


def _rpr_signature(run):
    """ランの書式(w:rPr)をシリアライズしてグループ化キーにする。無ければNone。"""
    rpr = run.find('w:rPr', NS)
    if rpr is None:
        return None
    return etree.tostring(rpr, method='c14n')


def _run_text(run):
    """ラン内の全 w:t を連結。w:tab / w:br を含む場合は None を返して結合対象から外す。"""
    texts = []
    for child in run:
        tag = etree.QName(child).localname
        if tag == 't':
            texts.append(child.text or '')
        elif tag in ('tab', 'br', 'cr'):
            return None  # タブ・改行を含むランは結合しない(境界にする)
    if not texts:
        return None
    return ''.join(texts)


def extract_units(document_xml_path):
    """
    document.xml を解析し、(tree, 翻訳単位のリスト) を返す。
    重要: 戻り値の tree はこの後 reinsert_translations() で書き換えられ、
    save_document_xml(tree, out_path) にそのまま渡すこと。
    パスから再parseし直すと、メモリ上の編集結果が失われる(要注意ポイント)。
    """
    tree = etree.parse(document_xml_path)
    root = tree.getroot()
    body = root.find('w:body', NS)
    paragraphs = body.findall('.//w:p', NS)

    units = []

    for p_idx, p in enumerate(paragraphs):
        # 段落直下の子要素を順番に見て、run と hyperlink>run を区別しつつ走査
        children = list(p)
        current_group = None  # {sig, is_hyperlink, hyperlink_id, runs: [run_element]}

        def flush(group):
            if group is None or not group['runs']:
                return
            texts = [_run_text(r) for r in group['runs']]
            if any(t is None for t in texts):
                # tab/brを含むランが混ざっていたら結合せず1ランずつ個別出力
                for r in group['runs']:
                    t = _run_text(r)
                    if t is not None and t.strip():
                        uid = f"P{p_idx:04d}-U{len(units):04d}"
                        units.append({
                            'id': uid, 'text': t, 'paragraph_index': p_idx,
                            'run_refs': [r], 'is_hyperlink': group['is_hyperlink'],
                        })
                return
            merged_text = ''.join(texts)
            if not merged_text.strip():
                return
            uid = f"P{p_idx:04d}-U{len(units):04d}"
            units.append({
                'id': uid, 'text': merged_text, 'paragraph_index': p_idx,
                'run_refs': group['runs'], 'is_hyperlink': group['is_hyperlink'],
            })

        for child in children:
            tag = etree.QName(child).localname
            if tag == 'r':
                sig = _rpr_signature(child)
                if (current_group is not None
                        and not current_group['is_hyperlink']
                        and current_group['sig'] == sig):
                    current_group['runs'].append(child)
                else:
                    flush(current_group)
                    current_group = {'sig': sig, 'is_hyperlink': False, 'runs': [child]}
            elif tag == 'hyperlink':
                flush(current_group)
                current_group = None
                hyper_runs = child.findall('w:r', NS)
                # ハイパーリンク内部でも同一書式の隣接ランは結合してよいが、外とは絶対結合しない
                sub_group = None
                for hr in hyper_runs:
                    sig = _rpr_signature(hr)
                    if sub_group is not None and sub_group['sig'] == sig:
                        sub_group['runs'].append(hr)
                    else:
                        flush(sub_group)
                        sub_group = {'sig': sig, 'is_hyperlink': True, 'runs': [hr]}
                flush(sub_group)
            else:
                # w:pPr など。グループ境界にはしない(継続)
                continue
        flush(current_group)

    return tree, units


def build_translation_manifest(units, out_json_path):
    manifest = [{'id': u['id'], 'text': u['text'], 'paragraph_index': u['paragraph_index']}
                for u in units]
    with open(out_json_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def reinsert_translations(units, translations: dict):
    """
    units: extract_units() の戻り値(run_refs が生きたXML要素を保持している前提)
    translations: {id: 訳文} の辞書
    グループの最初のランにのみ訳文を書き込み、残りは空文字にする。
    rPr(書式)要素自体は一切削除しない = 構造を壊さない。
    """
    missing = []
    for u in units:
        if u['id'] not in translations:
            missing.append(u['id'])
            continue
        translated = translations[u['id']]
        runs = u['run_refs']
        first_t = runs[0].find('w:t', NS)
        if first_t is None:
            first_t = etree.SubElement(runs[0], f'{{{W}}}t')
        first_t.text = translated
        first_t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        for r in runs[1:]:
            t = r.find('w:t', NS)
            if t is not None:
                t.text = ''
    return missing


def save_document_xml(tree, out_path):
    """extract_units() が返した同じ tree オブジェクトを書き出す(再parse禁止)。"""
    tree.write(out_path, xml_declaration=True, encoding='UTF-8', standalone=True)


def translate_docx(input_path, output_path, translations: dict):
    """
    OS依存のunzip/zipコマンドを使わず、Pythonのzipfileだけで完結するワンストップ関数。
    PowerShellのExpand-Archive/Compress-Archiveとの相性問題(内部フォルダ順序等)を回避する。

    使い方:
        from translate_pipeline import translate_docx
        from translations_sample_ja_en import TRANSLATIONS  # 自分の訳文辞書に差し替え可
        missing = translate_docx("input.docx", "output.docx", TRANSLATIONS)
        print("訳文が無かったID:", missing)
    """
    work_dir = tempfile.mkdtemp(prefix="docx_translate_")
    try:
        # --- 展開 ---
        with zipfile.ZipFile(input_path, 'r') as zin:
            names = zin.namelist()  # 元の並び順を保持しておく(重要)
            zin.extractall(work_dir)

        doc_xml_path = os.path.join(work_dir, 'word', 'document.xml')

        # --- 抽出・再挿入 ---
        tree, units = extract_units(doc_xml_path)
        missing = reinsert_translations(units, translations)
        save_document_xml(tree, doc_xml_path)

        # --- 再圧縮(元のnamelist順を維持してZip化) ---
        if os.path.exists(output_path):
            os.remove(output_path)
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                full_path = os.path.join(work_dir, name)
                if os.path.isfile(full_path):
                    zout.write(full_path, name)

        return missing
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
