# 利用ガイド & 操作方法

本ドキュメントでは、LangGraph + Python + Ollama エージェントの起動方法、対話インターフェースの使い方、各種コマンドについて解説します。

---

## 1. 前提条件と環境セットアップ

### 前提要件
- **Python**: 3.11 以上 (Python 3.12 推奨)
- **uv**: パッケージマネージャ
- **Ollama**: ローカルで起動していること (`ollama serve`)

### セットアップ手順

```bash
cd /Users/mauda/Projects/langgraph_sample

# 依存関係のインストール
uv sync

# 設定ファイルの確認 (必要に応じて .env を編集)
cat .env
```

`.env` 設定例:
```ini
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3.5:9b-mlx
TEMPERATURE=0.2
THREAD_ID=default-session
MAX_CONTEXT_TOKENS=12000
RECURSION_LIMIT=15
MAX_TOOL_CALLS=8
AGENT_TIMEOUT_SECONDS=120
OLLAMA_REQUEST_TIMEOUT_SECONDS=60
OLLAMA_HEALTH_TIMEOUT_SECONDS=3
API_HOST=127.0.0.1
API_PORT=8000
API_MAX_MESSAGE_CHARS=20000
WEB_API_BASE_URL=http://127.0.0.1:8000
WEB_API_TIMEOUT_SECONDS=180
IDEMPOTENCY_TTL_SECONDS=3600
IDEMPOTENCY_MAX_ENTRIES=1000
DATABASE_URL=postgresql+asyncpg://langgraph:パスワード@192.168.100.2:5432/langgraph
CHECKPOINT_DATABASE_URL=postgresql://langgraph:パスワード@192.168.100.2:5432/langgraph
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=10
EXECUTION_LEASE_SECONDS=300
CONVERSATION_RETENTION_DAYS=90
```

---

## 2. リッチCLIでの対話 (`src/cli.py`)

ターミナル上で非同期に配信される回答・ツール実行イベントを視覚的に確認しながら対話できます。

```bash
uv run python -m src.cli
```

### 画面イメージ & 機能
- 起動時に現在のモデル、Base URL、スレッドID、利用可能なツール一覧がバナー表示されます。
- AIが思考・ツール呼び出しを行うと、スピナーと同時にツール名・引数・実行結果パネルが表示されます。

### サポートされているスラッシュコマンド

| コマンド | 説明 |
|---|---|
| `/reset` または `/clear` | 新しいスレッドIDを生成し、会話履歴をリセットします |
| `/session <ID>` | 指定したセッションへ切り替え、過去の対話を再開します |
| `/history` | 現在のセッションの会話履歴を表示します |
| `/notes` | 保存されているメモを一覧表示します |
| `/model <モデル名>` | 使用するOllamaモデルを動的に切り替えます (例: `/model gemma4:12b-mlx`) |
| `/help` | コマンド一覧と使い方ヘルプを表示します |
| `/exit` または `/quit` | アプリケーションを終了します |

---

## 3. Streamlit Web UIでの対話 (`src/web_app.py`)

ブラウザ上で利用できるAPIクライアント型チャットUIです。StreamlitはLangGraphやデータベースへ直接接続しないため、先にFastAPIを起動します。

`AUTH_MODE=oidc`では、Keycloakに`langgraph-streamlit`機密クライアントを作成し、次の設定例をコピーしてクライアントシークレットを設定します。

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

`.streamlit/secrets.toml`はGit対象外です。リダイレクトURIは`http://localhost:8501/oauth2callback`、`expose_tokens`は`access`を指定します。

```bash
uv run streamlit run src/web_app.py
```

ブラウザで `http://localhost:8501` にアクセスします。

### Web UIの機能
1. **チャット対話**:
   - SSEによる回答差分を逐次表示します。
   - EnterはIME確定・改行に使用し、右側の送信ボタンまたはCtrl／Command+Enterで送信します。
   - 回答生成中は専用の停止ボタンを表示し、SSEハートビートを利用してAPIの実行も速やかにキャンセルします。
   - ツール実行はツール名と開始・完了だけを表示し、引数・出力は表示しません。
2. **会話管理**:
   - 新規作成、一覧選択、名前変更、アーカイブ・再開、削除ができます。
   - 同名会話でも、更新日時と短縮IDを使って区別できます。
   - URLの会話IDを初期選択として優先し、選択変更時はURLも同期します。
   - 最初の質問から会話名を自動設定します。会話が0件の場合は、明示的に「新しい会話」を押したときだけ作成します。
   - ブラウザ再読み込み後も、選択中の会話IDを使ってAPIから履歴を復元します。
   - APIへ保存された会話単位のメモを参照できます。
3. **接続・認証状態**:
   - API、Ollama、PostgreSQLの状態を表示します。
   - `AUTH_MODE=oidc`ではKeycloakのログイン・ログアウトを提供し、アクセストークンをAPI呼び出しだけに使用します。
   - モデルとTemperatureはAPIサーバー側の`.env`で管理します。

---

## 4. Web・モバイル向けAPIの起動 (`src/api/app.py`)

初回またはマイグレーション追加後に、アプリ所有テーブルを更新します。

```bash
uv run alembic upgrade head
```

```bash
uv run uvicorn src.api.app:app --host 127.0.0.1 --port 8000
```

起動後、`http://127.0.0.1:8000/docs`でOpenAPIの仕様と試行画面を確認できます。詳しいリクエスト、SSEイベント、エラー処理は[API利用ガイド](api_guide.md)を参照してください。

CLIはローカル互換用SQLiteを引き続き利用できます。Streamlit Web UIはFastAPI経由でPostgreSQL上の会話・履歴・メモを利用します。

SQLiteからの移行対象確認と実行：

```bash
uv run python -m src.db.migrate_sqlite
uv run python -m src.db.migrate_sqlite --apply
```

保存期限切れ会話の削除：

```bash
uv run python -m src.db.cleanup --limit 100
```

## 5. 単体テストの実行

```bash
uv run pytest
```

全71件のテストケース（ツール単体動作、安全性、同期・非同期グラフ、共通サービス、API、OIDC、SSE、会話・メモCRUD、冪等性、レート制限、Streamlit用APIクライアント、会話選択、IME安全な明示送信、ストリーミング停止、Web UIスモークテスト）が実行されます。
