# Supabase 移行計画書 (Keycloak・自前PostgreSQL から Supabase への移行)

## 1. 概要と目的

本ドキュメントは、現在の認証基盤（Keycloak）および自前PostgreSQL構成から、**Supabase**への移行計画をまとめた実装仕様書です。
本移行は、次の環境構成をターゲットとします。

- **本番環境**: **Supabase Cloud（クラウド版）** を利用（フルマネージドPostgreSQL + Supabase Auth）
- **開発環境**: **Docker サーバー (`192.168.100.2`)** 上で Supabase（Docker Compose / Dockge）を稼働

### 移行の目的
1. **運用の劇的な軽量化**:
   - メモリ消費が大きく運用負荷の高い Java/Quarkus ベースの Keycloak を廃止。
   - PostgreSQL データベースと認証（Auth）が一体化され、管理コンソール（Supabase Studio）で一元管理が可能。
2. **本プロジェクト既存設計との高親和性**:
   - FastAPI / DB層（`src/db/models.py`）は、JWTの `sub`（UUID文字列）をユーザー識別子として会話・メモの完全分離（マルチテナント）を行っています。
   - Supabase Auth も同じく `sub`（UUID）を標準クレームとして発行するため、データベーススキーマの変更が不要です。
3. **将来の拡張性**:
   - 画像解析API向けの画像アップロード先としての **Supabase Storage** 連携や、LangGraphの長期記憶・RAGに向けた **pgvector** の活用が容易になります。

---

## 2. 移行前後のアーキテクチャ比較

### 移行前 (Before: Keycloak + 自前PostgreSQL)
```mermaid
flowchart LR
    Streamlit[Streamlit UI] -->|OAuth2 / OIDC PKCE| Keycloak[Keycloak コンテナ]
    Streamlit -->|Bearer Token| FastAPI[FastAPI]
    FastAPI -->|JWKS (RS256)| Keycloak
    FastAPI --> AppDB[(自前 PostgreSQL)]
    FastAPI --> LangGraph[(PostgreSQL Checkpointer)]
```

### 移行後 (After: Supabase)
```mermaid
flowchart LR
    subgraph Client["クライアント層"]
        Streamlit[Streamlit Web UI]
        MobileWeb[Web / モバイルアプリ]
    end

    subgraph Backend["バックエンド層"]
        FastAPI[FastAPI (src/api)]
        Agent[LangGraph ReAct Agent]
        Ollama[ローカル Ollama]
    end

    subgraph Supabase["Supabase (本番: Cloud / ローカル: Docker)"]
        SupaAuth[Supabase Auth (GoTrue)]
        SupaDB[(PostgreSQL 15+)]
        Studio[Supabase Studio UI]
    end

    Streamlit -->|1. ログイン & トークン取得| SupaAuth
    MobileWeb -->|1. ログイン & トークン取得| SupaAuth
    Streamlit -->|2. Bearer トークン付きリクエスト| FastAPI
    MobileWeb -->|2. Bearer トークン付きリクエスト| FastAPI

    FastAPI -->|3. JWT検証 (JWT Secret / JWKS)| SupaAuth
    FastAPI -->|4. 会話・メモ・チェックポイント保存| SupaDB
    FastAPI --> Agent
    Agent --> Ollama
```

---

## 3. コンポーネント別変更仕様

### 3.1 データベース層 (PostgreSQL)

本アプリのデータ永続化は SQLAlchemy + Alembic（会話・メモ・実行履歴）および LangGraph `AsyncPostgresSaver`（チェックポイント）によって構成されています。

1. **接続文字列**:
   - **開発環境 (Dockerサーバー: `192.168.100.2`)**:
     - `DATABASE_URL=postgresql+asyncpg://postgres:postgres@192.168.100.2:5432/postgres`
     - `CHECKPOINT_DATABASE_URL=postgresql://postgres:postgres@192.168.100.2:5432/postgres`
     *(※Composeのポートマッピング設定に応じてポート5432または54322等を指定)*
   - **本番 (Supabase Cloud)**:
     - `DATABASE_URL=postgresql+asyncpg://postgres:[DB_PASSWORD]@db.[PROJECT_REF].supabase.co:5432/postgres`
     - `CHECKPOINT_DATABASE_URL=postgresql://postgres:[DB_PASSWORD]@db.[PROJECT_REF].supabase.co:5432/postgres`
