# 🤖 LangGraph + Python + Ollama 自律型エージェント

LangGraph、Python、ローカルLLM（Ollama）を組み合わせた自律型ReActエージェントアプリケーションです。
Web検索、計算機、システム日時取得、メモ管理などのツールを自律的に判断して使い分け、マルチターンでの会話履歴を保持します。

---

## 🌟 特徴

- **自律型 ReAct ループ (LangGraph)**:
  - モデルの判断によりツールを自動的に選択・実行し、その結果をもとに追加思考・回答生成を行います。
- **ローカルLLM連携 (Ollama)**:
  - `ChatOllama` / `langchain-ollama` を使用し、ローカル環境で動く軽量・高性能モデル（`qwen3.5:9b-mlx`, `qwen3.8:27b-mlx`, `gemma4:12b-mlx` など）と連携。
- **マルチターン対話メモリ**:
  - CLIは`SqliteSaver`、APIとStreamlit Web UIは`AsyncPostgresSaver`により、スレッド単位で過去の会話コンテキストを永続化。
- **共通エージェントサービス**:
  - CLIとFastAPIから共通利用できる `AgentService` を提供し、StreamlitはFastAPI経由で利用。
  - 非同期実行、全体タイムアウト、ツール呼び出し上限、安全なエラー変換に対応。
- **Web・モバイル向けAPI**:
  - FastAPIによる会話作成、通常応答、Server-Sent Events（SSE）ストリーミングを提供。
  - PostgreSQLによる会話・メモ・実行履歴・冪等性の永続化と複数APIプロセス間の同時実行制御に対応。
  - LangGraphチェックポイント用接続は貸出前に生存確認し、長時間アイドルで切断された接続を自動交換。
  - 会話一覧・更新・削除・履歴・キャンセル、メモCRUD、リクエストID、安全なエラー応答を提供。
  - Keycloak/OIDCのRS256アクセストークンを検証し、JWTの`sub`ごとに会話・メモを分離。
  - WEB・モバイルはAuthorization Code + PKCEでログイン可能。`/health`・`/ready`以外のAPIをBearer認証で保護。
  - 許可Origin限定CORS、IP・ユーザー単位の共有レート制限、機密情報を記録しないJSONアクセスログに対応。
  - 外部副作用ツールは承認実行基盤へ接続されるまで登録を拒否する、フェイルクローズのツールポリシーを提供。
- **汎用画像解析API**:
  - JPEG・PNG・WebPと任意プロンプトを、会話履歴から独立した画像対応Ollamaモデルで解析。
  - 任意の制限付きJSON SchemaによるStructured Outputs、モデル能力確認、実出力の再検証に対応。
  - MIME偽装・破損・過大画像を拒否し、EXIF除去、専用レート制限、画像・プロンプトを保存しない処理を提供。
- **Ollama状態確認**:
  - Ollamaへの接続状態とインストール済みモデルを取得し、Web UIへ表示。
- **充実のツールセット**:
  - 🌐 **Web検索 (`web_search`)**: DuckDuckGoを利用したリアルタイム最新情報取得
  - 🔢 **計算機 (`calculator`)**: 安全な数式評価・数学関数実行
  - ⏰ **日時取得 (`get_current_datetime`)**: 現在の日時・曜日・タイムゾーン（JST/UTC）情報取得
  - 📝 **メモ管理 (`save_note`, `read_notes`)**: CLIではSQLite、API・StreamlitではPostgreSQLへ会話単位の共有メモを保存・読み出し
- **3種類のインターフェース**:
  - 💻 **リッチCLI (`src/cli.py`)**: Richライブラリによるスタイリッシュな対話、ツール呼び出しプロセスの可視化
  - 🌐 **Web UI (`src/web_app.py`)**: StreamlitによるKeycloakログイン、会話管理、SSE回答表示、安全なツール実行状態表示
  - 🔌 **HTTP API (`src/api/app.py`)**: Web・モバイルアプリ向けJSON APIとSSEストリーミング

