# Keycloak（Dockge）開発環境

この構成は、LAN内でフェーズ4-Aを検証するためのKeycloak開発環境です。Keycloak本体と専用PostgreSQLを起動し、`langgraph` Realm、API、WEB、モバイル用クライアントを初回起動時に作成します。

## Dockgeでの起動

1. `compose.yaml`、`.env.example`、`realm/langgraph-realm.json`を同じ構成でDockgeのStackへ配置します。
2. `.env.example`を参考にStack環境変数を設定し、管理者・DBパスワードを十分に長い値へ変更します。
3. `KEYCLOAK_HOSTNAME`はクライアントとAPIの両方から到達できるURLにします。現在の想定は`http://192.168.100.2:8080`です。
4. Stackを起動し、`http://192.168.100.2:8080/admin/`へアクセスします。
5. `langgraph` Realmにテストユーザーを追加し、パスワードを設定します。

Realm importは同名Realmがすでに存在するとスキップされます。JSON変更後に既存Realmへ反映する場合は、管理画面で対応する設定を変更するか、検証データを確認したうえでRealmを作り直してください。

## API側の設定

プロジェクト直下の`.env`へ追加します。

```ini
AUTH_MODE=oidc
OIDC_ISSUER_URL=http://192.168.100.2:8080/realms/langgraph
OIDC_AUDIENCE=langgraph-api
```

設定後にAPIを再起動します。`/health`と`/ready`は監視用に認証不要、`/v1/*`はBearerアクセストークン必須です。

## クライアント設定

| 用途 | Client ID | 方式 | リダイレクトURI |
|---|---|---|---|
| WEB | `langgraph-web` | Authorization Code + PKCE (S256) | 開発用localhostの3000・5173番を登録済み |
| モバイル | `langgraph-mobile` | Authorization Code + PKCE (S256) | `langgraph://oauth/callback` |
| API | `langgraph-api` | Resource Server | ログインフローなし |

WEB・モバイルのアクセストークンには`langgraph-api` audienceが追加されます。APIはRS256署名、`iss`、`aud`、`exp`、`iat`、`sub`を検証します。クライアントシークレットはWEB・モバイルアプリへ埋め込みません。

## 本番利用前の注意

このComposeは`start-dev`とHTTPを使用するLAN内検証用です。本番では、HTTPS、固定hostname、リバースプロキシ、バックアップ、Keycloakのproduction mode、管理画面の公開制限を別途構成してください。