2. **接続ポートの重要ルール**:
   - Supabase はポート `6543`（PgBouncer / Supavisor トランザクションモード）とポート `5432`（Direct 接続 / セッションモード）を提供しています。
   - LangGraph の `AsyncPostgresSaver` および Alembic は、Prepared Statement やセッションレベルのDDLを利用するため、**必ずポート 5432（Direct接続 または セッションモード）** を使用してください。
3. **スキーママイグレーション**:
   - `uv run alembic upgrade head` により、Supabase 上に既存の `users`, `conversations`, `notes`, `execution_leases`, `execution_history`, `idempotency_records` テーブルを作成します。
   - LangGraph所有のチェックポイントテーブルは、API起動時に自動生成されます。
4. **RLS (Row Level Security)**:
   - 移行初期フェーズでは、FastAPI のリポジトリ層（`src/db/repositories.py`）によるアプリケーションレベル認可（`sub` チェック）を維持します。
   - 将来的に直接フロントエンドから Supabase DB にアクセスする要件が生じた場合に RLS ポリシーを追加します。

---

### 3.2 認証・認可層 (`src/config.py`, `src/api/auth.py`)

#### 設定 (`src/config.py`) の拡張
`Settings` に Supabase 向けの設定項目を追加します。

```python
# Authentication settings
auth_mode: Literal["disabled", "oidc", "supabase"] = "disabled"

# Supabase Auth settings
supabase_url: str | None = None               # 例: https://<project-ref>.supabase.co または http://127.0.0.1:54321
supabase_jwt_secret: str | None = None        # Supabase Dashboard > Project Settings > API > JWT Secret
supabase_jwt_algorithm: str = "HS256"         # デフォルトは HS256。非対称鍵利用時は RS256/ES256
supabase_audience: str = "authenticated"      # Supabase Auth が発行する JWT の標準 aud
```

#### JWT検証ロジック (`src/api/auth.py`) の拡張
Supabase Auth は標準で対称鍵（`HS256`）の JWT を発行します（非対称鍵 JWKS エンドポイントも提供）。
FastAPI 側でこれらを検証できるように `SupabaseTokenAuthenticator` を新設（または `OpenIDConnectAuthenticator` を拡張）します。

- **検証内容**:
  1. アルゴリズムの検証（`HS256` または `RS256` / `ES256`）
  2. 署名の検証（`supabase_jwt_secret` または Supabase JWKS）
  3. `aud` クレームの検証（`"authenticated"`）
  4. `exp`（有効期限）、`iat`（発行時刻）の検証
  5. `sub`（ユーザーUUID）の取得とバリデーション
  6. 表示名（`email` または `user_metadata.full_name`）の抽出
- **戻り値**:
  - 既存の `AuthenticatedPrincipal(subject=sub, display_name=...)` をそのまま返却するため、下流の `app.py` や `repositories.py` には一切影響を与えません。

---

### 3.3 フロントエンド (Streamlit: `src/web_app.py`)

Keycloak の OIDC リダイレクトフロー（`st.login()` + OAuth2 PKCE）から、Supabase Auth 連携へ移行します。

1. **依存関係の追加**:
   - `supabase>=2.0.0` を `pyproject.toml` に追加。
2. **ログイン画面のUI設計**:
   - Streamlit 側で Email & Password のログインフォーム（または Magic Link 送信フォーム）を表示。
   ```python
   from supabase import create_client, Client
   supabase: Client = create_client(settings.supabase_url, settings.supabase_anon_key)

   # ログイン処理
   auth_response = supabase.auth.sign_in_with_password({
       "email": email,
       "password": password,
   })
   access_token = auth_response.session.access_token
   st.session_state["access_token"] = access_token
   st.session_state["user"] = auth_response.user
   ```
3. **APIクライアントへのトークン受け渡し**:
   - 取得した `access_token` を `AgentApiClient(..., access_token=access_token)` に渡す（既存のコードと完全互換）。
4. **ログアウト処理**:
   - `supabase.auth.sign_out()` を呼び出し、`st.session_state` をクリアして `st.rerun()`。

---

