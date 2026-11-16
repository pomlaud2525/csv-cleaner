#!/usr/bin/env python3
"""顧客リストCSVの整形ツール

表記ゆれ・重複・欠損のある顧客リストCSVを読み、正規化したCSVと
「変更点の一覧」を出力する。Python標準ライブラリのみで動作する。

使い方:
    python3 clean_customers.py 入力.csv [-o 整形済み.csv] [-c 変更一覧.md]

出力ファイル名を省略すると、入力ファイルと同じフォルダに
「入力名_clean.csv」「入力名_changes.md」を作る。
"""

import argparse
import csv
import datetime
import os
import re
import sys
import unicodedata

EXPECTED_COLUMNS = ["氏名", "会社名", "電話番号", "メールアドレス", "登録日", "住所"]

# 全角数字・全角ハイフンなどを半角にするための変換表（住所用）
_ZENKAKU_MAP = str.maketrans(
    "０１２３４５６７８９－",
    "0123456789-",
)


def _strip(value: str) -> str:
    """前後の空白（半角スペース・タブ・全角スペース）を取り除く。"""
    return value.strip(" \t　")


def _collapse_spaces(value: str) -> str:
    """連続する空白（半角/全角混在）を半角スペース1つにまとめる。"""
    return re.sub(r"[ 　]+", " ", value)


def normalize_name(value: str) -> str:
    """氏名: 全角スペースを半角に統一し、空白を1つにまとめる。"""
    return _collapse_spaces(_strip(value).replace("　", " "))


def normalize_company(value: str) -> str:
    """会社名: NFKC正規化のうえ、(株)/（株）/㈱ を「株式会社」に統一する。

    前株・後株の位置は変えない（「株式会社○○」と「○○株式会社」は別表記のまま）。
    英字は半角化されるが、大文字・小文字は正式表記を尊重して変えない。
    """
    v = unicodedata.normalize("NFKC", _strip(value))
    v = v.replace("㈱", "株式会社").replace("(株)", "株式会社").replace("（株）", "株式会社")
    v = v.replace("㈲", "有限会社").replace("(有)", "有限会社").replace("（有）", "有限会社")
    return _collapse_spaces(v)


def normalize_phone(value: str):
    """電話番号: 数字だけを取り出し、桁数に応じたハイフン区切りに整える。

    戻り値は (整形後の文字列, 判定不能ならTrue)。
    +81 表記は先頭の 0 に戻す。全角数字・全角ハイフンは半角化する。
    """
    v = unicodedata.normalize("NFKC", _strip(value))
    if not v:
        return "", False
    has_plus = v.startswith("+")
    digits = re.sub(r"\D", "", v)
    # 国際表記 +81 / 81始まり（市外局番の0を落とした形）を国内表記に戻す
    if has_plus and digits.startswith("81"):
        digits = "0" + digits[2:]
    elif digits.startswith("81") and len(digits) in (11, 12):
        digits = "0" + digits[2:]
    if not digits.startswith("0") or len(digits) not in (10, 11):
        # 判定できない形式は数字のみで返し、変更一覧で注記する
        return digits, True
    if len(digits) == 11:
        # 携帯電話・IP電話など: 3-4-4
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}", False
    # 10桁
    if digits[:2] in ("03", "06"):
        return f"{digits[:2]}-{digits[2:6]}-{digits[6:]}", False
    if digits[:4] in ("0120", "0800"):
        # フリーダイヤル: 4-3-3
        return f"{digits[:4]}-{digits[4:7]}-{digits[7:]}", False
    # それ以外の10桁は市外局番3桁と仮定して 3-3-4（限界はREADMEに明記）
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}", False


def normalize_email(value: str) -> str:
    """メールアドレス: 前後の空白を取り除き、小文字に統一する。"""
    return unicodedata.normalize("NFKC", _strip(value)).lower()


_DATE_PATTERNS = [
    re.compile(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})$"),
    re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$"),
]


def normalize_date(value: str):
    """登録日: YYYY-MM-DD に統一する。戻り値は (文字列, 解釈不能ならTrue)。"""
    v = unicodedata.normalize("NFKC", _strip(value))
    if not v:
        return "", False
    for pattern in _DATE_PATTERNS:
        m = pattern.match(v)
        if m:
            year, month, day = (int(x) for x in m.groups())
            try:
                return datetime.date(year, month, day).isoformat(), False
            except ValueError:
                return v, True
    return v, True


