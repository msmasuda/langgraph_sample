# Web・モバイル向けAPI利用ガイド

## 1. 起動

Ollama、PostgreSQL、Keycloakを起動し、プロジェクト直下の`.env`へ次を設定します。

```ini
AUTH_MODE=oidc
OIDC_ISSUER_URL=http://192.168.100.2:8080/realms/langgraph
OIDC_AUDIENCE=langgraph-api
```

その後、APIサーバーを起動します。

```bash
uv run uvicorn src.api.app:app --host 127.0.0.1 --port 8000
```

- OpenAPI UI: `http://127.0.0.1:8000/docs`
- OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`

## 2. エンドポイント

`/health`と`/ready`は監視用に認証不要です。その他の`/v1/*`は`Authorization: Bearer <access_token>`が必要です。

| メソッド | パス | 用途 |
|---|---|---|
| GET | `/health` | APIプロセスの稼働確認 |
| GET | `/ready` | Ollamaを含む準備状態の確認 |
| GET | `/v1/models` | Ollamaの利用可能モデル一覧 |
| POST | `/v1/conversations` | 新しい会話IDの作成 |
| GET | `/v1/conversations` | 会話一覧 |
| GET/PATCH/DELETE | `/v1/conversations/{conversation_id}` | 会話詳細・更新・削除 |
| GET | `/v1/conversations/{conversation_id}/messages` | 永続化された会話履歴 |
| POST | `/v1/conversations/{conversation_id}/cancel` | 実行中メッセージのキャンセル要求 |
| POST | `/v1/conversations/{conversation_id}/messages` | 完了後にJSONで回答 |
| POST | `/v1/conversations/{conversation_id}/messages/stream` | SSEで逐次回答 |
| GET/POST | `/v1/conversations/{conversation_id}/notes` | メモ一覧・作成 |
| PATCH/DELETE | `/v1/conversations/{conversation_id}/notes/{note_id}` | メモ更新・削除 |

## 3. 基本的な利用手順

### 会話を作成する

```bash
curl -X POST http://127.0.0.1:8000/v1/conversations \
  -H 'Authorization: Bearer アクセストークン'
```

応答例：

```json
{
  "id": "00000000-0000-0000-0000-000000000000",
  "created_at": "2026-08-30T00:00:00Z"
}
```

### JSONで回答を受け取る

```bash
curl -X POST \
  http://127.0.0.1:8000/v1/conversations/会話ID/messages \
  -H 'Authorization: Bearer アクセストークン' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: クライアントで生成した一意なキー' \
  -d '{"content":"東京の現在時刻を教えてください"}'
```

### SSEで逐次回答を受け取る

```bash
curl -N -X POST \
  http://127.0.0.1:8000/v1/conversations/会話ID/messages/stream \
  -H 'Authorization: Bearer アクセストークン' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: クライアントで生成した一意なキー' \
  -d '{"content":"1+1を計算してください"}'
```

SSEのイベント種別：

| イベント | 内容 |
|---|---|
| `message.started` | メッセージ処理の開始 |
| `assistant.delta` | 回答テキストの差分 |
| `tool.started` | ツール実行の開始 |
| `tool.completed` | ツール実行の完了と結果 |
| `message.completed` | 最終回答と正常完了 |
| `message.failed` | 処理中に発生した安全なエラー |

イベント待ちの間は`: stream-heartbeat`というSSEコメントを約0.5秒間隔で送信します。通常のSSEクライアントはコメントとして無視できます。Streamlitクライアントは回答待ち中も停止操作を処理するために利用します。

## 4. 認証

- WEBはKeycloak client ID `langgraph-web`、モバイルは`langgraph-mobile`を使用します。
- どちらもAuthorization Code + PKCE（S256）を使用し、クライアントシークレットをアプリへ保存しません。
- APIへ送るのはIDトークンではなくアクセストークンです。
- APIはRS256署名、発行者、`langgraph-api` audience、有効期限、`sub`を検証します。
- `sub`は内部ユーザーUUIDへ対応付けられます。同じ利用者は再ログイン後も同じ会話へアクセスできます。
- 他ユーザーの会話IDへアクセスした場合、存在の推測を防ぐため`404 conversation_not_found`を返します。
- アクセストークンの更新は各WEB・モバイルOIDCライブラリへ任せ、401受信時は一度だけ更新・再送してください。

## 5. クライアント実装上の注意

- 各リクエストにクライアント側で一意な`Idempotency-Key`を設定してください。同じ会話・同じキー・同じ本文を再送すると、完了済みの応答を再利用します。
- 同じキーで異なる本文を送ると`409 idempotency_conflict`を返します。
- 同じ会話を同時に実行すると`409 conversation_busy`を返します。先の処理が完了してから再試行してください。
- 応答ヘッダーの`X-Request-ID`を障害調査用に記録してください。クライアントが安全な形式の値を送った場合は同じ値を返します。
- SSEでは`message.completed`を受信して成功と判断し、`message.failed`または通信切断時は失敗として扱ってください。
- `: stream-heartbeat`はデータイベントではないため、Web・モバイルクライアントは無視して構いません。
- クライアントが接続を閉じるとAPI側も処理を中断し、会話の実行ロックを解放します。

## 6. API保護

### CORS

```ini
CORS_ALLOWED_ORIGINS=http://localhost:3000,http://localhost:5173
CORS_ALLOW_CREDENTIALS=true
CORS_MAX_AGE_SECONDS=600
```

Originはカンマ区切りで完全な値を列挙します。`*`は設定できません。WEBアプリの本番URLが確定したらlocalhostを削除し、本番HTTPS Originだけに限定します。

### レート制限

```ini
RATE_LIMIT_ENABLED=true
RATE_LIMIT_WINDOW_SECONDS=60
RATE_LIMIT_IP_REQUESTS=120
RATE_LIMIT_USER_REQUESTS=60
```

`/v1/*`では認証前にIP単位、認証後にユーザー単位の制限を適用します。PostgreSQL利用時は複数APIプロセスでカウンターを共有します。レスポンスの`RateLimit`と`RateLimit-Policy`で残数・リセット時期・上限・窓時間を確認でき、超過時は`429 rate_limit_exceeded`と`Retry-After`を返します。従来の`RateLimit-Limit`、`RateLimit-Remaining`、`RateLimit-Reset`も移行期間の互換性のため返します。OPTIONSプリフライトはカウントしません。

リバースプロキシの`X-Forwarded-For`を利用する場合だけ、APIへ直接接続するプロキシIPを`TRUSTED_PROXY_IPS`へ列挙してください。未登録の接続元から届いた転送ヘッダーは信用しません。

### JSONログ

```ini
API_JSON_LOGGING=true
API_LOG_LEVEL=INFO
```

アクセスログにはリクエストID、メソッド、パス、ステータス、処理時間、ハッシュ化した接続元・ユーザーだけを記録します。リクエスト本文、Authorization、Cookie、トークン、メールアドレス、ツール引数は記録しません。

メッセージは最大20,000文字、Web検索語は最大500文字、検索結果は最大10件です。エージェントのツール呼び出し回数にも`MAX_TOOL_CALLS`の上限を適用します。現行APIは添付ファイルを受け付けないため、将来追加する際に受信サイズ上限を設けます。

### 副作用ツール

```ini
APPROVAL_REQUIRED_TOOLS=send_email,create_calendar_event,delete_file,execute_payment
```

列挙された外部副作用ツールは、承認・再開実行器が実装されるまでLangGraphへの登録自体を拒否します。引数プレビューは値を表示せず、引数名と型だけを返す設計です。

## 7. PostgreSQL永続化

PostgreSQL設定時は、会話ID、会話履歴、メモ、ツール実行、利用記録、冪等性応答、実行リースを永続化します。API再起動後も会話一覧と履歴を復元でき、複数APIプロセスから同じデータを利用できます。

`AUTH_MODE=oidc`では、JWTの`sub`ごとに会話とメモを分離します。`AUTH_MODE=disabled`はローカル互換専用です。CORSとレート制限はAPIで適用しますが、HTTPSは本番のリバースプロキシで構成してください。

## 8. Streamlit Web UI

Streamlitは`WEB_API_BASE_URL`の共通APIだけを使用し、LangGraphやデータベースへ直接接続しません。

```ini
WEB_API_BASE_URL=http://127.0.0.1:8000
WEB_API_TIMEOUT_SECONDS=180
```

OIDC有効時は`.streamlit/secrets.toml.example`を`.streamlit/secrets.toml`へコピーし、Keycloakの`langgraph-streamlit`クライアントシークレットを設定します。アクセストークンはStreamlitサーバー内のBearer認証にだけ使用し、ブラウザ画面、ログ、URLへ表示しません。

会話IDは`conversation`クエリパラメータへ保存しますが、APIは必ずJWT所有者を確認するため、他ユーザーがURLを知っていても会話を取得できません。回答はSSEで逐次表示し、ツール引数とツール出力はWeb UIへ表示しません。