### 3.4 廃止・整理対象の資産
移行完了後、不要となった Keycloak 関連資産を安全に整理・アーカイブします。
- `deploy/keycloak/` ディレクトリ全体（Docker Compose、Realm設定、README）
- `docs/keycloak_oidc_guide.md`（Supabase 移行ガイドへ置き換え、またはアーカイブ）
- `.streamlit/secrets.toml.example`（Keycloak OIDC 設定から Supabase 設定へ更新）

---

## 4. 開発用 Docker サーバー (`192.168.100.2`) の設計

開発環境では、LAN内サーバー `192.168.100.2`（Dockge 等の管理ツールまたは Docker Compose）上で Supabase コンテナ群を稼働させます。

### 構成: Self-hosted Supabase Compose (`deploy/supabase/compose.yaml`)
Supabase 公式の Docker Compose スタックを `deploy/supabase/compose.yaml` として用意し、`192.168.100.2` 上で起動します。

- **主要サービス**:
  - **PostgreSQL (`db`)**: `192.168.100.2:5432`（アプリのDBおよびLangGraphチェックポインタ）
  - **Supabase Auth / GoTrue (`auth`)**: `192.168.100.2:9999` または Kong 経由
  - **API Gateway (Kong)**: `192.168.100.2:8000` (または `54321`)
  - **Supabase Studio (`studio`)**: `http://192.168.100.2:54323`（ブラウザからテーブル・AuthユーザーをGUI管理）
  - **Inbucket (`inbucket`)**: `http://192.168.100.2:54324`（メール確認用UI）

- **ポートバインド上の配慮**:
  `deploy/postgres/compose.yaml` と同様に、LAN内から安全に接続できるようバインドIP・ポートを指定します。
  例: `"192.168.100.2:5432:5432"`, `"192.168.100.2:54323:3000"`

- **固定認証情報 (開発環境用)**:
  - `JWT_SECRET`: `super-secret-jwt-token-with-at-least-32-characters-long`
  - `ANON_KEY`: 固定生成キー

---

## 5. 環境変数設計 (`.env.example`)

Supabase 移行後の `.env.example` の設定例です。

```ini
# ==========================================
# Ollama 設定
# ==========================================
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3.5:9b-mlx

# ==========================================
# 認証モード (disabled | oidc | supabase)
# ==========================================
AUTH_MODE=supabase

# --- Supabase 認証設定 ---
# 開発環境 (Dockerサーバー 192.168.100.2) 利用時:
SUPABASE_URL=http://192.168.100.2:8000
SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
SUPABASE_JWT_SECRET=super-secret-jwt-token-with-at-least-32-characters-long
SUPABASE_AUDIENCE=authenticated
SUPABASE_JWT_ALGORITHM=HS256

# 本番 (Supabase Cloud) 利用時の設定例:
# SUPABASE_URL=https://<PROJECT_REF>.supabase.co
# SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
# SUPABASE_JWT_SECRET=<Dashboardで確認したJWT_SECRET>
# SUPABASE_AUDIENCE=authenticated
# SUPABASE_JWT_ALGORITHM=HS256

# ==========================================
# データベース (PostgreSQL) 設定
# ==========================================
# 開発環境 (Dockerサーバー 192.168.100.2) 利用時:
DATABASE_URL=postgresql+asyncpg://postgres:postgres@192.168.100.2:5432/postgres
CHECKPOINT_DATABASE_URL=postgresql://postgres:postgres@192.168.100.2:5432/postgres

# 本番 (Supabase Cloud) 利用時:
# DATABASE_URL=postgresql+asyncpg://postgres:[PASSWORD]@db.[PROJECT_REF].supabase.co:5432/postgres
# CHECKPOINT_DATABASE_URL=postgresql://postgres:[PASSWORD]@db.[PROJECT_REF].supabase.co:5432/postgres

DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=10
DATABASE_CONNECT_TIMEOUT_SECONDS=10
EXECUTION_LEASE_SECONDS=300
CONVERSATION_RETENTION_DAYS=90

# ==========================================
# API & サーバー保護設定
# ==========================================
API_HOST=127.0.0.1
API_PORT=8000
WEB_API_BASE_URL=http://127.0.0.1:8000
CORS_ALLOWED_ORIGINS=http://localhost:8501,http://localhost:3000
RATE_LIMIT_ENABLED=true
```

---

---

## 6. 懸念事項と設計上の対策 (Technical Considerations & Mitigations)

