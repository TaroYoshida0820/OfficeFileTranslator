# -*- coding: utf-8 -*-
"""
run_translation.py

④日英翻訳ツール - エンドツーエンド実行スクリプト

使い方:
    export ANTHROPIC_API_KEY="sk-ant-..."   (Windowsなら $env:ANTHROPIC_API_KEY="sk-ant-...")
    python run_translation.py input.docx output.docx --lang en --glossary AEC_翻訳用語集.xlsx

対応言語 (--lang):
    en : 英語
    zh : 簡体中文
    vi : Tiếng Việt(ベトナム語)
    ※ zh/vi は現状、用語集(AEC_翻訳用語集.xlsx)の該当列がまだ"(未着手)"のままなので、
      一般用語の対訳精度はenほど高くない。識別子保護は言語に関係なく機能する。
"""

import argparse
import zipfile
import shutil
import tempfile
import os

from translate_pipeline import extract_units, reinsert_translations, save_document_xml
from glossary_tools import load_glossary
from api_translate import translate_all_units

LANG_LABELS = {
    "en": "英語(English)",
    "zh": "簡体中文(Simplified Chinese)",
    "vi": "Tiếng Việt(ベトナム語)",
}


def main():
    parser = argparse.ArgumentParser(description="docx構造保持型 日→他言語 翻訳ツール")
    parser.add_argument("input", help="翻訳対象の .docx")
    parser.add_argument("output", help="出力先の .docx")
    parser.add_argument("--lang", default="en", choices=LANG_LABELS.keys())
    parser.add_argument("--glossary", default="AEC_翻訳用語集.xlsx")
    parser.add_argument("--api-key", default=None, help="未指定時は環境変数 ANTHROPIC_API_KEY を使用")
    args = parser.parse_args()

    print(f"[1/5] 用語集を読み込み中: {args.glossary}")
    terms, identifier_literals = load_glossary(args.glossary)
    print(f"      一般用語 {len(terms)}件 / 保護対象識別子 {len(identifier_literals)}件")

    work_dir = tempfile.mkdtemp(prefix="docx_translate_")
    try:
        print(f"[2/5] 展開中: {args.input}")
        with zipfile.ZipFile(args.input, 'r') as zin:
            names = zin.namelist()
            zin.extractall(work_dir)
        doc_xml_path = os.path.join(work_dir, 'word', 'document.xml')

        print("[3/5] テキスト抽出・ラン結合中")
        tree, units = extract_units(doc_xml_path)
        print(f"      {len(units)} 個の翻訳単位を抽出")

        print(f"[4/5] 翻訳API呼び出し中(対象言語: {LANG_LABELS[args.lang]})")
        translations = translate_all_units(
            units, LANG_LABELS[args.lang], terms, identifier_literals,
            api_key=args.api_key
        )
        missing = reinsert_translations(units, translations)
        if missing:
            print(f"      警告: {len(missing)}件は訳文が得られず原文のまま残っています: {missing[:5]}...")
        save_document_xml(tree, doc_xml_path)

        print(f"[5/5] 再構築中: {args.output}")
        if os.path.exists(args.output):
            os.remove(args.output)
        with zipfile.ZipFile(args.output, 'w', zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                full_path = os.path.join(work_dir, name)
                if os.path.isfile(full_path):
                    zout.write(full_path, name)

        print("完了。")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
