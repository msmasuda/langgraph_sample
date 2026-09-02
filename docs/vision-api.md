# 汎用画像解析API

`POST /v1/vision/analyze`は、Web・モバイルクライアントから画像1枚と任意の指示を受け取り、Ollamaの画像対応モデルで解析する独立APIです。料理固有のプロンプトや項目は持たず、必要な指示とJSON Schemaはクライアントが指定します。

## 設計

- LangGraphの会話、チェックポイント、メモ、Web検索ツールから分離した`VisionService`を使用します。
- 画像、プロンプト、モデルの生出力は会話履歴やPostgreSQLへ保存しません。
- JPEG、PNG、WebPを実デコードして検証し、MIME偽装、破損画像、アニメーション、過大なファイル・縦横・ピクセル数を拒否します。
- EXIFなどのメタデータを除去して再エンコードした画像だけをOllamaへ送ります。
- URL画像は受け付けないため、サーバーから外部URLを取得するSSRF経路はありません。
- JSON Schemaはサイズ、深さ、ノード数、プロパティ数、配列、文字列を制限し、`$ref`や未対応キーワードを拒否します。モデル出力もサーバー側で再検証します。
- Bearer認証、IP・ユーザー単位の共通レート制限、画像解析専用レート制限、`X-Request-ID`、共通エラー形式を適用します。

## モデル準備

画像対応モデルをOllamaへ導入します。例：

```bash
ollama pull qwen3.5:9b-mlx
```

`.env`へ既定モデルを設定します。クライアントに`model`指定を許可する場合だけ、追加モデルを許可リストへ列挙します。

```ini
VISION_MODEL=qwen3.5:9b-mlx
VISION_ALLOWED_MODELS=
```

APIは解析前にOllamaの`/api/show`を呼び、モデルが存在し、`capabilities`に`vision`を含むことを確認します。モデル名だけから画像対応可否を推測しません。

## リクエスト

```http
POST /v1/vision/analyze
Content-Type: multipart/form-data
Authorization: Bearer <access_token>
```

| フィールド | 必須 | 内容 |
|---|---:|---|
| `image` | はい | JPEG、PNG、WebPのいずれか1枚 |
| `prompt` | はい | 画像に対して行う指示 |
| `response_schema` | いいえ | JSON Schemaを表すJSON文字列 |
| `model` | いいえ | `VISION_ALLOWED_MODELS`で許可した画像対応モデル |

JSON Schemaなしの例：

```bash
curl -X POST http://127.0.0.1:8000/v1/vision/analyze \
  -H 'Authorization: Bearer アクセストークン' \
  -H 'X-Request-ID: mobile-vision-001' \
  -F 'image=@./sample.jpg;type=image/jpeg' \
  -F 'prompt=この画像について日本語で簡潔に説明してください。'
```

```json
{
  "content": "画像についての解析結果",
  "model": "qwen3.5:9b-mlx"
}
```

MealLog AI向け構造化応答の例：

```bash
curl -X POST http://127.0.0.1:8000/v1/vision/analyze \
  -H 'Authorization: Bearer アクセストークン' \
  -F 'image=@./meal.jpg;type=image/jpeg' \
  -F 'prompt=この画像に写っている料理を日本語で推定してください。確認できない食材を断定しないでください。' \
  -F 'response_schema={"type":"object","properties":{"dishName":{"type":"string"},"candidates":{"type":"array","items":{"type":"string"}},"ingredients":{"type":"array","items":{"type":"string"}}},"required":["dishName","candidates","ingredients"],"additionalProperties":false}'
```

```json
{
  "content": {
    "dishName": "推定した料理名",
    "candidates": ["別の候補"],
    "ingredients": ["画像から確認できる食材"]
  },
  "model": "qwen3.5:9b-mlx"
}
```

モバイルアプリでは、画像バイト列をBase64へ変換せずmultipartのファイルパートとして送信してください。Base64変換とOllama固有形式への変換はAPI内部で行います。

## エラー

すべてのエラーには`X-Request-ID`レスポンスヘッダーと、次の共通形式が付きます。