移行および運用にあたり、事前に把握・対処しておくべき 5 つの懸念事項と対策方針です。

### 6.1 開発サーバー (`192.168.100.2`) のコンテナリソース消費
- **懸念**: Supabase 公式のフルスタック Docker Compose は、PostgreSQL, GoTrue, PostgREST, Realtime, Storage, Imgproxy, Kong, Studio 等 **10〜12個以上のコンテナ** が起動し、メモリを消費します。
- **対策**: 本アプリに必要なサービス（**PostgreSQL + GoTrue + Kong + Studio**）のみに絞った「**スリム版 Compose (`deploy/supabase/compose.yaml`)**」を用意して運用します。これにより Keycloak 運用時と同等以下の軽量なリソースで稼働させます。

### 6.2 JWTトークンの有効期限（1時間）と自動リフレッシュ
- **懸念**: Supabase Auth のアクセストークン（JWT）はデフォルトで **1時間（3600秒）** で有効期限が切れます。長時間のチャットセッションで突然 `401 Unauthorized` が発生し、回答生成が中断する恐れがあります。
- **対策**: Streamlit 側でセッションの有効期限（`exp`）を監視し、期限が迫った際（例: 残り5分未満）またはAPI呼び出し前に `supabase.auth.refresh_session()` を実行して、**自動的にアクセストークンを再取得・更新** するロジックを実装します。

### 6.3 Supabase Cloud（本番）の同時接続数制限
- **懸念**: Supabase Cloud の Free Tier では直接接続（ポート 5432）の上限が約 60 接続です。本アプリは FastAPI 側の SQLAlchemy プール（5〜15接続）と LangGraph 用の `AsyncConnectionPool`（1〜5接続）の 2 つのプールを保持しています。
- **対策**: 個人〜少人数運用では問題ありませんが、APIプロセスを複数立ち上げる場合は `.env` の `DATABASE_POOL_SIZE`（デフォルト 5）を適切に抑えて設定します。また、LangGraph 側の `src/db/checkpoint.py` は既に `prepare_threshold: 0` が設定されており、トランザクションプーラー経由でも動作可能な設計になっています。

### 6.4 既存データ（会話履歴・メモ）の移行
- **懸念**: 旧 PostgreSQL (`192.168.100.2`) や SQLite に保存された既存の会話データやメモの扱い。
- **対策**:
  - 新規開発・クリーンスタートで問題ない場合は、Supabase DB に対して `alembic upgrade head` を実行するのみとします。
  - 過去データを移行したい場合は、既存の移行スクリプト `src/db/migrate_sqlite.py` を活用するか、SQL テーブル単位でのダンプ＆リストアを行います。

### 6.5 開発環境 (HTTP) と 本番環境 (HTTPS) の差異
- **懸念**: 開発環境は `http://192.168.100.2:...` の平文 HTTP、本番は `https://<ref>.supabase.co` の HTTPS となります。
- **対策**: Supabase Auth の Redirect URL / Site URL に `http://localhost:8501`（Streamlit）などの開発環境オリジンを明示的に登録し、ブラウザのセキュリティ制限（CORS / Secure Cookie）を回避します。

---

## 7. 段階的実装ロードマップ

| フェーズ | 作業内容 | 対象ファイル |
| :--- | :--- | :--- |
| **Phase 1**<br>開発用スリム版 Docker 環境構築 | ・スリム版 Compose ファイル作成（Postgres, GoTrue, Kong, Studio）<br>・`192.168.100.2` でのコンテナ起動確認・Studio アクセス確認 | `deploy/supabase/compose.yaml` |
| **Phase 2**<br>DB接続 & マイグレーション検証 | ・`.env` の DB 接続先を開発サーバー Supabase に向ける<br>・`uv run alembic upgrade head` の実行<br>・LangGraph チェックポイントテーブル生成確認 | `.env`<br>`migrations/` |
| **Phase 3**<br>FastAPI 認証の Supabase 対応 | ・`src/config.py` に Supabase 設定を追加<br>・`src/api/auth.py` に HS256/Supabase JWT 検証を追加<br>・`tests/test_auth.py` に Supabase JWT テストを追加 | `src/config.py`<br>`src/api/auth.py`<br>`tests/test_auth.py` |
| **Phase 4**<br>Streamlit の Supabase ログイン & 自動更新 | ・`pyproject.toml` に `supabase` クライアント追加<br>・`src/web_app.py` に Supabase ログイン画面実装<br>・トークン自動リフレッシュ処理の実装<br>・API クライアントへのトークン受け渡し | `pyproject.toml`<br>`src/web_app.py`<br>`.streamlit/secrets.toml.example` |
| **Phase 5**<br>結合テスト & クリーンアップ | ・E2E動作確認（ログイン → 会話作成 → 1時間以上の持続検証 → SSEストリーミング）<br>・既存テスト全件実行（`uv run pytest`）<br>・`deploy/keycloak/` の削除またはアーカイブ<br>・ドキュメント更新 | `tests/`<br>`deploy/keycloak/`<br>`README.md` |

