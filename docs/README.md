# LangGraph + Python + Ollama エージェント ドキュメント

本ディレクトリは、LangGraph、Python、Ollamaを用いた自律型ReActエージェントアプリケーションの設計および利用ガイドです。

---

## 📑 目次

1. **[アーキテクチャ・設計詳細](architecture.md)**
   - システム全体図
   - LangGraph ReActループとStateGraph設計
   - 状態管理 (`AgentState`) と 永続メモリ (`AsyncCompatibleSqliteSaver`)
   - ツール連携メカニズム
2. **[利用ガイド & 操作方法](usage_guide.md)**
   - セットアップと前提環境
   - リッチCLIインターフェースの使い方 & スラッシュコマンド
   - Streamlit Web UIの使い方 & 設定項目
   - FastAPIの起動方法
   - 単体テストの実行方法
3. **[拡張 & カスタマイズガイド](customization.md)**
   - 新しいツールの追加方法
   - Ollamaモデルの変更とパラメータ調整
   - システムプロンプトのカスタマイズ
   - チェックポインタ（永続データベース）の拡張
4. **[Web・モバイル向けAPI実装計画](api_implementation_plan.md)**
   - FastAPIとSSEによる共通API
   - 認証・認可とPostgreSQL対応
   - StreamlitのAPIクライアント化
   - テスト、監視、デプロイ計画
5. **[API利用ガイド](api_guide.md)**
   - APIの起動とエンドポイント
   - 通常応答とSSEストリーミング
   - 冪等性、エラー処理、会話・メモ管理
6. **[PostgreSQL構築・移行ガイド](postgresql_guide.md)**
   - Docker Compose構成と接続設定
   - AlembicとLangGraphテーブル初期化
   - SQLite移行、保存期限、バックアップ上の注意
7. **[Keycloak・OIDC構築ガイド](keycloak_oidc_guide.md)**
   - Dockge向けKeycloak構成
   - WEB・モバイルのPKCEクライアント
   - APIのJWT検証設定と確認方法

### 実装状況

- フェーズ1「エージェント共通コアの分離」：`feature/phase1-agent-core`ブランチで実装済み
- フェーズ2「FastAPIの最小API」：`develop`へマージ済み
- フェーズ3「PostgreSQLと会話管理」：`develop`へマージ済み
- フェーズ4-A「OIDC/JWT認証と所有者分離」：`develop`へマージ済み
- フェーズ4-B「API保護の強化」：`develop`へマージ済み
- フェーズ5「StreamlitのAPIクライアント化」：`develop`へマージ済み
- Streamlit会話選択改善：`codex/fix-streamlit-conversation-selection`ブランチで実装・動作確認済み
- Streamlit IME誤送信・生成停止改善：`codex/fix-streamlit-ime-cancel`ブランチで実装・自動テスト済み（ログイン後の実画面確認待ち）
- フェーズ6以降：未着手

---

## 🚀 プロジェクト概要

- **プロジェクトパス**: `/Users/mauda/Projects/langgraph_sample`
- **主要スタック**:
  - Python 3.12 (`uv` パッケージ管理)
  - `langgraph` (0.2.x+ / 1.x+)
  - `langchain-ollama` (ローカルLLM連携)
  - `rich` (CLI対話)
  - `streamlit` (Web UI)
  - `fastapi` / `uvicorn` (Web・モバイル向けAPI)
  - `postgresql` / `sqlalchemy` / `alembic` (永続化・マイグレーション)
  - `keycloak` / `pyjwt` (OIDC認証・JWT検証)
  - FastAPI CORS / PostgreSQL共有レート制限 / JSON構造化ログ
  - `ddgs` (Web検索ツール)