---

## 📁 プロジェクト構成

```
langgraph_sample/
├── pyproject.toml              # プロジェクト設定・依存関係 (uv)
├── .python-version             # Python 3.12 指定
├── .env.example                # 環境変数サンプル
├── .env                        # 設定ファイル (Ollama設定等)
├── .streamlit/
│   └── secrets.toml.example    # Streamlit OIDC設定例（実シークレットはGit対象外）
├── alembic.ini                 # DBマイグレーション設定
├── migrations/                # アプリ用PostgreSQLマイグレーション
├── deploy/
│   ├── postgres/compose.yaml   # PostgreSQL用Docker Compose例
│   └── keycloak/               # Dockge向けKeycloak・Realm設定
├── README.md                   # 本ドキュメント
├── docs/
│   ├── README.md               # ドキュメント目次
│   ├── architecture.md         # アーキテクチャ・設計詳細
│   ├── usage_guide.md          # セットアップ・利用ガイド
│   ├── customization.md        # 拡張・カスタマイズガイド
│   ├── api_implementation_plan.md # Web・モバイル向けAPI実装計画
│   ├── api_guide.md            # API利用ガイド
│   ├── postgresql_guide.md     # PostgreSQL構築・移行ガイド
│   ├── keycloak_oidc_guide.md  # Keycloak・OIDC構築ガイド
│   └── vision-api.md           # 汎用画像解析API設計・利用ガイド
├── src/
│   ├── __init__.py
│   ├── config.py               # 設定管理 (Pydantic Settings)
│   ├── tool_policy.py          # 副作用ツールの承認・登録ポリシー
│   ├── errors.py               # 利用者向けの安全なエラー分類
│   ├── state.py                # LangGraph 状態定義 (AgentState)
│   ├── tools.py                # エージェント用ツール群 (Web検索, 計算, 日時, SQLiteメモ)
│   ├── agent.py                # LangGraph ReActエージェント定義 & コンパイル
│   ├── api/
│   │   ├── app.py              # FastAPIアプリ、APIエンドポイント
│   │   ├── auth.py             # OIDC Discovery・JWKS・JWT検証
│   │   ├── protection.py       # CORS補助・レート制限ヘッダー・安全なJSONログ
│   │   ├── runtime.py          # PostgreSQL・チェックポインタのライフサイクル
│   │   ├── schemas.py          # API入出力スキーマ
│   │   ├── vision.py           # multipart画像の上限読込・確実な削除
│   │   └── sse.py              # SSEイベントエンコード
│   ├── db/
│   │   ├── models.py           # SQLAlchemyデータモデル
│   │   ├── repositories.py     # PostgreSQLリポジトリ・分散実行制御
│   │   ├── checkpoint.py       # LangGraph PostgreSQLチェックポインタ
│   │   ├── migrate_sqlite.py   # SQLiteからの移行ツール
│   │   └── cleanup.py          # 保存期限切れ会話の削除
│   ├── services/
│   │   ├── agent_service.py    # 共通実行サービス、非同期ストリーム、実行上限
│   │   ├── conversation_service.py # 会話メタデータ、同時実行・冪等性管理
│   │   ├── note_tools.py       # PostgreSQL対応メモツール
│   │   ├── retention_service.py # 保存期限クリーンアップ
│   │   ├── rate_limit.py       # 固定窓レート制限の共通型・ローカル実装
│   │   ├── model_service.py    # Ollama接続確認・モデル一覧取得
│   │   └── vision_service.py   # 画像・スキーマ検証、Ollama画像解析
│   ├── cli.py                  # 対話型Rich CLIアプリケーション
│   ├── web_api_client.py       # Streamlit用FastAPI・SSEクライアント
│   ├── web_conversation_ui.py  # 会話選択・表示名・自動タイトルの純粋ロジック
│   └── web_app.py              # Streamlit Webチャットアプリケーション
├── tests/
│   ├── __init__.py
│   ├── test_tools.py           # ツール群の単体テスト
│   ├── test_agent.py           # 同期・非同期グラフ構築・動作テスト
│   ├── test_api.py             # HTTP API、SSE、冪等性・同時実行テスト
│   ├── test_vision_api.py      # 画像アップロード一時領域の削除テスト
│   ├── test_vision_service.py  # 画像・Schema・Ollama異常系テスト
│   ├── test_auth.py            # JWT検証・認証必須・ユーザー分離テスト
│   ├── test_protection.py      # CORS・レート制限・ログ・承認ポリシーテスト
│   ├── test_persistence.py     # DBリポジトリ・メモ・保存期限テスト
│   ├── test_web_api_client.py  # Streamlit用APIクライアントテスト
│   ├── test_web_conversation_ui.py # Streamlit会話選択ロジックテスト
│   ├── test_web_app.py         # Streamlit画面スモークテスト
│   └── test_services.py        # 共通サービス、実行上限、Ollama状態テスト
└── data/                       # 会話履歴・メモのSQLite保存先 (自動生成、Git対象外)
```

