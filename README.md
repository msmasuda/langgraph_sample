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
  - LangGraphの `SqliteSaver` チェックポインタにより、スレッド単位で過去の会話コンテキストを永続化。
- **共通エージェントサービス**:
  - CLI、Streamlit、FastAPIから共通利用できる `AgentService` を提供。
  - 非同期実行、全体タイムアウト、ツール呼び出し上限、安全なエラー変換に対応。
- **Web・モバイル向けAPI**:
  - FastAPIによる会話作成、通常応答、Server-Sent Events（SSE）ストリーミングを提供。
  - リクエストID、冪等性キー、同一会話の同時実行防止、安全なエラー応答に対応。
- **Ollama状態確認**:
  - Ollamaへの接続状態とインストール済みモデルを取得し、Web UIへ表示。
- **充実のツールセット**:
  - 🌐 **Web検索 (`web_search`)**: DuckDuckGoを利用したリアルタイム最新情報取得
  - 🔢 **計算機 (`calculator`)**: 安全な数式評価・数学関数実行
  - ⏰ **日時取得 (`get_current_datetime`)**: 現在の日時・曜日・タイムゾーン（JST/UTC）情報取得
  - 📝 **メモ管理 (`save_note`, `read_notes`)**: スレッド単位のSQLiteメモ保存と読み出し
- **3種類のインターフェース**:
  - 💻 **リッチCLI (`src/cli.py`)**: Richライブラリによるスタイリッシュな対話、ツール呼び出しプロセスの可視化
  - 🌐 **Web UI (`src/web_app.py`)**: Streamlitによるブラウザ対話画面、モデル切り替え、ツール実行ログ詳細表示
  - 🔌 **HTTP API (`src/api/app.py`)**: Web・モバイルアプリ向けJSON APIとSSEストリーミング

---

## 📁 プロジェクト構成

```
langgraph_sample/
├── pyproject.toml              # プロジェクト設定・依存関係 (uv)
├── .python-version             # Python 3.12 指定
├── .env.example                # 環境変数サンプル
├── .env                        # 設定ファイル (Ollama設定等)
├── README.md                   # 本ドキュメント
├── src/
│   ├── __init__.py
│   ├── config.py               # 設定管理 (Pydantic Settings)
│   ├── errors.py               # 利用者向けの安全なエラー分類
│   ├── state.py                # LangGraph 状態定義 (AgentState)
│   ├── tools.py                # エージェント用ツール群 (Web検索, 計算, 日時, SQLiteメモ)
│   ├── agent.py                # LangGraph ReActエージェント定義 & コンパイル
│   ├── api/
│   │   ├── app.py              # FastAPIアプリ、APIエンドポイント
│   │   ├── schemas.py          # API入出力スキーマ
│   │   └── sse.py              # SSEイベントエンコード
│   ├── services/
│   │   ├── agent_service.py    # 共通実行サービス、非同期ストリーム、実行上限
│   │   ├── conversation_service.py # 会話メタデータ、同時実行・冪等性管理
│   │   └── model_service.py    # Ollama接続確認・モデル一覧取得
│   ├── cli.py                  # 対話型Rich CLIアプリケーション
│   └── web_app.py              # Streamlit Webチャットアプリケーション
├── tests/
│   ├── __init__.py
│   ├── test_tools.py           # ツール群の単体テスト
│   ├── test_agent.py           # 同期・非同期グラフ構築・動作テスト
│   ├── test_api.py             # HTTP API、SSE、冪等性・同時実行テスト
│   └── test_services.py        # 共通サービス、実行上限、Ollama状態テスト
└── data/                       # 会話履歴・メモのSQLite保存先 (自動生成、Git対象外)
```

---

## 🚀 クイックスタート

### 1. 前提条件

- Python 3.11 以上 (推奨: Python 3.12)
- [uv](https://docs.astral.sh/uv/) がインストールされていること
- [Ollama](https://ollama.com/) がインストール・起動されていること

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
API_HOST=127.0.0.1
API_PORT=8000
API_MAX_MESSAGE_CHARS=20000
IDEMPOTENCY_TTL_SECONDS=3600
IDEMPOTENCY_MAX_ENTRIES=1000
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

ブラウザ上で操作できるWebチャットインターフェースを起動します。

```bash
uv run streamlit run src/web_app.py
```

ブラウザで `http://localhost:8501` にアクセスします。
- サイドバーからOllama接続状態とインストール済みモデルの確認、モデル選択、Temperatureの調整、スレッドのリセット、現在のスレッド専用メモの確認が可能です。
- アシスタントの回答時に「ツール実行ログ」がアコーディオン形式で詳細表示されます。
- Web UIのスレッドIDは推測困難なUUIDとしてURLに保持されます。URLを共有すると会話へアクセスできるため、外部公開時は別途認証を追加してください。

---

### 3. Web・モバイル向けAPIを起動する

```bash
uv run uvicorn src.api.app:app --host 127.0.0.1 --port 8000
```

起動後は次のURLを利用できます。

- API仕様・試行画面: `http://127.0.0.1:8000/docs`
- 稼働確認: `GET /health`
- Ollamaを含む準備状態: `GET /ready`
- 利用可能モデル: `GET /v1/models`
- 会話作成: `POST /v1/conversations`
- 通常メッセージ: `POST /v1/conversations/{conversation_id}/messages`
- SSEメッセージ: `POST /v1/conversations/{conversation_id}/messages/stream`

会話を作成してメッセージを送る例：

```bash
curl -X POST http://127.0.0.1:8000/v1/conversations

curl -X POST \
  http://127.0.0.1:8000/v1/conversations/会話ID/messages \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 任意の一意なキー' \
  -d '{"content":"1+1を計算してください"}'
```

SSEでは `message.started`、`assistant.delta`、`tool.started`、
`tool.completed`、`message.completed`、`message.failed` のイベントを返します。

> [!NOTE]
> フェーズ2では、会話IDの登録、同時実行制御、冪等性キャッシュはAPIプロセス内で管理します。
> API再起動時にこれらは失われるため、現段階ではワーカー数を1にしてください。
> PostgreSQLによる永続化・複数ワーカー対応はフェーズ3で実装予定です。
> インターネットへ公開する場合は、認証・認可、HTTPS、レート制限を追加してください。

---

## 🧪 テスト実行

```bash
uv run pytest
```
