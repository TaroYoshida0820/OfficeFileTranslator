"""
estimate.py

実際にAPIを呼ぶ前に、対象ファイルのユニット数・文字数・概算コストを確認するための
「見積もり専用モード」。課金は一切発生しない(ネットワーク通信自体を行わない)。

使い方:
    python estimate.py "input.docx"
"""

import argparse
import zipfile
import tempfile
import shutil
import os

from translate_pipeline import extract_units

# 2026年8月時点の claude-sonnet-4-6 の公開料金(百万トークンあたり)
# ※料金は変更される可能性があるため、最新値は Anthropic公式サイトで要確認
PRICE_PER_MTOK_INPUT = 3.00
PRICE_PER_MTOK_OUTPUT = 15.00


def rough_token_estimate(char_count, is_japanese=True):
    """
    簡易換算。日本語は1トークンあたり平均約1.5〜2文字、英語は1トークンあたり平均約4文字
    (実際のトークナイザーとは誤差があるため、あくまで概算)。
    """
    if is_japanese:
        return char_count / 1.7
    return char_count / 4.0


def main():
    parser = argparse.ArgumentParser(description="翻訳実行前のユニット数・概算コスト見積もり(無料・通信なし)")
    parser.add_argument("input", help="見積もり対象の .docx")
    args = parser.parse_args()

    work_dir = tempfile.mkdtemp(prefix="docx_estimate_")
    try:
        with zipfile.ZipFile(args.input, 'r') as zin:
            zin.extractall(work_dir)
        doc_xml_path = os.path.join(work_dir, 'word', 'document.xml')

        tree, units = extract_units(doc_xml_path)
        total_chars = sum(len(u["text"]) for u in units)
        paragraph_count = len(set(u["paragraph_index"] for u in units))

        input_tokens = rough_token_estimate(total_chars, is_japanese=True)
        # プロンプトの指示文・用語集ヒント等のオーバーヘッドを1呼び出しあたり概算+300トークンとして加算
        input_tokens += paragraph_count * 300
        # 出力(訳文)は原文よりやや短くなる傾向。同程度の文字数の英語と仮定
        output_tokens = rough_token_estimate(total_chars, is_japanese=False)

        cost_input = (input_tokens / 1_000_000) * PRICE_PER_MTOK_INPUT
        cost_output = (output_tokens / 1_000_000) * PRICE_PER_MTOK_OUTPUT
        total_cost = cost_input + cost_output

        print(f"ファイル: {args.input}")
        print(f"  翻訳対象ユニット数: {len(units)}")
        print(f"  API呼び出し回数(段落単位バッチ): 約{paragraph_count}回")
        print(f"  原文の総文字数: {total_chars:,}")
        print(f"  概算トークン数: 入力 約{input_tokens:,.0f} / 出力 約{output_tokens:,.0f}")
        print(f"  概算コスト: 約${total_cost:.2f} (入力${cost_input:.2f} + 出力${cost_output:.2f})")
        print("  ※簡易換算のため、実際の課金額とは誤差があります(目安として利用してください)")

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()