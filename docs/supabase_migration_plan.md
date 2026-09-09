# Supabase 移行計画書 (Keycloak・自前PostgreSQL から Supabase への移行)

## 1. 概要と目的

本ドキュメントは、現在の認証基盤（Keycloak）および自前PostgreSQL構成から、**Supabase**への移行計画をまとめた実装仕様書です。
本移行は、次の環境構成をターゲットとします。

> 実装状況（2026-09-08）: アプリコード、Dockge向けCompose、鍵生成、自動テスト、ドキュメントは実装済みです。`192.168.100.2`で5サービスの起動、Auth/JWKS、Studio認証、DB接続（公開ポート15432）を確認しました。Alembic適用、実ログインを使った結合確認が残っています。

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

    FastAPI -->|3. JWT検証 (OIDC / JWKS)| SupaAuth
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
     - `DATABASE_URL=postgresql+asyncpg://postgres:[DB_PASSWORD]@192.168.100.2:15432/postgres`
     - `CHECKPOINT_DATABASE_URL=postgresql://postgres:[DB_PASSWORD]@192.168.100.2:15432/postgres`
     *(※Composeのポートマッピング設定に応じてポート5432または54322等を指定)*
   - **本番 (Supabase Cloud)**:
     - IPv6で到達できる常駐バックエンドはDirect接続を使用します。
       - `DATABASE_URL=postgresql+asyncpg://postgres:[DB_PASSWORD]@db.[PROJECT_REF].supabase.co:5432/postgres`
       - `CHECKPOINT_DATABASE_URL=postgresql://postgres:[DB_PASSWORD]@db.[PROJECT_REF].supabase.co:5432/postgres`
     - IPv4のみの環境はDashboardのConnect画面に表示されるSession Pooler（ポート`5432`）を使用します。
     - パスワードに予約文字が含まれる場合はURLエンコードします。
2. **接続ポートの重要ルール**:
   - AlembicはDirect接続を使用します。
   - FastAPIとLangGraphの常駐接続は、Direct接続またはSession Pooler（いずれもポート`5432`）を使用します。
   - Transaction Pooler（ポート`6543`）はprepared statementに制約があり、現行のSQLAlchemy設定では対応していないため使用しません。
3. **スキーママイグレーション**:
   - 既存のAlembicを唯一のマイグレーション管理手段として維持し、Supabase CLIのマイグレーションとの二重管理は行いません。
   - `uv run alembic upgrade head` により、Supabase上に既存の`users`、`conversations`、`notes`、`tool_executions`、`usage_records`、`idempotency_records`、`conversation_executions`、`rate_limit_buckets`を作成します。
   - LangGraph所有のチェックポイントテーブルは、API起動時に自動生成されます。
4. **Data APIとRLS (Row Level Security)**:
   - クライアントはFastAPIだけを利用し、Supabase Data APIからアプリ所有テーブルへ直接アクセスさせません。
   - 本番Supabase CloudではData APIを無効化します。無効化できない場合は、`anon`と`authenticated`の権限を明示的に剥奪し、全アプリ所有テーブルでRLSを有効化してポリシーなし（既定拒否）とします。
   - 開発用ComposeにはPostgRESTを含めません。
   - 将来Data APIを利用すると決まった時点で、専用の公開スキーマとRLSポリシーを追加します。

---

### 3.2 認証・認可層 (`src/config.py`, `src/api/auth.py`)

#### 設定 (`src/config.py`) の拡張
Supabase専用の認証モードや検証クラスは増やさず、既存の汎用OIDC設定を拡張します。

```python
# Authentication settings（既存項目）
auth_mode: Literal["disabled", "oidc"] = "disabled"
oidc_issuer_url: str | None = None
oidc_audience: str = "authenticated"
oidc_jwks_url: str | None = None

# 非対称署名方式を環境ごとに1つだけ許可
oidc_jwt_algorithm: Literal["RS256", "ES256"] = "RS256"

# StreamlitがSupabase Authへ接続するための公開設定
supabase_url: str | None = None
supabase_publishable_key: str | None = None
```

