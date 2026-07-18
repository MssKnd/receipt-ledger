# receipt-ledger

レシート・領収書の画像から、[MF クラウド会計](https://biz.moneyforward.com/accounting/)の
**仕訳帳インポート形式** の月次 CSV を生成する経費精算パイプライン。

監視対象ディレクトリ (例: NAS/SMB 共有内の `inbox/`) に画像を置くと、
ローカル LLM (Ollama の Vision-Language モデル) が中身を読み取り、勘定科目を推定し、
外貨 (カナダドル等) のレシートは取引日の為替で日本円に換算して CSV に追記する。

NixOS module を同梱しており、systemd path unit で inbox を監視して自動処理できる。
ホスト名・勘定科目・貸方・通知先などの環境固有の値は、この module を import する
側 (自分の NixOS 構成) の設定として注入する。

## しくみ

```
iPhone/PC ──SMB──▶ inbox/
                     │  systemd path unit (inotify) + フォールバック timer
                     ▼
             receipt-ledger scan
                     │  ① 画像正規化 (HEIC/PDF→JPEG)
                     │  ② Ollama VL モデルで構造化抽出 (レシート配列)
                     │  ③ 検算 (明細合計≈総額) / 確信度チェック
                     │  ④ CAD→JPY 換算 (Frankfurter API, 取引日レート)
                     │  ⑤ 借方科目推定 (ホワイトリスト) + 税区分
                     │  ⑥ 重複チェック → 月次 CSV へ追記
                     ▼
             csv/receipts-YYYY-MM.csv
```

処理の結果はディレクトリで表現される (置き場所がそのまま状態):

| ディレクトリ | 意味 |
|---|---|
| `inbox/` | 未処理。Ollama/為替が一時的に不達なら残って次回リトライ |
| `processed/YYYY/` | 正常に CSV へ追記済み |
| `failed/` | 抽出不可・検算NG・低確信。CSV には書かれない |
| `review/` | 重複疑い。良い行は CSV へ入れつつ画像は人手確認へ |
| `csv/` | 生成された月次 CSV。MF へ取り込む |

## 主な仕様

- **1 画像に複数レシート**があれば、それぞれ別の仕訳行に展開する。
- 仕訳は法人単一仕訳: 借方 = 費用科目 / 貸方 = **設定で固定した勘定科目**
  (例: 立替を役員から借りた形にする「短期借入金」+ 補助科目に立替者名)。
- 借方勘定科目は設定ファイルの**ホワイトリスト**から選ぶ。判定できなければ
  フォールバック科目 (雑費) + 摘要に「要確認」。
- 税区分: 国外 (外貨) は「対象外」、国内は税率表記から 10% / 軽減 8%。
- 為替は **Frankfurter API** (ECB 公表レート)。取引日レートを使い、土日祝は
  Frankfurter が直前営業日にフォールバック。摘要に `CAD 12.34 @107.5 (2024-05-10)`
  のように換算根拠を残す。取得済みレートはローカルにキャッシュ。
- 重複 (同じ 日付・金額・店名) は当月+前月 CSV と照合し `review/` に隔離。

> **注意 (未確定)**: MF 仕訳帳インポート CSV の正確な列順・ヘッダ名は、実物の
> エクスポートで確定してから `receipt_ledger/csvwriter.py` の `MF_COLUMNS` を
> 合わせること。現状は MF が公開する代表的な列を暫定採用している。

## 使い方

### 開発

```sh
nix develop            # pytest / ruff 入りのシェル
pytest -q
```

### 単体実行 (デバッグ)

```sh
export RECEIPT_LEDGER_CONFIG=./config/accounts.example.toml
export RECEIPT_LEDGER_BASE=/tmp/receipts   # 試すとき
receipt-ledger extract path/to/receipt.jpg   # 抽出 JSON を表示
receipt-ledger process path/to/receipt.jpg   # 1 ファイル処理
receipt-ledger scan                          # inbox を 1 巡
```

### NixOS へのデプロイ

自分の flake の input に追加して module を有効化する:

```nix
# flake.nix
inputs.receipt-ledger.url = "github:MssKnd/receipt-ledger";

# 自分のホスト構成 (configuration.nix 等)
imports = [ inputs.receipt-ledger.nixosModules.default ];

services.receipt-ledger = {
  enable = true;
  user = "youruser";               # inbox/CSV を所有するユーザ (SMB 共有と揃える)
  creditSubAccount = "立替者名";    # 貸方補助科目 (立替者)
  accounts = [
    { account = "会議費"; hints = [ "カフェ" "コーヒー" "食事" ]; }
    { account = "新聞図書費"; hints = [ "書店" "書籍" ]; }
    # ...
  ];
  settings = {
    ollama_model = "qwen2.5vl:7b";   # Ollama に pull 済みの VL モデル
    ollama_base_url = "http://127.0.0.1:11435";  # ローカルの Ollama エンドポイント
  };
  # ntfyUrlFile = config.age.secrets.receipt-ntfy-url.path;  # 通知 (任意)
};
```

環境固有の設定 (ホスト名・勘定科目ホワイトリスト・貸方・通知先など) と秘密情報は、
この module を import する側で管理する。コードやこのリポジトリには置かない。

## 構成

```
receipt_ledger/
  models.py     Ollama に抽出させる構造化スキーマ (pydantic)
  config.py     TOML + 環境変数から設定を読む
  images.py     HEIC/PDF → JPEG 正規化
  extract.py    Ollama /api/chat 呼び出し (structured output)
  fx.py         Frankfurter で取引日レート取得 + キャッシュ
  validate.py   検算・確信度チェック
  classify.py   借方科目 (ホワイトリスト) + 税区分
  dedup.py      重複レシート検出
  csvwriter.py  MF 仕訳帳インポート CSV 生成 (月次追記)
  pipeline.py   オーケストレーション (状態機械)
  cli.py        エントリポイント
nix/module.nix  NixOS module (systemd path/timer/tmpfiles)
```