---

## 🚀 クイックスタート

### 1. 前提条件

- Python 3.11 以上 (推奨: Python 3.12)
- [uv](https://docs.astral.sh/uv/) がインストールされていること
- [Ollama](https://ollama.com/) がインストール・起動されていること
- APIの永続化を利用する場合はPostgreSQL 15以上

```bash
# Ollamaモデルの準備 (例)
ollama pull qwen3.5:9b-mlx
# または
ollama pull qwen2.5:7b
```

### 2. インストール

```bash
# 依存関係の同期
uv sync
```

### 3. 環境設定

`.env.example` をコピーして `.env` を作成します（必要に応じてモデル名やURLを変更）。

```bash
cp .env.example .env
```

`.env` 設定項目：
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
VISION_MODEL=qwen3.5:9b-mlx
VISION_ALLOWED_MODELS=
VISION_TIMEOUT_SECONDS=120
VISION_THINK=false
VISION_KEEP_ALIVE=30m
VISION_MAX_IMAGE_BYTES=10485760
VISION_ALLOWED_MIME_TYPES=image/jpeg,image/png,image/webp
VISION_MAX_PROMPT_CHARS=5000
VISION_MAX_SCHEMA_BYTES=16384
VISION_MAX_RESPONSE_BYTES=1048576
VISION_MAX_IMAGE_WIDTH=8192
VISION_MAX_IMAGE_HEIGHT=8192
VISION_MAX_IMAGE_PIXELS=25000000
VISION_MAX_MODEL_IMAGE_EDGE=1280
VISION_MAX_SCHEMA_DEPTH=8
VISION_MAX_SCHEMA_PROPERTIES=100
VISION_MAX_ARRAY_ITEMS=100
VISION_MAX_OUTPUT_STRING_CHARS=10000
VISION_RATE_LIMIT_REQUESTS=10
VISION_RATE_LIMIT_WINDOW_SECONDS=60
API_HOST=127.0.0.1
API_PORT=8000
API_MAX_MESSAGE_CHARS=20000
WEB_API_BASE_URL=http://127.0.0.1:8000
WEB_API_TIMEOUT_SECONDS=180
IDEMPOTENCY_TTL_SECONDS=3600
IDEMPOTENCY_MAX_ENTRIES=1000
AUTH_MODE=oidc
OIDC_ISSUER_URL=http://192.168.100.2:8080/realms/langgraph
OIDC_AUDIENCE=langgraph-api
CORS_ALLOWED_ORIGINS=http://localhost:3000,http://localhost:5173
RATE_LIMIT_ENABLED=true
RATE_LIMIT_WINDOW_SECONDS=60
RATE_LIMIT_IP_REQUESTS=120
RATE_LIMIT_USER_REQUESTS=60
API_JSON_LOGGING=true
API_LOG_LEVEL=INFO
APPROVAL_REQUIRED_TOOLS=send_email,create_calendar_event,delete_file,execute_payment
DATABASE_URL=postgresql+asyncpg://langgraph:パスワード@192.168.100.2:5432/langgraph
CHECKPOINT_DATABASE_URL=postgresql://langgraph:パスワード@192.168.100.2:5432/langgraph
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=10
DATABASE_CONNECT_TIMEOUT_SECONDS=10
EXECUTION_LEASE_SECONDS=300
CONVERSATION_RETENTION_DAYS=90
```

---

## 🖥️ 実行方法

### 1. リッチCLIで対話する

ターミナル上で対話型エージェントを起動します。

```bash
uv run python -m src.cli
```

**CLIコマンド**:
- `/reset` or `/clear`: 会話セッション（Thread ID）をリセット
- `/session <ID>`: 指定したセッションへ切り替え
- `/history`: 現在のセッションの会話履歴を表示
- `/notes`: 保存されているメモを一覧表示
- `/model <モデル名>`: 使用するOllamaモデルを切り替え
- `/help`: ヘルプを表示
- `/exit` or `/quit`: 終了

---

### 2. Streamlit Web UIで対話する

先にFastAPIを起動し、ブラウザ上で操作できるWebチャットインターフェースを起動します。StreamlitはLangGraphやデータベースへ直接接続せず、すべての操作を`WEB_API_BASE_URL`のAPIへ送信します。

`AUTH_MODE=oidc`の場合は、Keycloakに機密クライアント`langgraph-streamlit`を作成し、OIDC設定例をコピーして実際のシークレットを設定します。

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

`.streamlit/secrets.toml`はGit対象外です。Keycloak側には`http://localhost:8501/oauth2callback`を有効なリダイレクトURIとして登録します。詳しい手順は[`deploy/keycloak/README.md`](deploy/keycloak/README.md)を参照してください。

```bash
uv run streamlit run src/web_app.py
```

ブラウザで `http://localhost:8501` にアクセスします。
- Keycloakログイン状態とAPI・Ollama・PostgreSQLの稼働状態を確認できます。
- 会話の新規作成、一覧選択、名前変更、アーカイブ・再開、削除に対応します。
- 会話選択では会話名・更新日時・短縮IDを表示し、同名会話を区別できます。
- 会話IDはURL指定を優先し、選択変更時もURLと同期します。最初の質問から会話名を自動設定します。
- 会話がない場合は空会話を自動作成せず、「新しい会話」を押したときだけ作成します。
- IME確定のEnterでは送信せず、右側の送信ボタンまたはCtrl／Command+Enterで送信します。
- 回答生成中は専用の停止ボタンを表示し、SSEハートビートを利用してAPIにも速やかにキャンセルを通知します。
- 部分応答やハートビートが継続していても、`AGENT_TIMEOUT_SECONDS`を超えた生成はAPI側で確実に終了し、Web UIは入力可能な状態へ戻ります。
- 会話IDはURLに保持され、ブラウザ再読み込み後もAPIから履歴を復元します。
- ツール実行は名前と状態だけを表示し、引数や実行出力は画面へ表示しません。
- 利用モデルとTemperatureはAPIサーバー側の`.env`で管理します。

---

### 3. Web・モバイル向けAPIを起動する

PostgreSQLとKeycloakを用意し、`.env`へデータベース、OIDC、CORS、レート制限設定を追加してから、アプリ所有テーブルを作成します。LangGraph所有テーブルはAPI初回起動時に安全に初期化されます。Dockge向けKeycloak設定は[`deploy/keycloak/README.md`](deploy/keycloak/README.md)を参照してください。

```bash
uv run alembic upgrade head
```

```bash
uv run uvicorn src.api.app:app --host 127.0.0.1 --port 8000
```

起動後は次のURLを利用できます。

- API仕様・試行画面: `http://127.0.0.1:8000/docs`
- 稼働確認: `GET /health`
- Ollamaを含む準備状態: `GET /ready`
- 利用可能モデル: `GET /v1/models`
- 汎用画像解析: `POST /v1/vision/analyze`
- 会話作成: `POST /v1/conversations`
- 会話一覧: `GET /v1/conversations`
- 会話詳細・更新・削除: `GET/PATCH/DELETE /v1/conversations/{conversation_id}`
- 会話履歴: `GET /v1/conversations/{conversation_id}/messages`
- 実行キャンセル: `POST /v1/conversations/{conversation_id}/cancel`
- 通常メッセージ: `POST /v1/conversations/{conversation_id}/messages`
- SSEメッセージ: `POST /v1/conversations/{conversation_id}/messages/stream`
- メモ一覧・作成: `GET/POST /v1/conversations/{conversation_id}/notes`
- メモ更新・削除: `PATCH/DELETE /v1/conversations/{conversation_id}/notes/{note_id}`

会話を作成してメッセージを送る例：

```bash
curl -X POST http://127.0.0.1:8000/v1/conversations \
  -H 'Authorization: Bearer アクセストークン'

curl -X POST \
  http://127.0.0.1:8000/v1/conversations/会話ID/messages \
  -H 'Authorization: Bearer アクセストークン' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 任意の一意なキー' \
  -d '{"content":"1+1を計算してください"}'
```

`AUTH_MODE=oidc`では、最初の会話作成リクエストにも`Authorization: Bearer ...`が必要です。StreamlitはKeycloakから取得したアクセストークンをサーバー側API呼び出しだけに使用します。`AUTH_MODE=disabled`はローカル開発互換専用であり、外部公開には使用しないでください。

画像解析APIのモデル準備、multipartリクエスト、JSON Schema、モバイル実装、エラー、保存・ログ方針は[`docs/vision-api.md`](docs/vision-api.md)を参照してください。

SSEでは `message.started`、`assistant.delta`、`tool.started`、
`tool.completed`、`message.completed`、`message.failed` のイベントを返します。
処理待ちの間は `: stream-heartbeat` コメントを送信します。一般的なSSEクライアントは
このコメントを無視でき、Streamlitは停止操作を受け付けるために利用します。

> [!NOTE]
> PostgreSQL設定時は、会話、メモ、LangGraph履歴、実行履歴、冪等性がAPI再起動後も保持されます。
> 同一会話の実行リースもPostgreSQLで共有されるため、複数APIプロセスから安全に利用できます。
> OIDC有効時はJWTの`sub`を内部ユーザーへ対応付け、全会話・メモ操作で所有者を確認します。他ユーザーの会話IDを指定しても`404`を返します。
> CORSは`CORS_ALLOWED_ORIGINS`に列挙したOriginだけを許可します。`*`は起動時に拒否されます。
> レート制限はPostgreSQL利用時に全APIプロセスで共有され、`429`、`RateLimit`、`RateLimit-Policy`、`Retry-After`で再試行時期を通知します。移行期間の互換性のため`RateLimit-Limit`、`RateLimit-Remaining`、`RateLimit-Reset`も返します。
> APIログにはリクエスト本文、Authorization、Cookie、ツール引数を記録せず、IPとユーザーIDはハッシュ化します。
> 現在のKeycloak ComposeはLAN内検証用です。インターネットへ公開する前にHTTPSとリバースプロキシを構成してください。

### SQLiteデータの移行

まず移行対象件数だけを確認し、内容を確認後に`--apply`を付けて実行します。再実行しても同じIDのデータは重複作成しません。

```bash
uv run python -m src.db.migrate_sqlite
uv run python -m src.db.migrate_sqlite --apply
```

### 保存期限切れ会話の削除

```bash
uv run python -m src.db.cleanup --limit 100
```

実行中の会話は削除せず次回へ保留し、会話メタデータ、メモ、実行履歴、LangGraphチェックポイントをまとめて削除します。

---

## 🧪 テスト実行

```bash
uv run pytest
```

現在は104件の自動テストを実行します。