#### JWT検証ロジック (`src/api/auth.py`) の拡張
既存の`OpenIDConnectAuthenticator`をそのまま利用し、設定された非対称署名方式を検証できるよう最小限拡張します。Supabase CloudではDashboardで選択したES256（推奨）またはRS256、開発用self-hosted環境では公式構成が生成するES256を使用し、共有`JWT_SECRET`をFastAPIへ渡しません。

- **検証内容**:
  1. 設定した単一アルゴリズムの検証（`RS256`または`ES256`）
  2. Supabase JWKSによる署名検証
  3. `aud` クレームの検証（`"authenticated"`）
  4. `iss`が環境ごとの`OIDC_ISSUER_URL`と完全一致することの検証
  5. `exp`（有効期限）、`iat`（発行時刻）の検証
  6. `sub`（ユーザーUUID）の取得とバリデーション
  7. 表示名（`email`または`user_metadata.full_name`）の抽出
- **戻り値**:
  - 既存の `AuthenticatedPrincipal(subject=sub, display_name=...)` をそのまま返却するため、下流の `app.py` や `repositories.py` には一切影響を与えません。

---

### 3.3 フロントエンド (Streamlit: `src/web_app.py`)

Keycloak の OIDC リダイレクトフロー（`st.login()` + OAuth2 PKCE）から、Supabase Auth 連携へ移行します。

1. **既存HTTPクライアントの再利用**:
   - 既存依存の`httpx`でGoTrueのログイン、更新、ログアウトだけを呼びます。Supabase SDKは追加しません。
2. **ログイン画面のUI設計**:
   - 初期実装ではStreamlit側にEmail & Passwordのログインフォームだけを表示します。
   ```python
   auth_client = SupabaseAuthClient(
       settings.supabase_url,
       settings.supabase_publishable_key,
       settings.oidc_http_timeout_seconds,
   )
   st.session_state.supabase_session = auth_client.sign_in(email, password)
   ```
3. **APIクライアントへのトークン受け渡し**:
   - 取得した `access_token` を `AgentApiClient(..., access_token=access_token)` に渡す（既存のコードと完全互換）。
4. **ログアウト処理**:
   - GoTrueの`/logout`を呼び出し、`st.session_state` をクリアして `st.rerun()`。
5. **セッションの扱い**:
   - 認証クライアントをグローバル変数や`st.cache_resource`で共有せず、利用者ごとのトークンを`st.session_state`に保持します。
   - 更新時は保存済みrefresh tokenをGoTrueへ渡し、返されたaccess tokenと一回限りのrefresh tokenを1つのセッションとして置き換えます。
   - `st.session_state`はタブを閉じた場合やサーバー再起動時には失われるため、初期実装では再ログインが必要です。refresh tokenを独自Cookieへ保存する実装は行いません。
   - ブラウザ再起動後もログインを維持する要件が確定した場合は、Supabase OAuth/OIDC ServerとStreamlitの`st.login()`を利用する方式を別フェーズで検討します。

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
Supabase公式self-hostedリリースのComposeと設定生成スクリプトを基に、使用するリリースを固定して`192.168.100.2`上で起動します。Gateway設定とAuth用DBユーザー設定をComposeへ埋め込み、DockgeのWeb UIでは`compose.yaml`と`.env`だけを編集します。

- **主要サービス**:
  - **PostgreSQL (`db`)**: `192.168.100.2:15432`（アプリのDBおよびLangGraphチェックポインタ）
  - **Supabase Auth / GoTrue (`auth`)**: API Gateway経由で公開
  - **API Gateway (Kong)**: `192.168.100.2:8000`（公式v0.8.0の対応Gateway構成を縮小利用）
  - **Supabase Studio (`studio`)**: `http://192.168.100.2:8000`（Basic認証後、テーブル・AuthユーザーをGUI管理）
  - **Postgres Meta (`meta`)**: StudioからのDB管理に使用
- **含めないサービス**:
  - PostgREST、Realtime、Storage、Imgproxy、Edge Runtime、Analytics。必要性が確定した時点で公式構成から戻します。

- **ポートバインド上の配慮**:
  Gatewayの8000番とPostgreSQLの5432番だけを公開します。LAN外へは公開しません。

