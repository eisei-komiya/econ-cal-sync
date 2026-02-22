# econ-cal-sync

> 🇺🇸 [English README is here](README-EN.md)

GitHub Actions を使って、高重要度の経済指標イベントを毎週自動的に Google カレンダーへ同期するツールです。

データソースは**プラガブル**設計で、環境変数ひとつで切り替え可能です。デフォルトのデータソース（[ForexFactory](https://www.forexfactory.com/)）は API キー不要で使えます。

---

## 概要

毎週月曜日の朝 7:00 JST（日曜 22:00 UTC）にワークフローが起動し、設定した国・通貨（デフォルト: `USD`・`JPY`）の今後 4 週間分の経済指標イベントを取得して Google カレンダーへ upsert します。`extendedProperties` を使った重複チェックにより、同じイベントを何度登録しても冪等に動作します。

### 対応データソース

| 名称                    | 環境変数 `EVENT_SOURCE`         | API キー |
|-------------------------|---------------------------------|----------|
| Forex Factory           | `forexfactory` *(デフォルト)*   | 不要     |
| Financial Modeling Prep | `fmp`                           | 必要 (`FMP_API_KEY`) |

> 新しいデータソースを追加するには `src/fetchers/` に小さなフェッチャークラスを実装するだけです。  
> → [新しいデータソースの追加方法](#新しいデータソースの追加方法)

---

## 技術スタック

| 区分               | 技術                                                                                          |
|--------------------|-----------------------------------------------------------------------------------------------|
| 言語               | Python 3.14+                                                                                  |
| パッケージマネージャ | [uv](https://docs.astral.sh/uv/)                                                             |
| CI / 自動化        | [GitHub Actions](https://docs.github.com/en/actions)                                         |
| カレンダー API     | [Google Calendar API v3](https://developers.google.com/calendar/api/guides/overview)          |
| 認証               | Google サービスアカウント（`google-auth` 使用）                                               |
| デフォルトデータソース | [ForexFactory](https://www.forexfactory.com/)（`market-calendar-tool` による HTML スクレイピング） |
| オプションデータソース | [Financial Modeling Prep API](https://financialmodelingprep.com/)                         |

---

## 自分の環境で使うには

### 1. リポジトリをフォークする

1. このリポジトリページ右上の **Fork** ボタンをクリックします。
2. 必要に応じてローカルにクローンします（以降の手順は GitHub の Web UI だけでも完結します）。

### 2. Google Cloud – サービスアカウントと Calendar API の設定

1. [Google Cloud Console](https://console.cloud.google.com/) を開きます。
2. 新しいプロジェクトを作成するか、既存のプロジェクトを選択します。
3. **Google Calendar API** を有効化します  
   （*APIs & Services → Library → 「Google Calendar API」で検索*）。
4. **サービスアカウント**を作成します  
   （*IAM & Admin → Service Accounts → Create Service Account*）。
5. サービスアカウントの JSON キーを生成してダウンロードします  
   （*Keys → Add Key → Create new key → JSON*）。

### 3. Google カレンダーをサービスアカウントと共有する

1. [Google カレンダー](https://calendar.google.com/) を開き、対象のカレンダーの設定画面へ移動します。
2. **設定 → 特定のユーザーと共有** を選択します。
3. サービスアカウントのメールアドレス（`@<project>.iam.gserviceaccount.com` で終わる形式）を追加し、ロールを **「予定の変更」（Editor）** に設定します。
4. *カレンダーを統合* 欄に表示される **カレンダー ID** を控えておきます  
   （例: `abc123@group.calendar.google.com` や Gmailアドレス）。

### 4. GitHub Secrets を設定する

フォーク先のリポジトリで **Settings → Secrets and variables → Actions** へ進み、以下のシークレットを追加します：

| シークレット名       | 値                                                          |
|----------------------|-------------------------------------------------------------|
| `GOOGLE_SA_JSON`     | ダウンロードしたサービスアカウント JSON ファイルの**全内容** |
| `GOOGLE_CALENDAR_ID` | 手順 3 で控えたカレンダー ID                                |

> **メモ:** デフォルトのデータソース（ForexFactory）は API キー不要です。  
> 別のデータソースに切り替える場合は、対応する API キーをシークレットに追加し、ワークフローの環境変数として渡してください。

### 5. GitHub Actions を有効化する

フォーク後、GitHub Actions のワークフローがデフォルトで無効になっている場合があります。  
フォーク先の **Actions** タブを開き、**「I understand my workflows, go ahead and enable them」** をクリックして有効化してください。

---

## データソースの切り替え

`.github/workflows/sync.yml` の `EVENT_SOURCE` 環境変数を変更します：

```yaml
env:
  EVENT_SOURCE: forexfactory   # 別の登録済みソース名に変更
```

---

## 手動実行

**Actions → Sync Economic Calendar → Run workflow** から、スケジュールを待たずにすぐ実行できます。

---

## カスタマイズ

`src/sync.py` の先頭付近にある定数を編集します：

```python
# 対象国の通貨コード（ForexFactory は通貨コードで国を識別します）
TARGET_COUNTRIES = {"USD", "JPY"}

# 最低重要度（1=低, 2=中, 3=高）
IMPORTANCE_MIN = 3

# 何週間先まで取得するか
FETCH_WEEKS = 4
```

新しい国を追加する場合は、`COUNTRY_FLAG` にも対応するフラグ絵文字を追加してください。

---

## 新しいデータソースの追加方法

1. `src/fetchers/my_source.py` に `BaseFetcher` を継承したクラスを作成します。
2. `name` プロパティと `fetch()` メソッドを実装し、`EconomicEvent`（`src/models.py` 定義）のリストを返すようにします。
3. `src/fetchers/__init__.py` に登録します：
   ```python
   from .my_source import MySourceFetcher
   _FETCHERS["my_source"] = MySourceFetcher
   ```
4. ワークフローで `EVENT_SOURCE=my_source` を設定します。

---

## プロジェクト構成

```
src/
├── __init__.py
├── __main__.py          # python -m src エントリポイント
├── sync.py              # メイン同期ロジック（データソース非依存）
├── models.py            # EconomicEvent データクラス
└── fetchers/
    ├── __init__.py      # フェッチャーレジストリ & get_fetcher()
    ├── base.py          # BaseFetcher 抽象基底クラス
    ├── forexfactory.py
    └── fmp.py
```