def normalize_address(value: str) -> str:
    """住所: 全角数字・全角ハイフンを半角化し、空白を整理する。"""
    v = _strip(value).translate(_ZENKAKU_MAP)
    return _collapse_spaces(v.replace("　", " "))


NORMALIZERS = {
    "氏名": lambda v: (normalize_name(v), False),
    "会社名": lambda v: (normalize_company(v), False),
    "電話番号": normalize_phone,
    "メールアドレス": lambda v: (normalize_email(v), False),
    "登録日": normalize_date,
    "住所": lambda v: (normalize_address(v), False),
}

# 値が変わらなかったフィールドでも「要確認」として一覧に挙げる理由
FIELD_REASONS = {
    "氏名": "表記ゆれの正規化（空白の統一）",
    "会社名": "会社名の表記統一",
    "電話番号": "電話番号の正規化",
    "メールアドレス": "メールアドレスの正規化（小文字化・空白除去）",
    "登録日": "登録日の形式統一（YYYY-MM-DD）",
    "住所": "住所の表記ゆれの正規化（全角→半角）",
}

# 欠損をチェックする項目（空欄でも埋めず、一覧に「欠損」として挙げる）
REQUIRED_FIELDS = ["氏名", "電話番号", "メールアドレス"]


class ChangeLog:
    def __init__(self):
        self.entries = []  # (入力行番号, 項目, 変更前, 変更後, 理由)

    def add(self, row_no, field, before, after, reason):
        self.entries.append((row_no, field, before, after, reason))

    def __len__(self):
        return len(self.entries)


def fail(message: str) -> None:
    print(f"エラー: {message}", file=sys.stderr)
    sys.exit(1)


def read_input(path: str):
    """入力CSVを読み、(ヘッダー, [(行番号, 行dict), ...]) を返す。"""
    if not os.path.isfile(path):
        fail(f"入力ファイルが見つかりません: {path}")
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            content = f.read()
    except OSError as e:
        fail(f"入力ファイルを開けませんでした: {e}")
    if not content.strip():
        fail("入力ファイルが空です。ヘッダー行のある顧客リストCSVを指定してください。")
    rows = list(csv.reader(content.splitlines()))
    header = [_strip(h) for h in rows[0]] if rows else []
    missing = [c for c in EXPECTED_COLUMNS if c not in header]
    if missing:
        fail(
            "ヘッダーが想定と違います。不足している列: "
            + "、".join(missing)
            + f"。想定する列は {'、'.join(EXPECTED_COLUMNS)} です"
            + f"（実際のヘッダー: {'、'.join(header) if header else 'なし'}）。"
        )
    records = []
    for i, raw in enumerate(rows[1:], start=2):  # ヘッダーが1行目
        records.append((i, raw))
    return header, records


def row_to_dict(header, raw, row_no, log: ChangeLog):
    """列数が合わない行は、不足分を空欄で補い、余分は切り捨てて記録する。"""
    if len(raw) < len(header):
        log.add(row_no, "行全体", f"{len(raw)}列", f"{len(header)}列",
                "列数が不足しているため、不足分を空欄として読み込んだ")
        raw = raw + [""] * (len(header) - len(raw))
    elif len(raw) > len(header):
        log.add(row_no, "行全体", f"{len(raw)}列", f"{len(header)}列",
                "列数が多すぎるため、余分な列を切り捨てて読み込んだ")
        raw = raw[: len(header)]
    return {col: raw[header.index(col)] for col in EXPECTED_COLUMNS}