```json
{
  "error": {
    "code": "invalid_image",
    "message": "有効な画像ファイルを指定してください。",
    "request_id": "リクエストID"
  }
}
```

| コード | 主なHTTP状態 | 意味 |
|---|---:|---|
| `invalid_image` | 400 | 空、破損、画像以外、アニメーション |
| `unsupported_image_type` | 400 | 未許可形式またはMIME偽装 |
| `image_too_large` | 413 | バイト数、縦横、ピクセル数超過 |
| `invalid_prompt` | 400 | 指示なし、文字数超過 |
| `invalid_response_schema` | 400 | 不正、未対応、複雑すぎるスキーマ |
| `vision_model_unavailable` | 503 | 未導入、未許可、画像非対応、Ollama停止 |
| `vision_timeout` | 504 | 解析タイムアウト |
| `schema_validation_failed` | 502 | モデル出力が指定スキーマに不適合 |
| `rate_limit_exceeded` | 429 | IP、ユーザー、画像解析の上限超過 |

内部例外、モデルの生レスポンス、ファイル名・一時パス、トークンは返しません。

## 設定一覧

| 環境変数 | 既定値 | 内容 |
|---|---:|---|
| `VISION_MODEL` | `qwen3.5:9b-mlx` | 既定の画像対応モデル |
| `VISION_ALLOWED_MODELS` | 空 | クライアント指定を許可する追加モデル |
| `VISION_TIMEOUT_SECONDS` | `120` | Ollama呼び出し上限秒数 |
| `VISION_MAX_IMAGE_BYTES` | `10485760` | 画像最大バイト数 |
| `VISION_ALLOWED_MIME_TYPES` | JPEG/PNG/WebP | 許可MIMEタイプ |
| `VISION_MAX_PROMPT_CHARS` | `5000` | 指示の最大文字数 |
| `VISION_MAX_SCHEMA_BYTES` | `16384` | JSON Schema最大バイト数 |
| `VISION_MAX_RESPONSE_BYTES` | `1048576` | Ollamaレスポンス最大バイト数 |
| `VISION_MAX_IMAGE_WIDTH` | `8192` | 最大横幅 |
| `VISION_MAX_IMAGE_HEIGHT` | `8192` | 最大縦幅 |
| `VISION_MAX_IMAGE_PIXELS` | `25000000` | 最大総ピクセル数 |
| `VISION_MAX_SCHEMA_DEPTH` | `8` | スキーマ・出力の最大深さ |
| `VISION_MAX_SCHEMA_PROPERTIES` | `100` | スキーマノード・プロパティ数上限 |
| `VISION_MAX_ARRAY_ITEMS` | `100` | 構造化出力の配列要素上限 |
| `VISION_MAX_OUTPUT_STRING_CHARS` | `10000` | 構造化出力内の文字列上限 |
| `VISION_RATE_LIMIT_REQUESTS` | `10` | 画像解析のユーザー別上限 |
| `VISION_RATE_LIMIT_WINDOW_SECONDS` | `60` | 画像解析レート制限の窓（秒） |

## ローカル確認

認証処理を切り分けるローカル検証だけは、`.env`で`AUTH_MODE=disabled`にしてBearerヘッダーを省略できます。LAN・インターネットへ公開する環境では必ず`AUTH_MODE=oidc`を使用してください。

実モデル疎通では、APIを起動後に上記curlを実行します。`vision_model_unavailable`の場合は、`ollama list`でモデル名を確認し、OllamaがAPIサーバーから到達できることを確認してください。

## 保存・ログ・削除

- 画像、プロンプト、解析結果はデータベースやLangGraphチェックポイントへ保存しません。
- アクセスログはパス、状態、所要時間、リクエストID、ハッシュ化したIP・ユーザー識別子だけを記録します。
- 画像、Base64、プロンプト、モデル出力、Authorizationはログの機密キーとして除去します。
- 通常はメモリ上で処理します。multipartパーサーが大きなファイルを一時領域へ退避した場合も、成功・失敗を問わずアップロードを明示的に閉じて削除します。
- Ollamaへ送信した画像の保持方法はOllamaを稼働させる環境の運用方針にも従ってください。