- **認証情報 (開発環境用)**:
  - 公式v0.8.0の生成処理に合わせた`generate-env.mjs`で環境固有の秘密鍵と`SUPABASE_PUBLISHABLE_KEY`を生成し、リポジトリへコミットしません。
  - Authの`GOTRUE_JWT_KEYS`へ生成済み非対称鍵を設定し、公開JWKSエンドポイントで検証鍵を配布します。
  - StudioはGatewayのBasic認証で保護します。

---

## 5. 環境変数設計 (`.env.example`)

Supabase 移行後の `.env.example` の設定例です。本番ではDashboardでES256（推奨）またはRS256署名鍵を作成・有効化してから、選択した方式を設定します。

```ini
# ==========================================
# Ollama 設定
# ==========================================
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3.5:9b-mlx

# ==========================================
# 認証モード (disabled | oidc)
# ==========================================
AUTH_MODE=oidc

# --- Supabase Auth（既存の汎用OIDC検証を利用）---
# 開発環境 (Dockerサーバー 192.168.100.2) 利用時:
SUPABASE_URL=http://192.168.100.2:8000
SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
OIDC_ISSUER_URL=http://192.168.100.2:8000/auth/v1
OIDC_AUDIENCE=authenticated
OIDC_JWKS_URL=http://192.168.100.2:8000/auth/v1/.well-known/jwks.json
OIDC_JWT_ALGORITHM=ES256

# 本番 (Supabase Cloud) 利用時の設定例:
# SUPABASE_URL=https://<PROJECT_REF>.supabase.co
# SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
# OIDC_ISSUER_URL=https://<PROJECT_REF>.supabase.co/auth/v1
# OIDC_AUDIENCE=authenticated
# OIDC_JWKS_URL=https://<PROJECT_REF>.supabase.co/auth/v1/.well-known/jwks.json
# OIDC_JWT_ALGORITHM=ES256

# ==========================================
# データベース (PostgreSQL) 設定
# ==========================================
# 開発環境 (Dockerサーバー 192.168.100.2) 利用時:
DATABASE_URL=postgresql+asyncpg://postgres:[DB_PASSWORD]@192.168.100.2:15432/postgres
CHECKPOINT_DATABASE_URL=postgresql://postgres:[DB_PASSWORD]@192.168.100.2:15432/postgres

# 本番 (Supabase Cloud) 利用時（IPv6対応環境のDirect接続）:
# DATABASE_URL=postgresql+asyncpg://postgres:[PASSWORD]@db.[PROJECT_REF].supabase.co:5432/postgres
# CHECKPOINT_DATABASE_URL=postgresql://postgres:[PASSWORD]@db.[PROJECT_REF].supabase.co:5432/postgres
# IPv4のみの場合はDashboardのConnect画面に表示されるSession PoolerのURLを使用

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

## 6. 懸念事項と設計上の対策 (Technical Considerations & Mitigations)

移行および運用にあたり、事前に把握・対処しておくべき6つの懸念事項と対策方針です。

### 6.1 開発サーバー (`192.168.100.2`) のコンテナリソース消費
- **懸念**: Supabase公式のフルスタックDocker Composeは、PostgreSQL、GoTrue、PostgREST、Realtime、Storage、Imgproxy、API Gateway、Studio等、多数のコンテナが起動してメモリを消費します。
- **対策**: 公式self-hostedリリースを基に、**PostgreSQL + GoTrue + Kong + Studio + Postgres Meta**へ絞ります。採用した公式リリースと削除したサービスをREADMEへ記録します。

### 6.2 JWTトークンの有効期限（1時間）と自動リフレッシュ
- **懸念**: Supabase Auth のアクセストークン（JWT）はデフォルトで **1時間（3600秒）** で有効期限が切れます。長時間のチャットセッションで突然 `401 Unauthorized` が発生し、回答生成が中断する恐れがあります。
- **対策**: Streamlit側でセッションの有効期限（`exp`）を監視し、期限が迫った際（例: 残り5分未満）またはAPI呼び出し前に、利用者ごとのrefresh tokenを明示して`refresh_session()`を実行します。返されたaccess tokenと一回限りのrefresh tokenは同時に置き換えます。ページ操作によるrerunでは維持しますが、タブ終了・サーバー再起動後の永続ログインは初期対象外です。

### 6.3 Supabase Cloud（本番）の同時接続数制限
- **懸念**: 接続上限はSupabaseのプランとCompute構成で変わります。本アプリはFastAPI側のSQLAlchemyプールとLangGraph用`AsyncConnectionPool`の2つをAPIプロセスごとに保持します。
- **対策**: `DATABASE_POOL_SIZE`とAPIプロセス数から最大接続数を算出し、契約プランの上限内に抑えます。常駐バックエンドはDirect接続、IPv4のみならSession Poolerを使用し、Transaction Poolerは採用しません。

### 6.4 既存データ（会話履歴・メモ）の移行
- **懸念**: 旧 PostgreSQL (`192.168.100.2`) や SQLite に保存された既存の会話データやメモの扱い。
- **対策**:
  - 本移行ではテストユーザー・テストデータを引き継がず、Supabase DBに対して`alembic upgrade head`を実行してクリーンスタートします。
  - データ移行スクリプトは今回追加しません。

### 6.5 開発環境 (HTTP) と 本番環境 (HTTPS) の差異
- **懸念**: 開発環境の`http://192.168.100.2:...`では、ログインパスワードとトークンがLAN上を平文で通ります。
- **対策**: 当面はテストユーザーだけを使用し、外部公開しません。実ユーザーを扱う前にリバースプロキシでHTTPS化します。Supabase AuthのSite URLと許可Redirect URLは実際のStreamlit URLだけへ限定します。