def clean_records(header, records):
    """正規化と重複統合を行い、(整形済み行のリスト, ChangeLog) を返す。"""
    log = ChangeLog()
    normalized = []  # (行番号, dict)
    for row_no, raw in records:
        if all(not _strip(c) for c in raw):
            log.add(row_no, "行全体", "（空行）", "（出力しない）", "空行のため除外")
            continue
        original = row_to_dict(header, raw, row_no, log)
        cleaned = {}
        for col in EXPECTED_COLUMNS:
            before = original[col]
            after, suspicious = NORMALIZERS[col](before)
            cleaned[col] = after
            if after != before:
                log.add(row_no, col, before, after, FIELD_REASONS[col])
            elif suspicious:
                log.add(row_no, col, before, before,
                        "形式を判定できなかったため、そのまま出力（要確認）")
            if col in REQUIRED_FIELDS and not after:
                log.add(row_no, col, "（空欄）", "（空欄）",
                        f"欠損: {col}が入力されていない（推測では埋めない）")
            if col == "登録日" and not after:
                log.add(row_no, col, "（空欄）", "（空欄）",
                        "欠損: 登録日が入力されていない（推測では埋めない）")
        normalized.append((row_no, cleaned))

    # 重複統合: 「メールアドレス一致」または「電話番号＋氏名一致」を同一人物とする。
    # 先に現れた行を代表とし、代表の空欄は後続行の値で補う。
    kept = []  # (代表の入力行番号, dict)
    by_email = {}
    by_phone_name = {}
    merged_count = 0
    for row_no, row in normalized:
        target = None
        reason = ""
        email = row["メールアドレス"]
        phone_name = (row["電話番号"], row["氏名"])
        if email and email in by_email:
            target = by_email[email]
            reason = "重複統合（メールアドレスが一致）"
        elif row["電話番号"] and phone_name in by_phone_name:
            target = by_phone_name[phone_name]
            reason = "重複統合（電話番号と氏名が一致）"
        if target is None:
            kept.append((row_no, row))
            if email:
                by_email[email] = row
            if row["電話番号"]:
                by_phone_name[phone_name] = row
            continue
        merged_count += 1
        rep_no = next(n for n, r in kept if r is target)
        for col in EXPECTED_COLUMNS:
            if not target[col] and row[col]:
                target[col] = row[col]
                log.add(row_no, col, "（代表行では空欄）", row[col],
                        f"重複統合の際、{row_no}行目の値で代表行（{rep_no}行目）の空欄を補った")
        log.add(row_no, "行全体", f"{row_no}行目: {row['氏名']}",
                f"{rep_no}行目に統合", reason)
        if email and email not in by_email:
            by_email[email] = target
        if row["電話番号"]:
            key = (row["電話番号"], target["氏名"])
            by_phone_name.setdefault(key, target)
    return [row for _, row in kept], log, merged_count


def write_clean_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=EXPECTED_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _md_cell(value: str) -> str:
    return value.replace("|", "｜").replace("\n", " ")


def write_changes_md(path, log: ChangeLog, in_count, out_count, merged_count):
    lines = [
        "# 変更点の一覧",
        "",
        f"- 入力: {in_count}行 → 出力: {out_count}行（重複統合で {merged_count}行 を統合）",
        f"- 変更点: {len(log)}件",
        "",
        "| 入力行 | 項目 | 変更前 | 変更後 | 理由 |",
        "|---:|---|---|---|---|",
    ]
    for row_no, field, before, after, reason in log.entries:
        lines.append(
            f"| {row_no} | {_md_cell(field)} | {_md_cell(before)} | "
            f"{_md_cell(after)} | {_md_cell(reason)} |"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="顧客リストCSVの整形ツール")
    parser.add_argument("input", help="入力CSVファイル")
    parser.add_argument("-o", "--output", help="整形済みCSVの出力先")
    parser.add_argument("-c", "--changes", help="変更点の一覧（Markdown）の出力先")
    args = parser.parse_args(argv)

    header, records = read_input(args.input)
    cleaned, log, merged_count = clean_records(header, records)

    base, _ = os.path.splitext(args.input)
    out_csv = args.output or f"{base}_clean.csv"
    out_md = args.changes or f"{base}_changes.md"
    try:
        write_clean_csv(out_csv, cleaned)
        write_changes_md(out_md, log, len(records), len(cleaned), merged_count)
    except OSError as e:
        fail(f"出力ファイルに書き込めませんでした: {e}")

    print(f"入力: {len(records)}行")
    print(f"出力: {len(cleaned)}行（重複統合で {merged_count}行 を統合）")
    print(f"変更点: {len(log)}件")
    print(f"整形済みCSV: {out_csv}")
    print(f"変更点の一覧: {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
