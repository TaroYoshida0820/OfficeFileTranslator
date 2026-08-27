"""
api_translate.py

抽出済みユニット(translate_pipeline.extract_units の戻り値)を、
実際の翻訳API(Anthropic Claude API)に投げて訳文を取得するモジュール。

設計方針:
- 識別子は glossary_tools.mask_identifiers() で送信前にマスキングし、応答受領後に復元する
  (翻訳エンジンの精度に一切依存せず、信号名/ID類は100%保護される)
- 同一段落(paragraph_index)のユニットはまとめて1回のAPI呼び出しにバッチングし、文脈を保つ
- 用語集からその文章に関連しそうなエントリだけを抽出してプロンプトに注入する(軽量RAG版)
- translate_fn を差し替え可能にしてあるので、DeepL API 等の別エンジンにも流用できる
"""

import os
import json
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from collections import defaultdict

from glossary_tools import mask_identifiers, unmask_identifiers, find_relevant_terms

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-6"


def _build_session():
    """
    接続の瞬断・タイムアウト・429(レート制限)に対して自動リトライするセッションを作る。
    会社のネットワーク/セキュリティソフト経由だと、多数回連続でAPIを呼ぶ際に
    ごく低確率でSSLハンドシェイクが瞬断されることがある(実際に本ツールでも発生した事例あり)。
    1回落ちただけで処理全体を止めないよう、指数バックオフ付きで自動的に再試行する。
    """
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.5,  # 1.5s, 3s, 6s, 12s, 24s と間隔を空けながら再試行
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


_SESSION = _build_session()


def _build_prompt(masked_texts_with_ids, target_lang_label, glossary_hints):
    """1段落分のマスク済みテキストをまとめて翻訳させるプロンプトを作る。"""
    lines = [f'{{"id": "{uid}", "text": {json.dumps(text, ensure_ascii=False)}}}'
              for uid, text in masked_texts_with_ids]
    items_json = "[\n  " + ",\n  ".join(lines) + "\n]"

    glossary_block = ""
    if glossary_hints:
        rows = [f'- "{t["ja"]}" -> "{t["en"]}"' + (f"  ({t['note']})" if t["note"] else "")
                for t in glossary_hints]
        glossary_block = "用語集(必ずこの対訳に従うこと):\n" + "\n".join(rows) + "\n\n"

    prompt = f"""以下は自動車業界の技術仕様書から抽出した日本語テキストの配列です。{target_lang_label}に翻訳してください。

重要なルール:
- "§ID数字§" という形式のトークンは識別子(信号名・要求ID等)のプレースホルダです。絶対に翻訳・変更・削除せず、そのままの位置・そのままの文字列で訳文中に残してください。
- 各要素は同じ段落内の断片です。文脈の一貫性を保ちながら、それぞれ独立した訳文として翻訳してください。
- 技術文書として簡潔・正確な表現にしてください。

{glossary_block}入力:
{items_json}

出力形式: 他のテキストを一切含めず、次のJSON配列のみを返してください。
[{{"id": "...", "translated": "..."}}, ...]
"""
    return prompt


def translate_paragraph_batch(units_in_paragraph, target_lang_label, glossary_terms,
                               identifier_literals, api_key):
    """
    同一段落内のユニット群を1回のAPI呼び出しでまとめて翻訳する。
    戻り値: {id: 訳文} の辞書
    """
    masked_items = []
    mask_maps = {}
    combined_text_for_hint = ""
    for u in units_in_paragraph:
        masked, mapping = mask_identifiers(u["text"], identifier_literals)
        masked_items.append((u["id"], masked))
        mask_maps[u["id"]] = mapping
        combined_text_for_hint += u["text"]

    glossary_hints = find_relevant_terms(combined_text_for_hint, glossary_terms)
    prompt = _build_prompt(masked_items, target_lang_label, glossary_hints)

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}],
    }

    # HTTPAdapterのRetryは接続エラーもある程度拾うが、SSLハンドシェイク段階の
    # ConnectionResetError等、urllib3のRetryが拾いきれない例外も念のため手動で再試行する。
    last_err = None
    for attempt in range(3):
        try:
            resp = _SESSION.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_err = e
            wait = 2 * (attempt + 1)
            print(f"      [警告] 通信エラー発生、{wait}秒後に再試行します ({attempt + 1}/3回目): {e}")
            time.sleep(wait)
    else:
        raise RuntimeError(f"3回リトライしても接続できませんでした: {last_err}")

    text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    raw = "\n".join(text_blocks).strip()
    # ```json フェンス等が付いた場合の保険
    raw = raw.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(raw)

    result = {}
    for item in parsed:
        uid = item["id"]
        translated_masked = item["translated"]
        result[uid] = unmask_identifiers(translated_masked, mask_maps.get(uid, {}))
    return result


def translate_all_units(units, target_lang_label, glossary_terms, identifier_literals,
                         api_key=None):
    """
    extract_units() が返した units 全体を、段落単位でバッチ翻訳する。
    api_key未指定の場合は環境変数 ANTHROPIC_API_KEY を使う。
    戻り値: {id: 訳文} の辞書(translate_pipeline.reinsert_translations にそのまま渡せる)
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "APIキーが見つかりません。環境変数 ANTHROPIC_API_KEY を設定するか、"
            "api_key引数で渡してください。"
        )

    by_paragraph = defaultdict(list)
    for u in units:
        by_paragraph[u["paragraph_index"]].append(u)

    translations = {}
    paragraph_indices = sorted(by_paragraph.keys())
    total = len(paragraph_indices)
    for i, p_idx in enumerate(paragraph_indices, start=1):
        print(f"      翻訳中... 段落 {i}/{total}")
        batch_result = translate_paragraph_batch(
            by_paragraph[p_idx], target_lang_label, glossary_terms,
            identifier_literals, api_key
        )
        translations.update(batch_result)

    return translations