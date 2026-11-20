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

`MF_COLUMNS` は MF が配布する**インポート用サンプルフォーマット** (27 列) に
一致させている (`tests/fixtures/mf-journal-sample.csv` が実物)。MF の仕訳帳
**エクスポート** (19 列) とは別物で、エクスポート側には借方税額/貸方税額が無く、
メモ列の名前も「メモ」(インポートは「仕訳メモ」) と違う。取り込み時の列自動
マッピングのおかげでエクスポート形式でも取り込めてしまうが、**名前の違う列は
落ちる**。

取引No は必ず採番する。MF は「取引No が同一・取引日が同一・貸借の合計が一致」の
3 条件で複数行を 1 つの複合仕訳にまとめるため、空欄のまま同じ取引日の仕訳を
並べると別々の取引がマージされうる。会計年度で使用済みの番号は MF 側が
「最大取引No+1」に振り直す (実測: 衝突しても取引ごとに別々の番号が振られ、
グループは保たれた) ので、ファイル内で一意でありさえすればよい。

## Wise 取引履歴の取り込み (`wise`)

Wise の全取引エクスポート (`transaction-history.csv`) を仕訳に変換する。
レシートとは**入口が完全に別**で、共有するのは MF への出口と借方ホワイトリスト
だけ。Wise は構造化 CSV なので VL モデルの抽出も検算も通らない。

```sh
receipt-ledger wise preview transaction-history.csv   # 表示のみ (書き込まない)
receipt-ledger wise import  transaction-history.csv   # csv/wise-YYYY-MM.csv へ追記
```

出力はレシートとは別ファイル (`wise-YYYY-MM.csv`) にする。MF への取込単位と人の
確認単位を分けたいのと、`dedup.py` が `receipts-*.csv` を読むので混ぜると
レシートの重複判定を汚すため。

**MF は同じ内容を二度取り込んでも弾かない** (「インポートするたびに新しい仕訳が
登録されます」)。実測でも同一 CSV の 2 回取込で 4 取引になった。防止はこちら側の
責務で、Wise の取引 ID (`CARD_TRANSACTION-…`) を仕訳メモ列に残し、生成済みの
`wise-*.csv` を台帳として `wise import` が自動で除外する。**MF への取り込みは
CSV 1 本につき 1 回だけにすること。**

仕訳の形 (実際の帳簿の型に合わせた複合仕訳):

```
借方1  費用科目 / 補助科目      = Source amount (after fees)
借方2  支払手数料 / Wise        = Source fee amount
貸方   預け金 / Wise            = 上記の合計   ← 1 行目にのみ記載
```

- **為替は Wise の実レートを使う**。円残高から決済時に都度両替する運用なので
  円額は Wise 側で確定済み。Frankfurter は通らないし、外貨残高を持たない限り
  為替差損益も発生しない。摘要ではなくメモに `CAD 70.00 @114.29 (Wise実レート)`
  と根拠を残す。
- 借方科目を**無印で確定するのは `[[wise.merchants]]` に登録した取引先だけ**。
  共通の `accounts` ホワイトリストにも掛けるが、ヒントは日本語のレジ印字向けに
  調整したもので英語の取引先名に対しては根拠が弱いため、ヒットしても科目を
  提案するだけで「要確認」を付ける (実測: 同じヒント語を 2 科目が持っていると
  同点タイが定義順で解決され、無関係な科目に化けた)。どれも当たらなければ
  フォールバック科目 + 「要確認」。
- 税区分・インボイス列は空で出し、MF の科目デフォルトに委ねる (既存の手入力に
  合わせた)。
- 次の行は仕訳にせず「要手動確認」に回す:
  - `Direction=IN` — チャージ (`Category=Money added` か送金) は銀行明細側で
    計上済みなので二重計上になる。それ以外の入金は種別が未確認なので要判断
  - `Status != COMPLETED` — 金額 0 なら与信取消 (仕訳不要)。金額があれば返金の
    可能性があるので要判断
  - `Source currency != JPY` — 外貨残高からの決済。円額が確定しない
  - 費用本体が 0 円、または負の金額

> **注意**: Wise カードで払った対面決済のレシートを `inbox/` に入れると、
> レシート側 (費用/短期借入金) と Wise 側 (費用/預け金) で**二重計上**になる。
> 現行の `dedup.py` はレート差で円額が一致せず店名表記も違うため検出できない。
> Wise カード決済のレシートは証憑として保管するだけで inbox に入れないこと。

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
  wise/
    parse.py    Wise 取引履歴 CSV の読み取り (仕訳にする/しないの選別)
    journal.py  Wise 取引 → MF 複合仕訳 (借方 費用+手数料 / 貸方 預け金)
    ledger.py   取り込み済み取引 ID の台帳 (生成済み wise-*.csv を読む)
    importer.py wise-YYYY-MM.csv への追記
    report.py   仕訳案の人向け表示
nix/module.nix  NixOS module (systemd path/timer/tmpfiles)
```