---

## 8. Codex への実装指示書（プロンプト・タスクリスト）

実装を Codex へ依頼する際は、以下のプロンプトとタスクリストを使用してください。

````markdown
### Codex への指示プロンプト例

あなたは優秀なソフトウェアエンジニアです。
プロジェクト `langgraph-ollama-agent` において、認証基盤を Keycloak から Supabase へ切り替える実装を行ってください。
詳細な設計仕様および懸念対策は `docs/supabase_migration_plan.md` を参照してください。

#### 【実装タスク】

1. **開発用スリム版 Docker Compose の作成 (`deploy/supabase/compose.yaml`)**:
   - `192.168.100.2` 上で軽量に動作するよう、PostgreSQL, GoTrue (Auth), Kong (API Gateway), Supabase Studio に絞ったスリムな Compose ファイルを作成してください。

2. **依存関係の追加 (`pyproject.toml`)**:
   - `supabase>=2.0.0` を dependencies に追加してください。

3. **設定クラスの拡張 (`src/config.py`)**:
   - `auth_mode` に `"supabase"` を追加（Literal["disabled", "oidc", "supabase"]）。
   - `supabase_url`, `supabase_anon_key`, `supabase_jwt_secret`, `supabase_jwt_algorithm`, `supabase_audience` を追加してください。

4. **JWT認証検証の拡張 (`src/api/auth.py`)**:
   - Supabase Auth の HS256（対称鍵）および RS256/ES256 トークンを検証できる認証クラス（または既存クラスの拡張）を実装してください。
   - `aud`（デフォルト `"authenticated"`）と `sub`（UUID文字列）を正しく検証し、`AuthenticatedPrincipal(subject=sub, display_name=...)` を返却してください。

5. **FastAPI Lifespan / DI の対応 (`src/api/app.py`, `src/api/runtime.py`)**:
   - `AUTH_MODE=supabase` 時に適切な Authenticator を DI するよう対応してください。

6. **Streamlit UI の認証 & 自動リフレッシュ更新 (`src/web_app.py`)**:
   - `AUTH_MODE=supabase` 時に、Supabase Auth によるログイン画面（Email/Password）を表示し、取得した `access_token` を `AgentApiClient` に渡すよう実装してください。
   - セッション有効期限（`exp`）を監視し、トークン失効前に `supabase.auth.refresh_session()` で自動更新するハンドリングを組み込んでください。

7. **自動テストの更新 (`tests/test_auth.py`, `tests/test_api.py`)**:
   - Supabase 用の JWT（HS256 署名）を生成し、正常系・異常系（期限切れ、署名不正、aud不正等）を検証するテストを追加してください。
   - `uv run pytest` がすべてパスすることを確認してください。

8. **環境設定・ドキュメント更新**:
   - `.env.example` に Supabase 設定のサンプルを追加してください。
   - 不要となった `deploy/keycloak/` の整理を行ってください。
````

---

## 9. ロールバック計画

万が一移行中に予期せぬ障害が発生した場合の復旧手順です。

1. **設定ロールバック**:
   - `.env` の `AUTH_MODE` を `oidc` または `disabled` に戻す。
   - `DATABASE_URL` を以前の自前 PostgreSQL 接続文字列に戻す。
2. **コードロールバック**:
   - Git にて移行前のブランチ/コミットにチェックアウト。
   - Keycloak コンテナ（`deploy/keycloak/`）を再起動。
3. **データ保全**:
   - Supabase への移行中も、旧 PostgreSQL のデータベースファイルおよび Docker ボリュームは削除せず保持しておくこと。