### 6.6 Supabase Data APIによる意図しない公開
- **懸念**: Alembicで作成するアプリ所有テーブルは既定で`public`スキーマに入り、Supabase Data APIの公開対象になり得ます。
- **対策**: FastAPIを唯一のデータアクセス経路とし、CloudではData APIを無効化します。代替措置が必要な場合は`anon`・`authenticated`権限の剥奪とRLSの既定拒否を自動チェックに含めます。

---

## 7. 段階的実装ロードマップ

| フェーズ | 状況 | 作業内容 | 対象ファイル |
| :--- | :--- | :--- | :--- |
| **Phase 1**<br>開発用スリム版 Docker 環境構築 | 実機確認済み | ・公式self-hosted v0.8.0のイメージを固定<br>・5サービスすべてhealthy<br>・Auth/JWKS・Studio認証・DB接続を確認 | `deploy/supabase/` |
| **Phase 2**<br>DB接続 & マイグレーション検証 | 設定済み・実環境確認待ち | ・`.env.example`をSupabaseへ更新<br>・既存Alembicを継続利用<br>・Data APIはComposeから除外 | `.env.example`<br>`migrations/` |
| **Phase 3**<br>FastAPIの汎用OIDC対応 | 実装・自動テスト済み | ・既存AuthenticatorをRS256/ES256の単一選択へ拡張<br>・ES256検証テストを追加 | `src/config.py`<br>`src/api/auth.py`<br>`tests/test_auth.py` |
| **Phase 4**<br>StreamlitのSupabaseログイン & 自動更新 | 実装・自動テスト済み | ・既存`httpx`でEmail/Passwordログイン<br>・利用者別にaccess/refresh tokenを保持・更新<br>・タブ終了後の再ログイン制約を表示 | `src/supabase_auth.py`<br>`src/web_app.py`<br>`tests/test_supabase_auth.py` |
| **Phase 5**<br>結合テスト & クリーンアップ | 一部完了 | ・既存テスト全件実行とドキュメント更新<br>・実SupabaseでのE2Eは未実施<br>・Keycloakはロールバック用に保持 | `tests/`<br>`deploy/keycloak/`<br>`README.md` |

---

## 8. Codex への実装指示書（プロンプト・タスクリスト）

実装を Codex へ依頼する際は、以下のプロンプトとタスクリストを使用してください。

````markdown
### Codex への指示プロンプト例

あなたは優秀なソフトウェアエンジニアです。
プロジェクト `langgraph-ollama-agent` において、認証基盤を Keycloak から Supabase へ切り替える実装を行ってください。
詳細な設計仕様および懸念対策は `docs/supabase_migration_plan.md` を参照してください。

#### 【実装タスク】

