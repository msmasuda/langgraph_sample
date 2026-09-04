# PostgreSQL構築・移行ガイド

## 1. 構成

- API・Ollama: `/Users/mauda/Projects/langgraph_sample`を実行するMac
- PostgreSQL: `192.168.100.2`上のDocker / Dockge
- PostgreSQLポート: TCP `5432`
- データベース名・ユーザー名: `langgraph`

リポジトリの`deploy/postgres/compose.yaml`をDockgeへ登録し、同じディレクトリの`.env`へ`POSTGRES_PASSWORD`を設定します。データは名前付きボリューム`langgraph_postgres_data`へ保存されます。

PostgreSQLはインターネットへ公開せず、ファイアウォールではAPIを動かすMacからTCP 5432への接続だけを許可してください。

## 2. API側の接続設定

プロジェクトの`.env`へ次を設定します。`.env`はGit管理対象外です。

```ini
DATABASE_URL=postgresql+asyncpg://langgraph:パスワード@192.168.100.2:5432/langgraph
CHECKPOINT_DATABASE_URL=postgresql://langgraph:パスワード@192.168.100.2:5432/langgraph
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=10
DATABASE_CONNECT_TIMEOUT_SECONDS=10
EXECUTION_LEASE_SECONDS=300
CONVERSATION_RETENTION_DAYS=90
```

## 3. 初期化と起動

```bash
uv sync
uv run alembic upgrade head
uv run uvicorn src.api.app:app --host 127.0.0.1 --port 8000
```

Alembicはアプリ所有のテーブルを作成します。LangGraphチェックポイント用テーブルはAPI起動時に`AsyncPostgresSaver.setup()`が初期化・更新します。

`GET /ready`が次の内容を返せば、OllamaとPostgreSQLの両方が利用可能です。

```json
{"status":"ready","ollama":true,"database":true}
```

## 4. データと複数プロセス

- `conversations`: 会話タイトル、状態、所有者、保存期限、LangGraphスレッドID
- `notes`: APIとエージェントツールが共有する会話メモ
- `tool_executions` / `usage_records`: ツール結果と処理時間・文字数
- `idempotency_records`: 完了済みJSON・SSE応答
- `conversation_executions`: 期限付き実行リースとキャンセル要求
- LangGraphテーブル: チェックポイント、書き込み、バイナリデータ

期限付き実行リースにより、複数APIプロセスから同じ会話を同時実行しません。処理プロセスが異常終了してもリース期限後に再実行できます。

## 5. SQLiteからの移行

作業前に`data/checkpoints.sqlite`と`data/notes.sqlite`、PostgreSQLボリュームのバックアップを取得してください。

```bash
# 対象件数だけ確認
uv run python -m src.db.migrate_sqlite

# 実際に移行
uv run python -m src.db.migrate_sqlite --apply
```

UUIDではない既存スレッドIDには決定的なUUIDを割り当て、元の`thread_id`は保持します。同じSQLiteデータを再実行しても会話・メモIDは重複しません。

## 6. 保存期限

新しい会話の保存期限は`CONVERSATION_RETENTION_DAYS`で設定します。次のコマンドを定期実行すると、期限切れの会話、メモ、実行履歴、チェックポイントを削除します。

```bash
uv run python -m src.db.cleanup --limit 100
```

実行中の会話は削除せず、次回の処理へ保留します。定期スケジュールへの登録は運用環境側で行います。

## 7. バックアップ上の注意

- `docker compose down -v`はPostgreSQLボリュームを削除するため実行しないでください。
- アプリ更新前とSQLite移行前にPostgreSQLの論理バックアップを取得してください。
- 復旧テストと自動バックアップはフェーズ7の運用・デプロイで正式化します。
