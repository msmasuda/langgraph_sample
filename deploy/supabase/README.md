# ローカルSupabase（Dockge）

Supabase self-hosted v0.8.0のイメージを固定し、LangGraphで使うPostgreSQL、Auth、Studio、Postgres Meta、API Gatewayだけを起動します。PostgRESTを含めていないため、Data APIからアプリ所有テーブルへはアクセスできません。

## Dockge Web UIから起動

Docker Compose 2.23.1以上が必要です。Gateway設定とDBユーザー設定は`compose.yaml`へ埋め込んであるため、Dockgeサーバーへの補助ファイル配置は不要です。

1. Dockgeで新しいStack名を入力し、`compose.yaml`の内容をCompose欄へ貼り付けます。
2. 信頼できるローカルPCで次を実行し、表示された内容をDockgeの`.env`欄へ貼り付けます。

```bash
node deploy/supabase/generate-env.mjs --stdout
```

3. `.env`欄のURLと公開ポートを必要に応じて変更し、「Deploy」を実行します。

DockgeのWeb UIはSupabaseの署名鍵を生成しません。`generate-env.mjs --stdout`は秘密値をファイルへ保存せず、そのまま`.env`欄へ貼り付けられる形式で出力します。サーバー上へ通常の`.env`ファイルを作る場合は`--stdout`を外してください。既存ファイルは上書きしません。

- Studio / Gateway: `http://192.168.100.2:8000`
- Auth: `http://192.168.100.2:8000/auth/v1`
- JWKS: `http://192.168.100.2:8000/auth/v1/.well-known/jwks.json`
- PostgreSQL: `192.168.100.2:15432`

2026-09-08の実機では既存設定を引き継ぎ、`POSTGRES_HOST_PORT=15432`で稼働しています。アプリのDB接続URLもこのポートに合わせてください。

Studioは`.env`の`DASHBOARD_USERNAME`と自動生成された`DASHBOARD_PASSWORD`で保護されます。Authentication画面からテストユーザーを追加してください。開発用のため、メール確認は既定で省略しています。

### 旧Composeで`Permission denied`になった場合

DockgeのCompose欄を最新の`compose.yaml`へ置き換え、`generate-env.mjs --stdout`で新しい秘密値を生成して`.env`欄も置き換えてからDeployしてください。ログへ表示された`POSTGRES_PASSWORD`は再利用しないでください。

最新Composeは新しい`postgres-data-v2`ボリュームでDBを初期化し、その初期化処理内でAuth用DBパスワードを設定します。旧`db-data`と`postgres-data`ボリュームは保持しています。既に修正版で稼働しているDBに対して、この再初期化手順を繰り返す必要はありません。

Auth用のSQLはDBコンテナ起動時に読み取り可能なファイルとして生成します。初期化完了後の`postgres`では予約ロールを変更できないため、後付けの`db-init`サービスは使用しません。Kongには設定内の鍵を環境変数としても渡し、`.env`の鍵更新時にコンテナが再作成されるようにしています。

正常時は`db`・`auth`・`studio`・`meta`・`api-gw`の5サービスが稼働状態になります。

## アプリ設定

Stackの`.env`から`POSTGRES_PASSWORD`と`SUPABASE_PUBLISHABLE_KEY`を参照し、プロジェクト直下の`.env`へ設定します。

```ini
AUTH_MODE=oidc
OIDC_ISSUER_URL=http://192.168.100.2:8000/auth/v1
OIDC_AUDIENCE=authenticated
OIDC_JWKS_URL=http://192.168.100.2:8000/auth/v1/.well-known/jwks.json
OIDC_JWT_ALGORITHM=ES256
SUPABASE_URL=http://192.168.100.2:8000
SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
DATABASE_URL=postgresql+asyncpg://postgres:POSTGRES_PASSWORD@192.168.100.2:15432/postgres
CHECKPOINT_DATABASE_URL=postgresql://postgres:POSTGRES_PASSWORD@192.168.100.2:15432/postgres
```

初回だけアプリ所有テーブルを作成します。

```bash
uv run alembic upgrade head
```

## 確認

2026-09-08にDockge実機で5サービスすべての`healthy`、JWKS（ES256）、新しいキーによるAuth health/settings/管理APIのHTTP 200、旧キーの401、StudioのBasic認証前401・認証後200、DB接続とAuthマイグレーション76件の完了を確認しました。アプリでのログイン・会話保存は別途結合確認が必要です。

```bash
curl http://192.168.100.2:8000/auth/v1/.well-known/jwks.json
```

応答の公開鍵に`"alg":"ES256"`が含まれることを確認します。`docker compose ps`に`rest`サービスがないことが、Data APIを起動していないことの確認になります。

この構成はLAN内開発用のHTTP構成です。インターネットへ公開せず、本番ではSupabase Cloudを使用してください。

構成元: [Supabase self-hosted v0.8.0](https://github.com/supabase/supabase/tree/self-hosted/v0.8.0/docker)