1. **開発用スリム版 Docker Compose の作成 (`deploy/supabase/`)**:
   - Supabase公式self-hostedリリースを固定し、PostgreSQL、GoTrue、API Gateway、Studio、Postgres Metaだけを残してください。
   - 署名鍵と公開キーは公式v0.8.0の方式に合わせた`generate-env.mjs`で環境ごとに生成し、固定値をコミットしないでください。

2. **依存関係 (`pyproject.toml`)**:
   - 既存の`httpx`を使用し、Supabase SDKは追加しないでください。

3. **設定クラスの拡張 (`src/config.py`)**:
   - `auth_mode`は既存の`Literal["disabled", "oidc"]`を維持してください。
   - `oidc_jwt_algorithm`（RS256/ES256の単一選択）、`supabase_url`、`supabase_publishable_key`を追加してください。

4. **JWT認証検証の拡張 (`src/api/auth.py`)**:
   - 新しいSupabase専用クラスを作らず、既存`OpenIDConnectAuthenticator`を設定されたRS256またはES256でJWKS検証できるよう拡張してください。
   - 署名、`iss`、`aud`（`"authenticated"`）、`exp`、`iat`、`sub`を検証し、`AuthenticatedPrincipal(subject=sub, display_name=...)`を返却してください。

5. **FastAPI Lifespan / DI の対応 (`src/api/app.py`, `src/api/runtime.py`)**:
   - Supabase専用のDI分岐は追加せず、既存の`AUTH_MODE=oidc`経路を利用してください。

6. **Streamlit UI の認証 & 自動リフレッシュ更新 (`src/web_app.py`)**:
   - `AUTH_MODE=oidc`かつSupabase設定時にEmail/Passwordログインを表示し、取得した`access_token`を`AgentApiClient`へ渡してください。
   - access token、refresh token、有効期限を利用者ごとの`st.session_state`に保持し、refresh時は返されたトークン一式へ原子的に置き換えてください。
   - Supabaseクライアントを全利用者で共有するキャッシュへ保存しないでください。

7. **自動テストの更新 (`tests/test_auth.py`, `tests/test_api.py`)**:
   - RS256とES256のJWTを生成し、正常系・異常系（期限切れ、署名不正、`iss`不正、`aud`不正、設定外アルゴリズム）を検証してください。
   - `uv run pytest` がすべてパスすることを確認してください。

8. **環境設定・ドキュメント更新**:
   - `.env.example` に Supabase 設定のサンプルを追加してください。
   - Supabase Data APIがアプリ所有テーブルを公開しない設定と確認手順を記載してください。
   - `deploy/keycloak/` は実環境確認が終わるまでロールバック用に保持してください。
````

---

## 9. ロールバック計画

万が一移行中に予期せぬ障害が発生した場合の復旧手順です。

1. **設定ロールバック**:
   - `.env`の`OIDC_ISSUER_URL`、`OIDC_AUDIENCE`、`OIDC_JWKS_URL`、`OIDC_JWT_ALGORITHM`をKeycloak用の値へ戻す。認証なしのローカル確認だけは`AUTH_MODE=disabled`を使用する。
   - `DATABASE_URL` を以前の自前 PostgreSQL 接続文字列に戻す。
2. **コードロールバック**:
   - Git にて移行前のブランチ/コミットにチェックアウト。
   - Keycloak コンテナ（`deploy/keycloak/`）を再起動。
3. **データ保全**:
   - Supabase への移行中も、旧 PostgreSQL のデータベースファイルおよび Docker ボリュームは削除せず保持しておくこと。

---

## 10. 参照資料

- [Supabase JWT Signing Keys](https://supabase.com/docs/guides/auth/signing-keys)
- [Supabase JWT検証](https://supabase.com/docs/guides/auth/jwts)
- [Self-hosted Supabaseの非対称署名鍵](https://supabase.com/docs/guides/self-hosting/self-hosted-auth-keys)
- [Supabase Postgres接続方式](https://supabase.com/docs/guides/database/connecting-to-postgres)
- [Supabase Data APIの保護](https://supabase.com/docs/guides/api/securing-your-api)
- [Supabase Authのセッション](https://supabase.com/docs/guides/auth/sessions)
