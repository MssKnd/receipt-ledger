"""Wise の取引履歴を MF の仕訳に変換する経路。

レシート経路とは入口が完全に別で、共有するのは MF への出口 (csvwriter) と
勘定科目ホワイトリスト (classify) だけ。Wise は構造化 CSV なので VL モデルに
よる抽出・検算・Frankfurter 換算はいずれも通らない。
"""
