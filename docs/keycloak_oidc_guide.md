# Keycloak・OIDC構築ガイド

## 1. 構成

KeycloakはDockerサーバー`192.168.100.2`のDockgeで動かします。アプリ用PostgreSQLとは分離し、Keycloak専用PostgreSQLを同じStack内で使用します。

リポジトリの構成ファイル：

```text
deploy/keycloak/
├── compose.yaml
├── .env.example
├── README.md
└── realm/langgraph-realm.json
```

このComposeはLAN内検証用で、Keycloak 26.7.2の`start-dev`を使用します。

## 2. Dockgeで起動

1. `deploy/keycloak`以下をDockgeのStackディレクトリへ配置します。
2. `.env.example`を参考に次のStack環境変数を設定します。

```ini
KEYCLOAK_ADMIN_USERNAME=admin
KEYCLOAK_ADMIN_PASSWORD=十分に長い管理者パスワード
KEYCLOAK_DB_USERNAME=keycloak
KEYCLOAK_DB_PASSWORD=十分に長いDBパスワード
KEYCLOAK_HOSTNAME=http://192.168.100.2:8080
```

3. Stackを起動します。
4. `http://192.168.100.2:8080/admin/`を開き、管理者でログインします。
5. Realm一覧で`langgraph`が作成済みであることを確認します。
6. `langgraph` RealmのUsersからテスト利用者を作成し、Credentialsでパスワードを設定します。

Realm importは初回だけ実行され、同名Realmが存在する場合はスキップされます。

## 3. 登録済みクライアント

| Client ID | 用途 | フロー | audience |
|---|---|---|---|
| `langgraph-api` | FastAPI Resource Server | ログイン不可 | 検証対象 |
| `langgraph-web` | WEBアプリ | Authorization Code + PKCE S256 | `langgraph-api`を追加 |
| `langgraph-mobile` | iOS/Android | Authorization Code + PKCE S256 | `langgraph-api`を追加 |
| `langgraph-streamlit` | Streamlitサーバー | Authorization Code + client secret | `langgraph-api`を追加 |

WEB・モバイルは公開クライアントです。クライアントシークレットをブラウザやアプリへ埋め込まないでください。WEB開発用には`http://localhost:3000/*`と`http://localhost:5173/*`（および127.0.0.1）を登録しています。Keycloakではポート番号の`*`を使用せず、利用するポートを明示します。実際のWEB URLが確定したら、Valid redirect URIsを必要最小限へ変更します。

StreamlitはPythonサーバー上でOIDCコード交換を行うため、専用の機密クライアントを使用します。既存Realmには管理画面から次の内容で追加します。

1. Client IDを`langgraph-streamlit`、Client authenticationをOn、Standard flowをOnにする
2. Valid redirect URIsへ`http://localhost:8501/oauth2callback`と`http://127.0.0.1:8501/oauth2callback`を登録する
3. Client scopes → `langgraph-streamlit-dedicated`でAudience Mapperを作成し、Included Client Audienceを`langgraph-api`、Add to access tokenをOnにする。Included Custom Audienceは空欄、Add to ID tokenはOffにする
4. Credentialsタブで生成されたシークレットを確認する
5. `.streamlit/secrets.toml.example`をコピーし、シークレットを`.streamlit/secrets.toml`へ設定する

`.streamlit/secrets.toml`はGit対象外です。アクセストークンを利用するため、`expose_tokens = "access"`を削除しないでください。Realm importは既存Realmを上書きしないため、すでに構築済みの場合はこの手動追加が必要です。

Audience Mapper変更後は、古いアクセストークンを破棄するためStreamlitから一度ログアウトして再ログインします。ログインには成功するもののAPIが`401 invalid_access_token`を返す場合は、アクセストークンへ`langgraph-api` audienceが追加されているかを最初に確認してください。

## 4. APIの設定

プロジェクト直下の`.env`へ追加します。

```ini
AUTH_MODE=oidc
OIDC_ISSUER_URL=http://192.168.100.2:8080/realms/langgraph
OIDC_AUDIENCE=langgraph-api
OIDC_JWKS_CACHE_SECONDS=300
OIDC_HTTP_TIMEOUT_SECONDS=5
OIDC_CLOCK_SKEW_SECONDS=30
```

APIを再起動すると、`/health`と`/ready`を除く`/v1/*`でBearerアクセストークンが必須になります。APIはOIDC DiscoveryからJWKS URLを取得し、署名鍵をキャッシュします。

検証項目：

- アルゴリズム：RS256のみ
- 署名鍵：Keycloak JWKSの`kid`に一致
- `iss`：`OIDC_ISSUER_URL`と完全一致
- `aud`：`langgraph-api`を含む
- 必須claim：`exp`、`iat`、`sub`

## 5. ユーザー所有権

JWTの`sub`を`users.external_subject`へ保存し、内部UUIDへ対応付けます。会話・メモの検索条件には内部`user_id`を必ず含めます。LangGraphのメモツールにも同じ`user_id`を渡すため、会話APIだけでなくツール経由の読み書きも利用者間で分離されます。

他ユーザーが会話IDを知っていても、APIは`404 conversation_not_found`を返します。

## 6. 確認

認証なしの確認：

```bash
curl -i http://127.0.0.1:8000/health
curl -i http://127.0.0.1:8000/v1/conversations
```

1つ目は`200`、2つ目は`401 authentication_required`になることを確認します。

アクセストークン取得後：

```bash
curl -i http://127.0.0.1:8000/v1/conversations \
  -H 'Authorization: Bearer アクセストークン'
```

正しいトークンでは`200`、audienceや期限が不正なトークンでは`401 invalid_access_token`になります。

## 7. 本番化前の必須事項

現在のKeycloak ComposeはHTTP・`start-dev`のLAN内検証用です。外部公開前に以下を行います。

- Keycloakをproduction modeで起動する
- HTTPS証明書と固定hostnameを設定する
- 管理画面のアクセス元を制限する
- Keycloak DBのバックアップ・復旧を整備する
- WEBのredirect URIとWeb Originを本番URLだけに限定する
- API側の`CORS_ALLOWED_ORIGINS`をKeycloakのWeb Originsと同じ本番Originへ限定する
- PostgreSQL共有レート制限とJSONログマスキングを有効にする
